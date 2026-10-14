"""Independent SKU planning reuses stored snapshots, never network access."""
from datetime import datetime, timedelta, timezone

import pytest

from fba_replenishment.core import calculate, connect, stop
from fba_replenishment.discovery import discover, ensure_schema
from fba_replenishment.review import REVIEW_SCHEMA
from fba_replenishment.sku_plan import calculate_skus


@pytest.fixture
def data(tmp_path):
    con = connect(tmp_path / 'sku.db')
    con.executescript(REVIEW_SCHEMA)
    ensure_schema(con)
    stamp = datetime.now(timezone.utc).isoformat()
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    con.execute("INSERT INTO marketplace_binding VALUES('US','ATVPDKIKX0DER','UTC')")
    for sku, family in [('a', None), ('b', 'same'), ('c', 'other')]:
        con.execute('INSERT INTO sku VALUES(?,?,?,?,1)', ('US', sku, family, 'B000000001'))
        con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)', ('US', sku, 5, 100, 200, stamp))
        con.execute('INSERT INTO inventory_details VALUES(?,?,?,?,?,?)', ('US', sku, 0, 0, 0, stamp))
        con.executemany('INSERT INTO sales VALUES(?,?,?,?)',
                        [('US', sku, (end - timedelta(days=i)).isoformat(), 1) for i in range(90)])
    for source in ('stock', 'sales', 'inbound'):
        con.execute('INSERT INTO sync_log(at,marketplace,kind,status,row_count,detail) VALUES(?,?,?,?,?,?)',
                    (stamp, 'US', source, 'SUCCESS', 3, 'validated complete read-only snapshot'))
    con.commit()
    yield con, end
    con.close()


def planned(data, **options):
    con, end = data
    return {row['sku']: row for row in calculate_skus(con, asof=end.isoformat(), **options)}


def add_inbound(data, *, sku='a', quantity=10, eta_days=5, status='SHIPPED', summary=True):
    con, _ = data
    eta = (datetime.now(timezone.utc).date() + timedelta(days=eta_days)).isoformat() if eta_days is not None else ''
    con.execute('INSERT INTO inbound VALUES(?,?,?,?,?,?)', ('US', sku, 'test-shipment', quantity, eta, status))
    if summary and status in ('SHIPPED', 'IN_TRANSIT', 'RECEIVING', 'DELIVERED', 'CHECKED_IN'):
        con.execute('UPDATE inventory_details SET inbound_shipped=? WHERE marketplace=? AND sku=?',
                    (quantity, 'US', sku))
    if summary and status == 'WORKING':
        con.execute('UPDATE inventory_details SET inbound_working=? WHERE marketplace=? AND sku=?',
                    (quantity, 'US', sku))
    con.commit()


def test_sku_without_family_can_calculate_and_unconfirmed_is_scenario(data):
    row = planned(data)['a']
    assert row['family'] == '' and row['recommended_qty'] == 105
    assert row['status'] == row['parameter_status'] == 'SCENARIO_ONLY'
    assert row['parameters_confirmed'] is False and row['procurement_eligible'] is False
    assert row['data_complete'] is True
    approved_parameters = planned(data, parameters_confirmed=True)['a']
    assert approved_parameters['status'] == 'ACTIVE'
    assert approved_parameters['parameter_status'] == 'CONFIRMED'


def test_four_complete_windows_and_daily_averages(data):
    row = planned(data)['a']
    for days in (7, 30, 60, 90):
        assert row[f'sales_{days}d'] == days
        assert row[f'daily_sales_{days}d'] == 1
        assert row[f'sales_complete_{days}d'] is True
    assert row['sales_status'] == 'SOLD'


def test_30_day_daily_sales_is_default_demand(data):
    con, end = data
    con.execute('UPDATE sales SET units=10 WHERE marketplace=? AND sku=? AND day<?',
                ('US', 'a', (end - timedelta(days=29)).isoformat()))
    con.commit()
    row = planned(data)['a']
    assert row['sales_90d'] == 630
    assert row['daily_sales'] == 1 and row['recommended_qty'] == 105


