"""Automatic SKU discovery never grants replenishment or overwrites approval."""
from copy import deepcopy
import json

import pytest

from fba_replenishment.core import calculate, connect
from fba_replenishment.discovery import (
    approve_mapping, approved_rules, discover, discovery_export_rows, mark_inactive,
    manual_resume, mark_stop, register_approved_rule, should_run_discovery,
)
from fba_replenishment.spapi import SPAPIError

US, CA = 'ATVPDKIKX0DER', 'A2EUQ1WTGCTBG2'
FIRST = '2026-10-14T12:00:00+00:00'
SECOND = '2026-10-14T13:00:00+00:00'
NEXT = '2026-10-15T12:00:00+00:00'


def inventory(sku='J001-size 5', asin='B012345678', fnsku=None):
    return {'sellerSku': sku, 'fnSku': fnsku or 'FN-' + sku, 'asin': asin,
            'condition': 'NewItem', 'BuyerEmail': 'must-not-persist@example.invalid',
            'inventoryDetails': {'fulfillableQuantity': 8,
                                 'reservedQuantity': {'totalReservedQuantity': 2},
                                 'unfulfillableQuantity': {'totalUnfulfillableQuantity': 1},
                                 'inboundWorkingQuantity': 0,
                                 'inboundShippedQuantity': 0,
                                 'inboundReceivingQuantity': 0}}


@pytest.fixture
def con(tmp_path):
    connection = connect(tmp_path / 'discovery.db')
    yield connection
    connection.close()


def run(con, rows=None, timestamp=FIRST, **kwargs):
    return discover(con, [inventory()] if rows is None else rows, 'US', US,
                    observed_at=timestamp, **kwargs)


def row(con, sku='J001-size 5', market='US'):
    return next(r for r in discovery_export_rows(con, market) if r['sku'] == sku)


def naming_rule():
    return {'pattern': r'(J\d+)-size\s+\d+(?:\.\d+)?', 'family_group': 1,
            'approved': True, 'approved_by': 'manager', 'approved_at': FIRST,
            'name': 'J-size'}


def test_new_sku_is_discovered_with_api_identity_and_first_last_times(con):
    result = run(con)
    assert result['new_count'] == result['sku_count'] == 1
    found = row(con)
    assert found['api_marketplace'] == US
    assert found['first_discovered_at'] == found['last_seen_at'] == FIRST
    assert found['is_new'] is True and found['family'] is None
    assert con.execute("SELECT family FROM sku WHERE marketplace='US'").fetchone()[0] is None
    assert 'must-not-persist' not in json.dumps(result)


def test_existing_sku_is_not_duplicated_and_first_seen_is_preserved(con):
    run(con)
    result = run(con, timestamp=SECOND)
    assert result['new_count'] == 0 and result['existing_count'] == 1
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 1
    found = row(con)
    assert found['first_discovered_at'] == FIRST and found['last_seen_at'] == SECOND
    # Run delta is zero, but daily-new rows survive a repeated run on the same day.
    assert found['is_new'] is True


