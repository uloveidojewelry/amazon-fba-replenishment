"""Frozen human scopes, atomic audit trails and monotonic stop imports."""
import csv
import json
import sqlite3

import pytest
from openpyxl import Workbook

from fba_replenishment.batch_decisions import (
    approve_batch, create_review_manifest, import_stop_list, normalize_stop_row,
    refresh_pending_stops, reject_batch, review_audit_rows, review_manifest_members,
)
from fba_replenishment.core import calculate, connect
from fba_replenishment.discovery import (
    _refresh_safety, approve_mapping, discover, discovery_export_rows, manual_resume, mark_stop,
)

US, CA = 'ATVPDKIKX0DER', 'A2EUQ1WTGCTBG2'
FIRST, SECOND = '2026-10-14T12:00:00+00:00', '2026-10-15T12:00:00+00:00'


def inventory(sku, asin='B000000001'):
    return {'sellerSku': sku, 'asin': asin, 'fnSku': 'FN-' + sku, 'condition': 'NewItem',
            'inventoryDetails': {'fulfillableQuantity': 5,
                                 'reservedQuantity': {'totalReservedQuantity': 0},
                                 'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
                                 'inboundWorkingQuantity': 0, 'inboundShippedQuantity': 0,
                                 'inboundReceivingQuantity': 0}}


@pytest.fixture
def con(tmp_path):
    connection = connect(tmp_path / 'review.db')
    discover(connection, [inventory('J001-size 5'), inventory('J001-size 6'), inventory('J002-size 5')],
             'US', US, observed_at=FIRST)
    yield connection
    connection.close()


def state(con, sku='J001-size 5', marketplace='US'):
    return next(row for row in discovery_export_rows(con, marketplace) if row['sku'] == sku)


def manifest(con, family='J001', skus=None, **kwargs):
    return create_review_manifest(con, 'US', US, family,
                                  skus or ['J001-size 5', 'J001-size 6'], observed_at=FIRST, **kwargs)


def approve(con, frozen, family=None, **kwargs):
    return approve_batch(con, frozen['manifest_id'], frozen['member_sha'], family or frozen['family'],
                         'operator', confirm=True, observed_at=FIRST, **kwargs)


def stop_file(tmp_path, rows, excel=True):
    fields = ['marketplace', 'api_marketplace', 'sku', 'family', 'stop', 'reason',
              'effective_date', 'review_status', 'reviewer']
    path = tmp_path / ('review.xlsx' if excel else 'review.csv')
    if excel:
        workbook = Workbook()
        workbook.active.title = 'Instructions'
        worksheet = workbook.create_sheet('停补导入')
        worksheet.append(fields)
        for row in rows:
            worksheet.append([row.get(field) for field in fields])
        workbook.save(path)
    else:
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return path


def decision(**changes):
    return {'marketplace': 'US', 'api_marketplace': US, 'sku': 'J001-size 5', 'family': 'J001',
            'stop': True, 'reason': 'Confirmed discontinuation', 'effective_date': '2026-10-14',
            'review_status': 'APPROVED', 'reviewer': 'operator', **changes}