def test_exact_zero_requires_full_90_day_calendar(data):
    con, end = data
    con.execute("UPDATE sales SET units=0 WHERE marketplace='US' AND sku='a'")
    con.commit()
    assert planned(data)['a']['sales_status'] == 'NO_SALES'
    con.execute('DELETE FROM sales WHERE marketplace=? AND sku=? AND day=?',
                ('US', 'a', (end - timedelta(days=40)).isoformat()))
    con.commit()
    row = planned(data)['a']
    assert row['sales_status'] == 'UNKNOWN'
    assert row['sales_90d'] is None and row['recommended_qty'] is None
    assert row['sales_30d'] == 0  # A distinct short window stays visibly complete.
    assert 'INCOMPLETE_SALES' in row['anomaly_codes']


def test_missing_day_does_not_use_partial_total_as_full_demand(data):
    con, end = data
    con.execute('DELETE FROM sales WHERE marketplace=? AND sku=? AND day=?', ('US', 'a', end.isoformat()))
    con.commit()
    row = planned(data)['a']
    assert row['sales_30d'] is None and row['daily_sales'] is None
    assert row['observed_sales_30d'] == 29 and row['observed_sales_days_30d'] == 29
    assert row['recommended_qty'] is None and row['stockout_risk'] == 'UNKNOWN_DATA'


def test_invalid_date_cannot_replace_missing_calendar_day(data):
    con, end = data
    con.execute('DELETE FROM sales WHERE marketplace=? AND sku=? AND day=?', ('US', 'a', end.isoformat()))
    invalid = end.isoformat()[:7] + '-00'
    con.execute('INSERT INTO sales VALUES(?,?,?,?)', ('US', 'a', invalid, 50))
    con.commit()
    assert planned(data)['a']['sales_90d'] is None


def test_pending_orders_per_sku_block_even_if_daily_zero_rows_exist(data):
    con, _ = data
    con.execute("INSERT INTO data_quality_block VALUES('US','a','ORDERS_PENDING_STATUS','Pending order totals unresolved')")
    con.commit()
    rows = planned(data)
    assert rows['a']['sales_90d'] is None and rows['a']['sales_30d'] is None
    assert rows['a']['observed_sales_90d'] == 90 and rows['a']['recommended_qty'] is None
    assert rows['b']['sales_complete_90d'] and rows['b']['recommended_qty'] > 0


def test_completed_sales_with_sku_exceptions_isolated_to_pending_sku(data):
    con, _ = data
    con.execute("UPDATE sync_log SET status='SUCCESS_WITH_SKU_EXCEPTIONS' WHERE kind='sales'")
    con.execute("INSERT INTO data_quality_block VALUES('US','a','ORDERS_PENDING_STATUS','Pending order totals unresolved')")
    con.execute("DELETE FROM sales WHERE marketplace='US' AND sku='a'")
    con.commit()
    rows = planned(data)
    assert rows['a']['sales_status'] == 'UNKNOWN' and rows['a']['sales_90d'] is None
    assert rows['a']['recommended_qty'] is None
    assert rows['b']['sales_complete_90d'] and rows['b']['recommended_qty'] == 105
    assert rows['b']['status'] == 'SCENARIO_ONLY' and rows['b']['anomaly_codes'] == []


@pytest.mark.parametrize('status', ['PARTIAL', 'FAILED', 'INCOMPLETE', 'UNKNOWN'])
def test_other_non_success_sales_status_blocks_all_skus(data, status):
    con, _ = data
    con.execute("UPDATE sync_log SET status=? WHERE kind='sales'", (status,))
    con.commit()
    rows = planned(data)
    assert all(row['sales_90d'] is None and row['sales_status'] == 'UNKNOWN'
               and row['recommended_qty'] is None for row in rows.values())


@pytest.mark.parametrize('source', ['stock', 'inbound'])
def test_sales_exception_status_is_not_success_for_other_sources(data, source):
    con, _ = data
    con.execute('UPDATE sync_log SET status=? WHERE kind=?', ('SUCCESS_WITH_SKU_EXCEPTIONS', source))
    con.commit()
    assert all(row['recommended_qty'] is None for row in planned(data).values())


def test_no_successful_sales_collection_cannot_certify_zeros(data):
    con, _ = data
    con.execute("DELETE FROM sync_log WHERE kind='sales'")
    con.commit()
    row = planned(data)['a']
    assert row['sales_30d'] is None and row['sales_status'] == 'UNKNOWN'
    assert row['recommended_qty'] is None


