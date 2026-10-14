"""A simulation never mutates mappings or broadens human review scopes."""
from copy import deepcopy
import sqlite3

import pytest

from fba_replenishment.core import connect
from fba_replenishment.discovery import approve_mapping, discover, mark_stop
from fba_replenishment.first_batch_validation import validate_first_batch
from fba_replenishment.review_queue import build_review_queue

US, CA = 'ATVPDKIKX0DER', 'A2EUQ1WTGCTBG2'
STAMP = '2026-10-14T12:00:00+00:00'


def inventory(sku, asin='B000000001'):
    return {'sellerSku': sku, 'asin': asin, 'fnSku': 'FN-' + sku, 'condition': 'NewItem',
            'inventoryDetails': {'fulfillableQuantity': 5,
                                 'reservedQuantity': {'totalReservedQuantity': 0},
                                 'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
                                 'inboundWorkingQuantity': 0, 'inboundShippedQuantity': 0,
                                 'inboundReceivingQuantity': 0}}


def queue(con, *, warning=False):
    source = {sku: {'candidate_family': 'J001', 'basis': 'USER_WORKBOOK_EXACT_SKU',
                    'format_issue': warning and sku == 'J001-size 6'}
              for sku in ('J001-size 5', 'J001-size 6')}
    discover(con, [inventory(sku) for sku in source], 'US', US,
             source_candidates=source, observed_at=STAMP)
    return build_review_queue(con, 'US', asof='2026-10-13', observed_at=STAMP)


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / 'first.db'
    con = connect(path)
    frozen = queue(con)
    yield con, frozen, path
    con.close()


def decision(group, action='APPROVE', **changes):
    return dict(group, decision=action, reviewer='manager', reason='Reviewed the exact collection',
                approved_family=group['candidate_family'], **changes)


def validate(con, frozen, **kwargs):
    return validate_first_batch(con, frozen['groups'], frozen['items'], **kwargs)


def test_default_review_is_pending_and_never_import_ready(prepared):
    con, frozen, _ = prepared
    result = validate(con, frozen)
    assert result['status'] == 'PENDING_REVIEW'
    assert result['structure_status'] == 'READY'
    assert result['decision_status'] == 'PENDING'
    assert result['requires_explicit_user_confirmation'] is True
    assert result['import_permitted'] is False
    assert result['summary']['pending_group_count'] == 1
    assert result['summary']['approval_ready_group_count'] == 1
    assert result['summary']['sales_unknown_sku_count'] == 2


def test_approve_simulation_uses_read_only_database_and_selects_only(prepared):
    con, frozen, path = prepared
    before = path.read_bytes()
    original = list(con.execute('SELECT * FROM discovery_sku'))
    readonly = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    readonly.row_factory = sqlite3.Row
    observed = []

    def authorize(action, *args):
        observed.append(action)
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                                               sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    readonly.set_authorizer(authorize)
    try:
        result = validate(readonly, frozen, decisions=[decision(frozen['groups'][0])])
        assert not readonly.in_transaction
        assert result['status'] == 'SIMULATED_REQUIRES_EXPLICIT_CONFIRMATION'
        assert result['summary']['approve_simulated_group_count'] == 1
    finally:
        readonly.close()
    assert sqlite3.SQLITE_SELECT in observed
    assert path.read_bytes() == before
    assert list(con.execute('SELECT * FROM discovery_sku')) == original
    assert con.execute('SELECT COUNT(*) FROM batch_review_decision').fetchone()[0] == 0


@pytest.mark.parametrize('tamper', ['remove', 'duplicate', 'asin', 'digest', 'candidate'])
def test_visible_collection_tampering_blocks_entire_selected_group(prepared, tamper):
    con, original, _ = prepared
    frozen = deepcopy(original)
    if tamper == 'remove':
        frozen['items'].pop()
    elif tamper == 'duplicate':
        frozen['items'].append(deepcopy(frozen['items'][0]))
    elif tamper == 'asin':
        frozen['items'][0]['asin'] = 'B999999999'
    elif tamper == 'digest':
        frozen['items'][0]['member_digest'] = 'edited'
    else:
        frozen['items'][0]['candidate_family'] = 'OTHER'
    result = validate(con, frozen, decisions=[decision(frozen['groups'][0])])
    assert result['status'] == 'BLOCKED'
    assert result['summary']['approve_simulated_group_count'] == 0
    assert result['summary']['blocked_selected_group_count'] == 1