def test_new_size_matches_approved_rule_as_candidate_without_auto_approval(con):
    register_approved_rule(con, 'US', US, 'confirmed-size-format',
                           r'(R\d+)-size\s+\d+(?:\.\d+)?', 1, 'operator', FIRST)
    result = run(con, [inventory('R005-size 7')])
    found = result['records'][0]
    assert found['candidate_family'] == found['effective_family'] == 'R005'
    assert found['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert found['match_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert found['family'] is None and found['recommended_qty'] == 0
    assert len(approved_rules(con, US)) == 1


def test_stopped_family_new_child_inherits_stop_without_mapping_approval(con):
    run(con)
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'operator', FIRST)
    mark_stop(con, 'US', 'J001-size 5', 'Series discontinued', FIRST)
    run(con, [inventory(), inventory('J001-size 8')], timestamp=SECOND)
    child = row(con, 'J001-size 8')
    assert child['status'] == child['replenishment_status'] == 'STOPPED'
    assert child['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW' and child['family'] is None
    assert all(r['recommended_qty'] == 0 for r in calculate(con))
    assert con.execute("SELECT COUNT(*) FROM stop_flag WHERE active=1").fetchone()[0] == 1


def test_other_family_unaffected_by_stop_and_resume_requires_approver(con):
    run(con, [inventory(), inventory('J002-size 5')])
    mark_stop(con, 'US', 'J001-size 5', 'Series stop', FIRST)
    assert row(con, 'J002-size 5')['replenishment_status'] == 'BLOCKED_MAPPING'
    with pytest.raises(ValueError, match='approver'):
        manual_resume(con, 'US', 'J001-size 5', '', SECOND)
    manual_resume(con, 'US', 'J001-size 5', 'manager', SECOND)
    assert row(con)['replenishment_status'] == 'BLOCKED_MAPPING'
    assert con.execute("SELECT COUNT(*) FROM audit WHERE action='RESUME'").fetchone()[0] == 1


def test_same_sku_different_marketplaces_have_independent_identity_and_stops(con):
    run(con)
    discover(con, [inventory()], 'CA', CA, observed_at=FIRST)
    mark_stop(con, 'US', 'J001-size 5', 'US stop only', FIRST)
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 2
    assert row(con)['status'] == 'STOPPED'
    assert row(con, market='CA')['status'] == 'AUTO_MATCHED_PENDING_REVIEW'


def test_unknown_family_is_blocked_and_source_format_issue_is_review_required(con):
    run(con, [inventory('unknown'), inventory('format-check')], source_candidates={
        'format-check': {'candidate_family': 'MODEL', 'basis': 'USER_WORKBOOK_EXACT_SKU', 'format_issue': 'suffix ambiguous'}})
    found = row(con, 'unknown')
    assert found['mapping_status'] == 'REVIEW_REQUIRED' and found['family'] is None
    assert found['replenishment_status'] == 'BLOCKED_MAPPING'
    assert row(con, 'format-check')['mapping_status'] == 'REVIEW_REQUIRED'
    assert all(r['recommended_qty'] == 0 for r in calculate(con))


def test_conflicting_family_evidence_is_blocked_and_approved_rule_has_priority(con):
    run(con, rules=[naming_rule()], source_candidates={
        'J001-size 5': {'candidate_family': 'J999', 'basis': 'USER_WORKBOOK_EXACT_SKU'}})
    found = row(con)
    assert found['mapping_status'] == 'FAMILY_CONFLICT'
    assert found['replenishment_status'] == 'BLOCKED_CONFLICT'
    assert found['family'] is None and found['candidate_family'] == 'J001'
    assert set(found['candidate_families']) == {'J001', 'J999'}
    assert con.execute("SELECT code FROM data_quality_block WHERE code='DISCOVERY_FAMILY_CONFLICT'").fetchone()


def test_manual_mapping_is_not_overwritten_by_conflicting_new_candidate(con):
    run(con)
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'manager', FIRST)
    run(con, timestamp=SECOND, source_candidates={
        'J001-size 5': {'candidate_family': 'OTHER', 'basis': 'USER_WORKBOOK_EXACT_SKU'}})
    found = row(con)
    assert found['family'] == 'J001' and found['approved_by'] == 'manager'
    assert found['mapping_status'] == 'FAMILY_CONFLICT'
    assert found['replenishment_status'] == 'BLOCKED_CONFLICT'
    assert con.execute("SELECT family FROM sku WHERE marketplace='US'").fetchone()[0] == 'J001'


def test_failed_or_partial_sync_retains_history_presence_and_last_snapshot(con):
    run(con)
    before = row(con)
    failure = discover(con, None, 'US', US, observed_at=SECOND,
                       complete=False, failure_code='SPAPI_HTTP_429')
    assert failure['status'] == 'FAILED'
    assert row(con) == before
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 1
    malformed = inventory()
    del malformed['inventoryDetails']['fulfillableQuantity']
    with pytest.raises(SPAPIError):
        run(con, [malformed], timestamp=NEXT)
    assert row(con) == before
    assert con.execute("SELECT COUNT(*) FROM discovery_run WHERE status='FAILED'").fetchone()[0] == 2
    assert should_run_discovery(con, US, '2026-10-15') is True