@pytest.mark.parametrize('kind', ['stock', 'sales', 'inbound'])
def test_latest_failed_collection_never_uses_old_data_as_current(data, kind):
    con, _ = data
    con.execute('INSERT INTO sync_log(at,marketplace,kind,status,row_count,detail) VALUES(?,?,?,?,?,?)',
                (datetime.now(timezone.utc).isoformat(), 'US', kind, 'FAILED', 0, 'safe classification'))
    con.commit()
    row = planned(data)['a']
    assert row['recommended_qty'] is None and 'SYNC_FAILED' in row['anomaly_codes']
    assert con.execute("SELECT COUNT(*) FROM stock WHERE marketplace='US'").fetchone()[0] == 3


def test_independent_mode_ignores_approval_conflict_and_family_stop_codes(data):
    con, _ = data
    for code in ('MAPPING_UNAPPROVED', 'DISCOVERY_MAPPING_UNAPPROVED',
                 'DISCOVERY_FAMILY_CONFLICT', 'DISCOVERY_FAMILY_STOPPED', 'SCHEDULED_STOP_SCOPE_CONFLICT'):
        con.execute('INSERT INTO data_quality_block VALUES(?,?,?,?)', ('US', 'a', code, 'Family-mode requirement'))
    con.execute("INSERT INTO family_stop VALUES('US','same','b',1)")
    con.commit()
    rows = planned(data)
    assert rows['a']['recommended_qty'] == rows['b']['recommended_qty'] == 105
    assert rows['a']['anomaly_codes'] == []


def test_direct_sku_stop_does_not_stop_other_skus_or_markets(data):
    con, end = data
    con.execute("UPDATE sku SET family='same' WHERE marketplace='US' AND sku='a'")
    con.commit()
    stop(con, 'US', 'a', 'Approved direct discontinuation')
    rows = planned(data)
    assert rows['a']['status'] == 'STOPPED' and rows['a']['recommended_qty'] == 0
    assert rows['b']['recommended_qty'] == rows['c']['recommended_qty'] == 105
    # The legacy family planner and persisted stop policy are unchanged.
    old = {r['sku']: r for r in calculate(con, asof=end.isoformat())}
    assert old['a']['status'] == old['b']['status'] == 'STOPPED'
    assert con.execute("SELECT active FROM family_stop WHERE marketplace='US' AND family='same'").fetchone()[0] == 1
    assert calculate_skus(con, marketplace='CA', asof=end.isoformat()) == []


def test_disabled_sku_stays_zero_even_with_missing_data(data):
    con, _ = data
    con.execute("UPDATE sku SET enabled=0 WHERE marketplace='US' AND sku='a'")
    con.execute("DELETE FROM stock WHERE marketplace='US' AND sku='a'")
    con.commit()
    row = planned(data)['a']
    assert row['status'] == 'STOPPED' and row['recommended_qty'] == 0
    assert row['fulfillable'] is None and 'MISSING_STOCK' in row['anomaly_codes']


def test_reserved_and_unfulfillable_are_not_available_stock(data):
    row = planned(data)['a']
    assert row['reserved'] == 100 and row['unfulfillable'] == 200
    assert row['recommended_qty'] == 105


def test_inventory_summary_is_not_an_additional_inbound_deduction(data):
    add_inbound(data)
    row = planned(data)['a']
    assert row['eligible_inbound'] == 10 and row['inventory_inbound'] == 10
    assert row['recommended_qty'] == 95
    assert row['inbound_complete'] and row['inbound_eta_complete']


def test_unknown_eta_blocks_default_but_explicit_exclude_reports_it(data):
    add_inbound(data, eta_days=None)
    row = planned(data)['a']
    assert row['recommended_qty'] is None and row['unknown_eta_inbound'] == 10
    assert row['inbound_complete'] and not row['inbound_eta_complete']
    excluded = planned(data, inbound_rule='exclude')['a']
    assert excluded['recommended_qty'] == 105 and excluded['eligible_inbound'] == 0
    assert 'INBOUND_REVIEW_REQUIRED' in excluded['anomaly_codes']
    assert not next(a for a in excluded['anomalies'] if a['code'] == 'INBOUND_REVIEW_REQUIRED')['blocking']


def test_eta_outside_lead_horizon_not_deducted(data):
    add_inbound(data, eta_days=60)
    row = planned(data)['a']
    assert row['eligible_inbound'] == 0 and row['inbound_after_horizon'] == 10
    assert row['recommended_qty'] == 105