def test_manifest_freezes_exact_scope_and_original_states_without_approval(con):
    frozen = manifest(con)
    same = manifest(con)
    assert frozen['manifest_id'] == same['manifest_id']
    assert frozen['batch_eligible'] is True
    members = review_manifest_members(con, frozen['manifest_id'])
    assert [row['sku'] for row in members] == ['J001-size 5', 'J001-size 6']
    assert all(row['expected_state']['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW' for row in members)
    assert state(con)['family'] is None


def test_batch_approval_covers_exact_members_and_never_future_skus(con):
    frozen = manifest(con)
    result = approve(con, frozen)
    assert result['approved_count'] == 2 and result['unchanged_count'] == 0
    assert state(con)['mapping_status'] == 'APPROVED'
    assert state(con, 'J002-size 5')['family'] is None
    discover(con, [inventory('J001-size 5'), inventory('J001-size 6'), inventory('J001-size 7')],
             'US', US, observed_at=SECOND)
    assert state(con, 'J001-size 7')['family'] is None
    assert state(con, 'J001-size 7')['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    audits = review_audit_rows(con, 'US')
    assert len(audits) == 2
    assert all(row['reviewer'] == 'operator' and row['at'] == FIRST for row in audits)
    assert audits[0]['original_values']['family'] is None
    assert audits[0]['changed_values']['family'] == 'J001'


def test_approval_requires_real_confirmation_and_reviewer(con):
    frozen = manifest(con)
    with pytest.raises(ValueError, match='confirmation'):
        approve_batch(con, frozen['manifest_id'], frozen['member_sha'], 'J001', 'operator')
    with pytest.raises(ValueError, match='reviewer'):
        approve_batch(con, frozen['manifest_id'], frozen['member_sha'], 'J001', '', confirm=True)
    assert state(con)['family'] is None


@pytest.mark.parametrize('change', ['asin', 'candidate', 'core', 'stop', 'presence'])
def test_changed_original_state_invalidates_entire_review(con, change):
    frozen = manifest(con)
    with con:
        if change == 'asin':
            con.execute("UPDATE discovery_sku SET asin='B999999999' WHERE sku='J001-size 6'")
        elif change == 'candidate':
            con.execute("UPDATE discovery_sku SET candidate_family='J900' WHERE sku='J001-size 6'")
        elif change == 'core':
            con.execute("UPDATE sku SET family='NEW' WHERE sku='J001-size 6'")
        elif change == 'stop':
            con.execute('INSERT INTO family_stop VALUES(?,?,?,1)', ('US', 'J001', 'J001-size 6'))
        else:
            con.execute("UPDATE discovery_sku SET presence_status='UNKNOWN' WHERE sku='J001-size 6'")
    with pytest.raises(ValueError, match='Stale'):
        approve(con, frozen)
    assert state(con)['family'] is None
    assert not review_audit_rows(con)


def test_wrong_membership_hash_or_target_family_is_rejected(con):
    frozen = manifest(con)
    with pytest.raises(ValueError, match='hash'):
        approve_batch(con, frozen['manifest_id'], '0' * 64, 'J001', 'operator', confirm=True)
    with pytest.raises(ValueError, match='family'):
        approve(con, frozen, family='OTHER')


def test_unknown_format_group_is_non_approvable_even_with_human_cli_flag(con):
    frozen = manifest(con, family=None)
    assert frozen['batch_eligible'] is False
    with pytest.raises(ValueError, match='unknown family'):
        approve(con, frozen, family='ASSUMED')
    with pytest.raises(ValueError, match='unknown family'):
        manifest(con, family=None, batch_eligible=True)


def test_explicitly_defined_manual_family_can_be_frozen_separately(con):
    frozen = manifest(con, family='MANUAL-FAMILY')
    assert not frozen['batch_eligible']
    with pytest.raises(ValueError, match='groups cannot'):
        approve(con, frozen)
    manual = manifest(con, family='MANUAL-FAMILY', batch_eligible=True)
    assert approve(con, manual)['approved_count'] == 2


def test_duplicates_unknown_skus_and_wrong_marketplace_rejected(con):
    with pytest.raises(ValueError, match='duplicate'):
        manifest(con, skus=['J001-size 5', 'J001-size 5'])
    with pytest.raises(ValueError, match='discovered'):
        manifest(con, skus=['J001-size 5', 'UNKNOWN'])
    with pytest.raises(ValueError, match='bound'):
        create_review_manifest(con, 'US', CA, 'J001', ['J001-size 5'])


def test_preserves_existing_manual_approver_unless_explicit_remap(con):
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'original-reviewer', FIRST)
    frozen = manifest(con)
    result = approve(con, frozen)
    assert result['unchanged_count'] == result['approved_count'] == 1
    assert state(con)['approved_by'] == 'original-reviewer'
    remap = manifest(con, family='OTHER', batch_eligible=True)
    with pytest.raises(ValueError, match='remap'):
        approve(con, remap)
    assert approve(con, remap, allow_remap=True)['approved_count'] == 2


def test_conflict_requires_resolution_and_identity_conflict_is_not_overridable(con):
    with con:
        con.execute("UPDATE discovery_sku SET mapping_status='FAMILY_CONFLICT',mapping_conflict=1 WHERE sku='J001-size 5'")
    frozen = manifest(con)
    with pytest.raises(ValueError, match='conflict'):
        approve(con, frozen)
    assert approve(con, frozen, resolve_conflicts=True)['approved_count'] == 2
    with con:
        con.execute("UPDATE discovery_sku SET asin=NULL WHERE sku='J001-size 5'")
    broken = manifest(con)
    with pytest.raises(ValueError, match='identity'):
        approve(con, broken, resolve_conflicts=True, allow_remap=True)


def test_transaction_rolls_back_earlier_members_and_audits_on_write_error(con):
    frozen = manifest(con)
    con.execute('''CREATE TRIGGER fail_second_mapping BEFORE UPDATE OF family ON discovery_sku
        WHEN NEW.sku='J001-size 6' AND NEW.family IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Injected storage failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='storage failure'):
        approve(con, frozen)
    assert state(con)['family'] is None
    assert not review_audit_rows(con)
    assert con.execute('SELECT COUNT(*) FROM batch_review_decision').fetchone()[0] == 0


def test_nested_approval_does_not_commit_callers_unrelated_transaction(con):
    frozen = manifest(con)
    con.execute("INSERT INTO audit(at,action,marketplace,sku) VALUES(?,?,?,?)", (FIRST, 'OUTER', 'US', ''))
    approve(con, frozen)
    assert con.in_transaction
    con.rollback()
    assert state(con)['family'] is None
    assert con.execute("SELECT COUNT(*) FROM audit WHERE action='OUTER'").fetchone()[0] == 0


def test_manifests_and_decisions_are_immutable_and_not_replayable(con):
    frozen = manifest(con)
    for statement in ("UPDATE batch_review_member SET sku='forged'", 'DELETE FROM batch_review_member',
                      "UPDATE batch_review_manifest SET family='forged'"):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            con.execute(statement)
        con.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        con.execute('INSERT INTO batch_review_member VALUES(?,?,?)', (frozen['manifest_id'], 'future', '{}'))
    con.rollback()
    approve(con, frozen)
    with pytest.raises(ValueError, match='already'):
        approve(con, frozen)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        con.execute('DELETE FROM batch_review_audit')
    con.rollback()


@pytest.mark.parametrize('excel', [True, False])
def test_approved_stop_import_stops_confirmed_family_only(con, tmp_path, excel):
    approve(con, manifest(con))
    discover(con, [inventory('J001-size 5')], 'CA', CA, observed_at=FIRST)
    approve_mapping(con, 'CA', 'J001-size 5', 'J001', 'operator', FIRST)
    imported = import_stop_list(con, stop_file(tmp_path, [decision()], excel), 'operator', observed_at=FIRST)
    assert imported['applied_count'] == 1
    assert state(con)['replenishment_status'] == 'STOPPED'
    assert state(con, 'J001-size 6')['replenishment_status'] == 'STOPPED'
    assert state(con, 'J002-size 5')['replenishment_status'] != 'STOPPED'
    assert state(con, marketplace='CA')['replenishment_status'] != 'STOPPED'
    assert all(row['recommended_qty'] == 0 for row in calculate(con))


def test_false_blank_and_unapproved_rows_cannot_resume_or_overwrite_manual_stop(con, tmp_path):
    approve(con, manifest(con))
    mark_stop(con, 'US', 'J001-size 5', 'Keep original manual reason', FIRST)
    before = dict(con.execute('SELECT * FROM stop_flag').fetchone())
    imported = import_stop_list(con, stop_file(tmp_path, [decision(stop=False), decision(stop=None),
                               decision(review_status='UNAPPROVED')]), 'operator', observed_at=SECOND)
    assert imported['no_change_count'] == 3 and imported['applied_count'] == 0
    assert dict(con.execute('SELECT * FROM stop_flag').fetchone()) == before
    import_stop_list(con, stop_file(tmp_path, [decision()]), 'operator', observed_at=SECOND)
    assert dict(con.execute('SELECT * FROM stop_flag').fetchone()) == before


def test_unknown_stop_value_or_invalid_later_row_rolls_back_complete_file(con, tmp_path):
    approve(con, manifest(con))
    for invalid in (decision(stop='UNKNOWN'), decision(sku='J002-size 5', family='J002'),
                    decision(effective_date='not-a-date'), decision(reason=''),
                    decision(api_marketplace=CA), decision(reviewer='someone-else')):
        path = stop_file(tmp_path, [decision(), invalid])
        with pytest.raises(ValueError):
            import_stop_list(con, path, 'operator', observed_at=FIRST)
        assert con.execute('SELECT COUNT(*) FROM stop_flag').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM batch_scheduled_stop').fetchone()[0] == 0


def test_family_only_stop_uses_explicit_confirmed_family_without_mapping_unknowns(con, tmp_path):
    approve(con, manifest(con))
    imported = import_stop_list(con, stop_file(tmp_path, [decision(sku='')]), 'operator', observed_at=FIRST)
    assert imported['applied_count'] == 1
    assert state(con, 'J001-size 6')['replenishment_status'] == 'STOPPED'
    assert state(con, 'J002-size 5')['family'] is None


def test_future_stop_uses_marketplace_business_day_and_preserves_pending(con, tmp_path):
    approve(con, manifest(con))
    with con:
        con.execute("UPDATE marketplace_binding SET sales_timezone='America/Los_Angeles' WHERE marketplace='US'")
    imported = import_stop_list(con, stop_file(tmp_path, [decision(effective_date='2026-10-15')]),
                                'operator', observed_at=FIRST)
    assert imported['pending_count'] == 1 and imported['applied_count'] == 0
    assert state(con)['replenishment_status'] != 'STOPPED'
    assert refresh_pending_stops(con, '2026-10-15T02:00:00+00:00')['applied_count'] == 0
    assert refresh_pending_stops(con, '2026-10-15T07:00:00+00:00')['applied_count'] == 1
    assert state(con, 'J001-size 6')['replenishment_status'] == 'STOPPED'
    assert refresh_pending_stops(con, SECOND)['applied_count'] == 0


def test_future_stop_after_manual_remap_requires_review_and_does_not_stop_wrong_series(con, tmp_path):
    approve(con, manifest(con))
    import_stop_list(con, stop_file(tmp_path, [decision(effective_date='2026-10-15')]),
                     'operator', observed_at=FIRST)
    remap = manifest(con, skus=['J001-size 5'], family='OTHER', batch_eligible=True)
    approve(con, remap, allow_remap=True)
    refreshed = refresh_pending_stops(con, SECOND)
    assert refreshed['applied_count'] == 0 and refreshed['conflict_count'] == 1
    assert con.execute('SELECT status FROM batch_scheduled_stop').fetchone()[0] == 'CONFLICT_PENDING'
    assert con.execute('SELECT COUNT(*) FROM stop_flag').fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM data_quality_block WHERE code='SCHEDULED_STOP_SCOPE_CONFLICT'").fetchone()[0] == 2


def test_duplicate_stop_import_is_idempotent_and_never_replays_after_manual_resume(con, tmp_path):
    approve(con, manifest(con))
    path = stop_file(tmp_path, [decision()])
    import_stop_list(con, path, 'operator', observed_at=FIRST)
    duplicate = import_stop_list(con, path, 'operator', observed_at=SECOND)
    assert duplicate['duplicate_count'] == 1 and duplicate['reviewed_stop_count'] == 0
    assert con.execute('SELECT COUNT(*) FROM batch_scheduled_stop').fetchone()[0] == 1
    manual_resume(con, 'US', 'J001-size 5', 'authorized-manager', SECOND)
    import_stop_list(con, path, 'operator', observed_at=SECOND)
    assert state(con)['replenishment_status'] != 'STOPPED'


def test_excel_formulas_cannot_authorize_stops_and_chinese_headers_normalize(con, tmp_path):
    path = stop_file(tmp_path, [decision(stop='=TRUE()')])
    with pytest.raises(ValueError, match='formulas'):
        import_stop_list(con, path, 'operator', observed_at=FIRST)
    assert normalize_stop_row({'SKU': 'sku', '是否停补': 'FALSE', '审核状态': 'APPROVED'})['stop'] == 'NO_CHANGE'
    with pytest.raises(ValueError, match='Unknown'):
        normalize_stop_row({'stop': 0})


def test_approval_of_stopped_child_never_resumes_it(con, tmp_path):
    approve(con, manifest(con, skus=['J001-size 5']))
    import_stop_list(con, stop_file(tmp_path, [decision()]), 'operator', observed_at=FIRST)
    assert approve(con, manifest(con, skus=['J001-size 6']))['approved_count'] == 1
    assert state(con, 'J001-size 6')['replenishment_status'] == 'STOPPED'
    audit = review_audit_rows(con)
    assert any(row['action'] == 'STOP_IMPORT_APPLIED' for row in audit)
    assert 'B000000001' in json.dumps(audit)


def test_reject_exact_scope_persists_reason_and_keeps_all_quantities_blocked(con):
    frozen = manifest(con)
    result = reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator',
                          'Family requires a new business identifier', confirm=True, observed_at=FIRST)
    assert result['rejected_count'] == 2
    saved = con.execute('SELECT * FROM batch_review_decision').fetchone()
    assert saved['decision_type'] == 'REJECTED' and saved['reviewer'] == 'operator'
    assert saved['reason'] == 'Family requires a new business identifier'
    assert state(con)['mapping_status'] == 'REVIEW_REQUIRED' and state(con)['family'] is None
    assert all(row['recommended_qty'] == 0 for row in calculate(con))
    assert all(row['changed_values']['review_decision'] == 'REJECTED' for row in review_audit_rows(con))
    with pytest.raises(ValueError, match='already'):
        approve(con, frozen)


def test_rejection_preserves_formal_manual_mapping_and_active_stops(con):
    approve_mapping(con, 'US', 'J001-size 5', 'J001', 'manager', FIRST)
    mark_stop(con, 'US', 'J001-size 5', 'Manual stop', FIRST)
    frozen = manifest(con)
    result = reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator',
                          'New evidence must be reviewed', confirm=True, observed_at=FIRST)
    assert result['retained_approved_count'] == 1
    assert state(con)['family'] == 'J001' and state(con)['approved_by'] == 'manager'
    assert state(con)['mapping_status'] == 'APPROVED' and state(con)['replenishment_status'] == 'STOPPED'


def test_rejection_of_unknown_format_group_is_audited_but_not_approved(con):
    frozen = manifest(con, family=None)
    reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator',
                 'Mixed naming formats require individual mapping', confirm=True, observed_at=FIRST)
    assert con.execute('SELECT decision_type FROM batch_review_decision').fetchone()[0] == 'REJECTED'
    assert state(con)['family'] is None


def test_rejection_requires_confirmation_and_rejects_stale_scope_atomically(con):
    frozen = manifest(con)
    with pytest.raises(ValueError, match='confirmation'):
        reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator', 'Reason')
    with con:
        con.execute("UPDATE discovery_sku SET asin='B999999999' WHERE sku='J001-size 6'")
    with pytest.raises(ValueError, match='Stale'):
        reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator',
                     'Reason', confirm=True, observed_at=FIRST)
    assert state(con)['mapping_status'] == 'AUTO_MATCHED_PENDING_REVIEW'
    assert not review_audit_rows(con)


def test_approval_and_stop_import_never_trim_sku_identity(con, tmp_path):
    with pytest.raises(ValueError, match='discovered'):
        manifest(con, skus=[' J001-size 5'])
    approve(con, manifest(con))
    path = stop_file(tmp_path, [decision(sku='J001-size 5 ')])
    with pytest.raises(ValueError, match='discovered'):
        import_stop_list(con, path, 'operator', observed_at=FIRST)
    assert con.execute('SELECT COUNT(*) FROM stop_flag').fetchone()[0] == 0


def test_resume_before_future_stop_requires_new_review_before_reapplying(con, tmp_path):
    approve(con, manifest(con))
    mark_stop(con, 'US', 'J001-size 5', 'Initial manual stop', FIRST)
    import_stop_list(con, stop_file(tmp_path, [decision(effective_date='2026-10-15')]),
                     'operator', observed_at=FIRST)
    manual_resume(con, 'US', 'J001-size 5', 'manager', FIRST)
    result = refresh_pending_stops(con, SECOND)
    assert result['conflict_count'] == 1 and result['applied_count'] == 0
    assert con.execute('SELECT active FROM stop_flag').fetchone()[0] == 0


def test_batch_cannot_overwrite_legacy_core_mapping_without_explicit_remap(con):
    with con:
        con.execute("UPDATE sku SET family='LEGACY-MANUAL' WHERE sku='J001-size 5'")
    frozen = manifest(con)
    with pytest.raises(ValueError, match='remap'):
        approve(con, frozen)
    assert con.execute("SELECT family FROM sku WHERE sku='J001-size 5'").fetchone()[0] == 'LEGACY-MANUAL'
    assert approve(con, frozen, allow_remap=True)['approved_count'] == 2


def test_batch_rejects_inconsistent_core_asin_even_when_mapping_conflict_resolution_is_confirmed(con):
    with con:
        con.execute("UPDATE sku SET asin='B999999999' WHERE sku='J001-size 5'")
    frozen = manifest(con)
    with pytest.raises(ValueError, match='identity conflicts'):
        approve(con, frozen, allow_remap=True, resolve_conflicts=True)
    assert state(con)['family'] is None


def test_deferred_safety_refresh_requires_an_existing_caller_transaction(con):
    frozen = manifest(con)
    with pytest.raises(ValueError, match='existing caller transaction'):
        approve(con, frozen, refresh_safety=False)
    with pytest.raises(ValueError, match='existing caller transaction'):
        reject_batch(con, frozen['manifest_id'], frozen['member_sha'], 'operator',
                     'Reason', confirm=True, refresh_safety=False)
    assert state(con)['family'] is None


def test_one_deferred_refresh_handles_stopped_sibling_across_multiple_frozen_groups(con):
    # A direct stop on an unapproved SKU initially cannot stop a formal series.
    mark_stop(con, 'US', 'J001-size 5', 'Manually stopped trigger', FIRST)
    first = manifest(con, skus=['J001-size 5'])
    second = manifest(con, skus=['J001-size 6'])
    con.execute('BEGIN IMMEDIATE')
    try:
        approve(con, first, refresh_safety=False)
        approve(con, second, refresh_safety=False)
        assert con.in_transaction
        assert con.execute('SELECT COUNT(*) FROM family_stop').fetchone()[0] == 0
        _refresh_safety(con, 'US')
        con.commit()
    except BaseException:
        con.rollback()
        raise
    assert state(con)['replenishment_status'] == 'STOPPED'
    assert state(con, 'J001-size 6')['replenishment_status'] == 'STOPPED'
    assert state(con, 'J002-size 5')['replenishment_status'] != 'STOPPED'
    assert con.execute('SELECT COUNT(*) FROM batch_review_decision').fetchone()[0] == 2