def test_successful_absence_is_suspected_only_preserves_sales_and_approval(con):
    run(con, [inventory(), inventory('J002-size 5')])
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'manager', FIRST)
    con.execute("INSERT INTO sales VALUES('US','J001-size 5','2026-10-13',3)")
    con.commit()
    result = run(con, [inventory('J002-size 5')], timestamp=SECOND)
    assert result['missing_count'] == 1
    found = row(con)
    assert found['presence_status'] == 'SUSPECTED_INACTIVE' and found['status'] != 'INACTIVE'
    assert found['family'] == 'J001' and found['mapping_status'] == 'APPROVED'
    assert found['last_seen_at'] == FIRST and found['missing_count'] == 1
    assert con.execute('SELECT COUNT(*) FROM sales').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM sku').fetchone()[0] == 2


def test_reappearance_resets_suspected_absence_not_stop_or_first_seen(con):
    run(con, [inventory(), inventory('J002-size 5')])
    mark_stop(con, 'US', 'J001-size 5', 'Keep stopped', FIRST)
    run(con, [inventory('J002-size 5')], timestamp=SECOND)
    run(con, [inventory(), inventory('J002-size 5')], timestamp=NEXT)
    found = row(con)
    assert found['presence_status'] == 'ACTIVE' and found['missing_count'] == 0
    assert found['first_discovered_at'] == FIRST and found['status'] == 'STOPPED'


def test_daily_baseline_is_fixed_and_run_delta_is_independent(con):
    first = run(con)
    second = run(con, [inventory(), inventory('J002-size 5')], timestamp=SECOND)
    assert first['daily_baseline_count'] == second['daily_baseline_count'] == 0
    assert first['daily_new_count'] == 1 and second['daily_new_count'] == 2
    assert second['new_count'] == 1 and second['existing_count'] == 1
    assert should_run_discovery(con, US, '2026-10-14') is False
    assert should_run_discovery(con, US, '2026-10-15') is True
    third = run(con, [inventory(), inventory('J002-size 5')], timestamp=NEXT)
    assert third['daily_baseline_count'] == 2 and third['daily_new_count'] == 0
    assert all(r['is_new'] is False for r in third['records'])


def test_rules_require_approval_metadata_and_full_match(con):
    with pytest.raises(ValueError, match='approver'):
        run(con, rules={'pattern': r'(J\d+)-size\s+\d+', 'approved': True})
    rule = {'pattern': r'(R\d+)-size\s+\d+', 'family_group': 1,
            'approved': True, 'approved_by': 'manager', 'approved_at': FIRST, 'name': 'R-size'}
    run(con, [inventory('R001-size 5-extra')], rules=[rule])
    assert row(con, 'R001-size 5-extra')['candidate_family'] is None
    run(con, [inventory('R001-size 5')], timestamp=SECOND,
        rules=[dict(rule, approved=False)])
    assert row(con, 'R001-size 5')['candidate_family'] is None


def test_reference_template_approval_field_cannot_approve_mapping(con):
    run(con, [inventory('reference-sku')], source_candidates={'reference-sku': {
        'candidate_family': 'MODEL', 'basis': 'USER_WORKBOOK_EXACT_SKU',
        'approved': True, 'approved_by': 'forged-template-approver'}})
    found = row(con, 'reference-sku')
    assert found['family'] is None and found['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert found['approved_by'] is None


def test_manual_resolution_of_existing_conflict_survives_unchanged_evidence(con):
    sources = {'J001-size 5': {'candidate_family': 'J999', 'basis': 'USER_WORKBOOK_EXACT_SKU'}}
    run(con, rules=[naming_rule()], source_candidates=sources)
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'manager', FIRST)
    run(con, timestamp=SECOND, rules=[naming_rule()], source_candidates=sources)
    assert row(con)['mapping_status'] == 'APPROVED'


def test_multi_fnsku_rows_single_master_and_conflicting_asins_block(con):
    second = inventory(fnsku='SECOND-FNSKU')
    run(con, [inventory(), second])
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 1
    assert len(row(con)['inventory_identities']) == 2
    second['asin'] = 'B987654321'
    run(con, [inventory(), second], timestamp=SECOND)
    assert row(con)['mapping_status'] == 'FAMILY_CONFLICT'


def test_empty_snapshot_and_duplicate_inventory_cannot_mark_missing(con):
    run(con)
    before = row(con)
    with pytest.raises(SPAPIError, match='Empty'):
        run(con, [], timestamp=SECOND)
    with pytest.raises(SPAPIError):
        run(con, [inventory(), deepcopy(inventory())], timestamp=SECOND)
    assert row(con) == before