def test_overdue_eta_requires_confirmation(data):
    add_inbound(data, eta_days=-1)
    row = planned(data)['a']
    assert row['recommended_qty'] is None and 'INBOUND_ETA_OVERDUE' in row['anomaly_codes']


@pytest.mark.parametrize('status', ['UNKNOWN', 'ERROR'])
def test_unknown_shipment_state_blocks_even_when_inbound_excluded(data, status):
    add_inbound(data, status=status)
    row = planned(data, inbound_rule='exclude')['a']
    assert row['recommended_qty'] is None and 'INBOUND_STATE_UNKNOWN' in row['anomaly_codes']
    assert row['inbound_complete'] is False and row['eligible_inbound'] is None


def test_unmatched_inventory_inbound_counters_block(data):
    add_inbound(data, summary=False)
    row = planned(data)['a']
    assert row['recommended_qty'] is None and 'INBOUND_RECONCILIATION_REQUIRED' in row['anomaly_codes']


def test_working_and_terminal_quantities_are_not_deducted(data):
    add_inbound(data, status='WORKING')
    con, _ = data
    con.execute("INSERT INTO inbound VALUES('US','a','closed-shipment',50,'','CLOSED')")
    con.commit()
    row = planned(data)['a']
    assert row['recommended_qty'] == 105 and row['eligible_inbound'] == 0
    assert row['shipment_working_inbound'] == 10 and row['unknown_eta_inbound'] == 0


def test_missing_inventory_counter_remains_unknown(data):
    con, _ = data
    con.execute("UPDATE inventory_details SET inbound_receiving=NULL WHERE marketplace='US' AND sku='a'")
    con.commit()
    row = planned(data)['a']
    assert row['inventory_inbound'] is None and row['inventory_complete'] is False
    assert row['recommended_qty'] is None and row['inbound_receiving'] is None


def test_missing_or_stale_inventory_blocks(data):
    con, _ = data
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    con.execute('UPDATE stock SET observed_at=? WHERE marketplace=? AND sku=?', (old, 'US', 'a'))
    con.commit()
    row = planned(data)['a']
    assert row['recommended_qty'] is None and 'STALE_STOCK' in row['anomaly_codes']


def test_inventory_counters_must_share_snapshot_timestamp(data):
    con, _ = data
    con.execute('UPDATE inventory_details SET observed_at=? WHERE marketplace=? AND sku=?',
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), 'US', 'a'))
    con.commit()
    row = planned(data)['a']
    assert row['inventory_complete'] is False and 'INVENTORY_SNAPSHOT_MISMATCH' in row['anomaly_codes']


def test_case_pack_moq_and_parameter_formula(data):
    row = planned(data, lead_days=10, target_days=15, safety_days=5, moq=50, case_pack=12)['a']
    assert row['recommended_qty'] == 60
    assert row['coverage_days'] == 5 and row['stockout_risk'] == 'STOCKOUT_BEFORE_REPLENISHMENT'


@pytest.mark.parametrize('settings', [
    {'lead_days': -1}, {'target_days': -1}, {'safety_days': -1},
    {'max_stock_age_days': -1}, {'lead_days': float('nan')}, {'target_days': float('inf')},
    {'lead_days': True}, {'moq': 0}, {'case_pack': 0}, {'case_pack': 1.5},
    {'inbound_rule': 'automatic'}, {'parameters_confirmed': 'yes'},
])
def test_invalid_parameters_rejected_before_calculation(data, settings):
    with pytest.raises(ValueError):
        planned(data, **settings)


def test_query_only_calculation_preserves_all_tables(data):
    con, end = data
    con.execute('PRAGMA query_only=ON')
    before = con.total_changes
    queries = []
    con.set_trace_callback(queries.append)
    rows = calculate_skus(con, asof=end.isoformat())
    assert len(rows) == 3 and con.total_changes == before
    assert all(q.lstrip().upper().startswith('SELECT') for q in queries)
    assert len(queries) < 25  # Bounded query count independent of SKU count.


def test_direct_stopped_sku_with_failed_sync_still_deliberately_zero(data):
    con, _ = data
    stop(con, 'US', 'a', 'Do not replenish')
    con.execute("UPDATE sync_log SET status='FAILED'")
    con.commit()
    row = planned(data)['a']
    assert row['recommended_qty'] == 0 and row['status'] == 'STOPPED'
    assert row['sales_90d'] is None and row['fulfillable'] == 5