@pytest.mark.parametrize('column,value', [('asin', 'B999999999'),
                                        ('candidate_family', 'J002'),
                                        ('mapping_basis', 'NEW_SOURCE'),
                                        ('presence_status', 'UNKNOWN')])
def test_original_state_changes_are_stale(prepared, column, value):
    con, frozen, _ = prepared
    with con:
        con.execute(f'UPDATE discovery_sku SET {column}=? WHERE sku=?', (value, 'J001-size 5'))
    result = validate(con, frozen, decisions=[decision(frozen['groups'][0])])
    assert result['status'] == 'BLOCKED'
    assert 'STALE_REVIEW_STATE' in result['groups'][0]['structure_blockers']


def test_format_exception_blocks_only_its_approval_not_extraction(tmp_path):
    con = connect(tmp_path / 'warning.db')
    frozen = queue(con, warning=True)
    result = validate(con, frozen)
    assert result['status'] == 'PENDING_REVIEW'
    assert result['structure_status'] == 'READY'
    assert result['summary']['group_count'] == 2
    assert result['summary']['blocked_group_count'] == 1
    assert result['summary']['approval_ready_group_count'] == 1
    blocked = next(row for row in result['groups'] if row['approval_status'] == 'BLOCKED')
    assert 'REFERENCE_FORMAT_WARNING_REQUIRES_MANUAL_RESOLUTION' in blocked['approval_blockers']
    result = validate(con, frozen, decisions=[decision(group) for group in frozen['groups']])
    assert result['status'] == 'BLOCKED'
    con.close()


def test_existing_manual_family_is_protected_without_remapping(prepared):
    con, _, _ = prepared
    approve_mapping(con, 'US', 'J001-size 5', 'OTHER', 'original-reviewer', STAMP)
    frozen = build_review_queue(con, 'US', asof='2026-10-13', observed_at=STAMP)
    # An edited group cannot be used to override a protected formal family.
    candidate = next(group for group in frozen['groups'] if group['candidate_family'] == 'OTHER')
    candidate['candidate_family'] = 'J001'
    result = validate(con, frozen)
    assert any('PROTECTED_FORMAL_MAPPING' in group['blockers'] for group in result['groups'])
    assert con.execute('SELECT family FROM discovery_sku WHERE sku=?', ('J001-size 5',)).fetchone()[0] == 'OTHER'


def test_mapping_conflict_cannot_be_silently_resolved(prepared):
    con, _, _ = prepared
    with con:
        con.execute("UPDATE discovery_sku SET mapping_conflict=1,mapping_status='FAMILY_CONFLICT' WHERE sku='J001-size 5'")
    frozen = build_review_queue(con, 'US', asof='2026-10-13', observed_at=STAMP)
    result = validate(con, frozen)
    assert result['summary']['blocked_group_count'] == 1
    assert any('MAPPING_CONFLICT' in group['approval_blockers'] for group in result['groups'])


def test_unknown_quantities_are_preserved_as_unknown(prepared):
    con, frozen, _ = prepared
    for item in frozen['items']:
        for key in ('fulfillable', 'reserved', 'unfulfillable', 'inbound_working',
                    'inbound_shipped', 'inbound_receiving'):
            item[key] = 0
    frozen['items'][0]['fulfillable'] = None
    result = validate(con, frozen)
    assert frozen['items'][0]['fulfillable'] is None
    assert result['summary']['inventory_unknown_sku_count'] == 1
    assert result['summary']['sales_unknown_sku_count'] == 2


def test_new_future_sku_is_outside_frozen_approval_collection(prepared):
    con, frozen, _ = prepared
    discover(con, [inventory('J001-size 5'), inventory('J001-size 6'), inventory('J001-size 7')],
             'US', US, observed_at=STAMP,
             source_candidates={sku: {'candidate_family': 'J001', 'basis': 'USER_WORKBOOK_EXACT_SKU'}
                                for sku in ('J001-size 5', 'J001-size 6')})
    result = validate(con, frozen, decisions=[decision(frozen['groups'][0])])
    assert result['status'] == 'SIMULATED_REQUIRES_EXPLICIT_CONFIRMATION'
    assert result['summary']['sku_count'] == 2
    assert result['summary']['future_skus_auto_approved'] == 0
    assert con.execute('SELECT family FROM discovery_sku WHERE sku=?', ('J001-size 7',)).fetchone()[0] is None