def test_same_namespace_cannot_mix_marketplace_and_stale_observations_rejected(con):
    run(con)
    with pytest.raises(ValueError, match='another API marketplace'):
        discover(con, [inventory()], 'US', CA, observed_at=SECOND)
    with pytest.raises(ValueError, match='older'):
        run(con, timestamp='2026-10-13T12:00:00+00:00')
    assert row(con)['last_seen_at'] == FIRST


def test_source_conflicting_families_captured_and_no_fallback_conflict_with_model(con):
    run(con, source_candidates={'J001-size 5': {
        'candidate_family': 'MODEL-A', 'conflicting_families': ['MODEL-A', 'MODEL-B'],
        'basis': 'USER_WORKBOOK_EXACT_SKU'}})
    assert row(con)['mapping_status'] == 'FAMILY_CONFLICT'
    assert set(row(con)['candidate_families']) == {'MODEL-A', 'MODEL-B'}
    run(con, timestamp=SECOND, source_candidates={'J001-size 5': {
        'candidate_family': 'MODEL-A', 'basis': 'USER_WORKBOOK_EXACT_SKU'}})
    assert row(con)['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert row(con)['candidate_family'] == 'MODEL-A'


def test_catalog_is_candidate_only_and_not_compared_to_model_identifier(con):
    run(con, source_candidates={'J001-size 5': {
        'candidate_family': 'MODEL-A', 'basis': 'USER_WORKBOOK_EXACT_SKU'}},
        catalog_candidates={'B012345678': ['B098765432']})
    assert row(con)['candidate_family'] == 'MODEL-A'
    assert row(con)['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert row(con)['family'] is None


def test_manual_inactive_requires_approver_is_retained_and_does_not_delete(con):
    run(con)
    with pytest.raises(ValueError, match='approver'):
        mark_inactive(con, 'US', 'J001-size 5', '', 'Retired', SECOND)
    mark_inactive(con, 'US', 'J001-size 5', 'manager', 'Retired', SECOND)
    assert row(con)['status'] == 'INACTIVE'
    run(con, timestamp=NEXT)
    assert row(con)['presence_status'] == row(con)['status'] == 'INACTIVE'
    assert con.execute('SELECT COUNT(*) FROM sku').fetchone()[0] == 1


def test_seller_sku_whitespace_is_not_normalized_into_another_identity(con):
    run(con, [inventory('exact'), inventory(' exact')])
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 2
    assert {r['sku'] for r in discovery_export_rows(con)} == {'exact', ' exact'}


def test_exact_reference_with_unknown_model_does_not_trigger_name_fallback(con):
    run(con, source_candidates={'J001-size 5': {
        'candidate_family': None, 'basis': 'USER_WORKBOOK_EXACT_SKU'}})
    assert row(con)['mapping_status'] == 'REVIEW_REQUIRED'
    assert row(con)['candidate_family'] is None


def test_discovery_inside_caller_transaction_rolls_back_with_stock_snapshot(con):
    run(con)
    before = row(con)
    runs = con.execute('SELECT COUNT(*) FROM discovery_run').fetchone()[0]
    with pytest.raises(RuntimeError, match='later validation'):
        with con:
            con.execute("INSERT INTO stock VALUES('US','J001-size 5',8,2,1,?)", (SECOND,))
            discover(con, [inventory(), inventory('J002-size 5')], 'US', US,
                     observed_at=SECOND, manage_transaction=False)
            raise RuntimeError('later validation failed')
    assert row(con) == before
    assert con.execute('SELECT COUNT(*) FROM stock').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM discovery_run').fetchone()[0] == runs


def test_validation_failure_inside_caller_transaction_never_commits_parent(con):
    run(con)
    malformed = inventory()
    del malformed['inventoryDetails']['fulfillableQuantity']
    with pytest.raises(SPAPIError):
        with con:
            con.execute("UPDATE sku SET family='UNCOMMITTED' WHERE marketplace='US'")
            discover(con, [malformed], 'US', US, observed_at=SECOND,
                     manage_transaction=False)
    assert con.execute('SELECT family FROM sku').fetchone()[0] is None
    assert con.execute("SELECT COUNT(*) FROM discovery_run WHERE status='FAILED'").fetchone()[0] == 0