def test_sku_only_present_in_sales_is_retained_with_unknown_inventory(data):
    con, end = data
    con.execute('INSERT INTO sales VALUES(?,?,?,?)', ('US', 'new-order-sku', end.isoformat(), 2))
    con.commit()
    row = planned(data)['new-order-sku']
    assert row['observed_sales_90d'] == 2 and row['fulfillable'] is None
    assert row['recommended_qty'] is None


def test_family_conflict_does_not_hide_actual_sku_asin_identity_conflict(data):
    con, _ = data
    inventory = []
    for number, asin in enumerate(('B000000001', 'B000000002')):
        inventory.append({'sellerSku': 'a', 'asin': asin, 'fnSku': str(number), 'condition': 'NewItem',
                          'inventoryDetails': {'fulfillableQuantity': 0, 'inboundWorkingQuantity': 0,
                                               'inboundShippedQuantity': 0, 'inboundReceivingQuantity': 0,
                                               'reservedQuantity': {'totalReservedQuantity': 0},
                                               'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0}}})
    discover(con, inventory, 'US', 'ATVPDKIKX0DER')
    row = planned(data)['a']
    assert 'SKU_ASIN_IDENTITY_CONFLICT' in row['anomaly_codes'] and row['recommended_qty'] is None


def test_same_seller_sku_in_another_market_is_isolated(data):
    con, end = data
    stamp = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO marketplace_binding VALUES('CA','A2EUQ1WTGCTBG2','UTC')")
    con.execute("INSERT INTO sku VALUES('CA','a',NULL,'B000000001',1)")
    con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)', ('CA', 'a', 5, 100, 200, stamp))
    con.execute('INSERT INTO inventory_details VALUES(?,?,?,?,?,?)', ('CA', 'a', 0, 0, 0, stamp))
    con.executemany('INSERT INTO sales VALUES(?,?,?,?)',
                    [('CA', 'a', (end - timedelta(days=i)).isoformat(), 1) for i in range(90)])
    for source in ('stock', 'sales', 'inbound'):
        con.execute('INSERT INTO sync_log(at,marketplace,kind,status,row_count,detail) VALUES(?,?,?,?,?,?)',
                    (stamp, 'CA', source, 'SUCCESS', 1, 'validated snapshot'))
    con.commit()
    stop(con, 'US', 'a', 'US only discontinuation')
    ca = calculate_skus(con, marketplace='CA', asof=end.isoformat())[0]
    assert ca['recommended_qty'] == 105 and ca['status'] == 'SCENARIO_ONLY'
    assert planned(data)['a']['status'] == 'STOPPED'


def test_due_scheduled_direct_sku_stop_is_observed_without_family_writes(data):
    con, end = data
    con.execute('CREATE TABLE batch_scheduled_stop(marketplace,sku,status,effective_date,effective_timezone,reason)')
    con.execute('INSERT INTO batch_scheduled_stop VALUES(?,?,?,?,?,?)',
                ('US', 'a', 'PENDING', end.isoformat(), 'UTC', 'Confirmed scheduled SKU stop'))
    con.execute('INSERT INTO batch_scheduled_stop VALUES(?,?,?,?,?,?)',
                ('US', 'b', 'PENDING', (end + timedelta(days=30)).isoformat(), 'UTC', 'Future SKU stop'))
    con.commit()
    before = con.total_changes
    rows = planned(data)
    assert rows['a']['recommended_qty'] == 0 and rows['a']['status'] == 'STOPPED'
    assert rows['b']['recommended_qty'] == 105
    assert con.total_changes == before
    assert con.execute("SELECT status FROM batch_scheduled_stop WHERE sku='a'").fetchone()[0] == 'PENDING'


@pytest.mark.parametrize('code,field', [('INVENTORY_QUANTITY_CONFLICT', 'inventory_complete'),
                                       ('INBOUND_ITEMS_INCOMPLETE', 'inbound_complete')])
def test_persisted_source_quality_blocks_invalidate_completeness(data, code, field):
    con, _ = data
    con.execute('INSERT INTO data_quality_block VALUES(?,?,?,?)', ('US', 'a', code, 'Unresolved source data issue'))
    con.commit()
    row = planned(data)['a']
    assert row[field] is False and row['data_complete'] is False and row['recommended_qty'] is None