def test_direct_stop_on_pending_member_predicts_family_spillover(prepared):
    con, _, _ = prepared
    discover(con, [inventory('outside')], 'US', US, observed_at=STAMP)
    approve_mapping(con, 'US', 'outside', 'J001', 'manager', STAMP)
    mark_stop(con, 'US', 'J001-size 5', 'Do not replenish', STAMP)
    frozen = build_review_queue(con, 'US', asof='2026-10-13', observed_at=STAMP)
    chosen = next(group for group in frozen['groups'] if group['exact_match_count'])
    first = {'groups': [chosen], 'items': [item for item in frozen['items'] if item['group_id'] == chosen['group_id']]}
    result = validate(con, first)
    risk = result['groups'][0]['stop_risk']
    assert risk['direct_stopped_member_count'] == 1
    assert risk['projected_stopped_member_count'] == 2
    assert risk['outside_review_formal_child_count'] == 1
    assert con.execute('SELECT COUNT(*) FROM family_stop WHERE active=1').fetchone()[0] == 0
    assert con.execute('SELECT replenishment_status FROM discovery_sku WHERE sku=?', ('outside',)).fetchone()[0] == 'MAPPING_APPROVED'


def test_same_family_stop_in_other_marketplace_does_not_propagate(prepared):
    con, frozen, _ = prepared
    discover(con, [inventory('canada')], 'CA', CA, observed_at=STAMP)
    approve_mapping(con, 'CA', 'canada', 'J001', 'manager', STAMP)
    mark_stop(con, 'CA', 'canada', 'Canada only', STAMP)
    result = validate(con, frozen)
    assert not result['groups'][0]['stop_risk']['known_stopped_family']
    assert result['summary']['stop_risk_sku_count'] == 0


def test_stop_preview_after_approval_is_read_only_and_reports_same_family_children(prepared):
    con, frozen, _ = prepared
    stops = [{'marketplace': 'US', 'api_marketplace': US, 'sku': 'J001-size 5', 'family': 'J001',
              'stop': True, 'review_status': 'APPROVE', 'reviewer': 'manager',
              'reason': 'Manual stop requested', 'effective_date': '2026-10-14'}]
    result = validate(con, frozen, decisions=[decision(frozen['groups'][0])], stops=stops)
    assert result['stops'][0]['status'] == 'SIMULATED_STOP_REQUIRES_CONFIRMATION'
    assert result['stops'][0]['affected_sku_count'] == 2
    assert con.execute('SELECT COUNT(*) FROM stop_flag').fetchone()[0] == 0


def test_stop_cannot_approve_a_pending_mapping_and_false_cannot_resume(prepared):
    con, frozen, _ = prepared
    row = {'marketplace': 'US', 'sku': 'J001-size 5', 'family': 'J001', 'stop': True,
           'review_status': 'APPROVE', 'reviewer': 'manager', 'reason': 'Manual stop',
           'effective_date': '2026-10-14'}
    result = validate(con, frozen, stops=[row])
    assert 'STOP_REQUIRES_APPROVED_MAPPING' in result['stops'][0]['blockers']
    result = validate(con, frozen, stops=[dict(row, stop=False)])
    assert result['stops'][0]['status'] == 'NO_CHANGE'


@pytest.mark.parametrize('changes', [{'snapshot_id': 'edited'}, {'member_digest': 'edited'},
                                     {'reviewer': None}, {'reason': '=formula'},
                                     {'approved_family': 'OTHER'}, {'decision': 'APPROVED'}])
def test_edited_decision_identity_or_unsupported_values_are_blocked(prepared, changes):
    con, frozen, _ = prepared
    row = decision(frozen['groups'][0])
    row.update(changes)
    result = validate(con, frozen, decisions=[row])
    assert result['status'] == 'BLOCKED' or result['structure_status'] == 'BLOCKED'
    assert not result['import_permitted']


def test_missing_schema_reports_unknown_counts_and_does_not_create_tables():
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    result = validate_first_batch(con, [], [])
    assert result['status'] == 'BLOCKED'
    assert result['summary']['stop_risk_sku_count'] is None
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    con.close()
