"""Monthly demand never overwrites API evidence or weakens snapshot safety."""
from copy import deepcopy
import hashlib
import math

from openpyxl import Workbook
import pytest

from fba_replenishment.monthly_plan import calculate_monthly_plan, read_monthly_workbook


def workbook(tmp_path, rows):
    path = tmp_path / 'monthly.xlsx'
    wb = Workbook()
    wb.active.title = 'Monthly sales'
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    return path


def api(sku='J001-size 5', **values):
    row = dict(marketplace='US', api_marketplace='ATVPDKIKX0DER', sku=sku,
               asin='B000000001', family='', status='SCENARIO_ONLY',
               parameter_status='SCENARIO_ONLY', parameters_confirmed=False,
               lead_days=35,
               fulfillable=100, reserved=999, unfulfillable=888,
               inbound_working=0, inbound_shipped=10, inbound_receiving=0,
               inventory_inbound=10, eligible_inbound=10, unknown_eta_inbound=0,
               inventory_complete=True, inbound_complete=True, data_complete=True,
               planning_data_complete=True, recommended_qty=0, coverage_days=100,
               daily_sales=1, stockout_risk='COVERED', anomalies=[], anomaly_codes=[])
    for days in (7, 30, 60, 90):
        row.update({f'sales_{days}d': days, f'daily_sales_{days}d': 1,
                    f'sales_complete_{days}d': True,
                    f'observed_sales_days_{days}d': days,
                    f'observed_sales_{days}d': days})
    row.update(values)
    return row


def record(sku='J001-size 5', units=60, status='VALID'):
    return dict(sku=sku, monthly_units=units, monthly_status=status,
                source_rows=[2], issues=[], source_sheet='Monthly sales',
                source_sha256='a' * 64)


def plan(row=None, rec=None, **options):
    return calculate_monthly_plan([row or api()], [rec or record()], **options)[0]


def test_numeric_monthly_header_is_header_not_a_sale(tmp_path):
    path = workbook(tmp_path, [['sku', 1], ['a', 12], ['b', 0]])
    got = read_monthly_workbook(path)
    assert [r['monthly_units'] for r in got['records']] == [12, 0]
    assert got['records'][0]['source_rows'] == [2]
    assert got['audit']['header_row'] == 1
    assert 'MONTHLY_QUANTITY_HEADER_NUMERIC' in got['audit']['issues']
    assert 'MONTHLY_PERIOD_UNSPECIFIED' in got['audit']['issues']
    assert got['audit']['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_sheet_can_be_selected_and_missing_header_is_not_guessed(tmp_path):
    path = workbook(tmp_path, [['a', 12]])
    with pytest.raises(ValueError, match='SKU header'):
        read_monthly_workbook(path)
    path = workbook(tmp_path, [['SKU'], ['a', 3]])
    got = read_monthly_workbook(path, 'Monthly sales')
    assert 'MONTHLY_QUANTITY_HEADER_MISSING' in got['audit']['issues']
    with pytest.raises(KeyError):
        read_monthly_workbook(path, 'missing')


def test_blank_rows_do_not_invent_records(tmp_path):
    got = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], ['a', 1], [None, None], ['b', 2]]))
    assert len(got['records']) == 2 and got['audit']['blank_rows'] == 1


def test_exact_duplicate_is_deduplicated_without_summing(tmp_path):
    got = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], ['a', 12], ['a', 12]]))
    assert len(got['records']) == 1
    assert got['records'][0]['monthly_units'] == 12
    assert got['records'][0]['source_rows'] == [2, 3]
    assert got['records'][0]['monthly_status'] == 'VALID'
    assert 'DUPLICATE_IDENTICAL' in got['records'][0]['issues']
    assert got['audit']['duplicate_identical_skus'] == 1


@pytest.mark.parametrize('second', [14, None, '=6+6'])
def test_conflicting_or_invalid_duplicate_is_unknown(tmp_path, second):
    got = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], ['a', 12], ['a', second]]))
    row = got['records'][0]
    assert row['monthly_status'] == 'DUPLICATE_CONFLICT'
    assert row['monthly_units'] is None and row['source_rows'] == [2, 3]
    assert got['audit']['duplicate_conflict_skus'] == 1


@pytest.mark.parametrize('quantity', [None, -1, 1.2, True, '12', '=2+3'])
def test_invalid_monthly_quantity_is_not_zero_or_evaluated(tmp_path, quantity):
    row = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], ['a', quantity]]))['records'][0]
    assert row['monthly_units'] is None and row['monthly_status'] == 'INVALID'
    assert ('MONTHLY_QUANTITY_FORMULA' if quantity == '=2+3' else 'MONTHLY_QUANTITY_INVALID') in row['issues']


def test_formula_or_missing_sku_never_becomes_an_identity(tmp_path):
    got = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], ['="a"', 1], [None, 2], [123, 3]]))
    assert len(got['records']) == 3
    assert all(row['sku'] == '' and row['monthly_status'] == 'INVALID' and row['monthly_units'] is None
               for row in got['records'])


def test_special_skus_keep_case_prefix_and_internal_spacing(tmp_path):
    got = read_monthly_workbook(workbook(tmp_path, [['SKU', 'units'], [' a-b-size 5 ', 2],
        ['A-b-size 5', 3], ['a-b-size  5', 4], ['a-b-size 6', 5]]))
    assert [row['sku'] for row in got['records']] == ['a-b-size 5', 'A-b-size 5', 'a-b-size  5', 'a-b-size 6']
    assert got['audit']['sku_whitespace_trimmed_rows'] == [2]


def test_monthly_basis_changes_quantity_and_risk_without_changing_windows():
    original, rec = api(), record(units=120)
    before, rec_before = deepcopy(original), deepcopy(rec)
    row = plan(original, rec)
    assert row['recommended_qty'] == 330  # 120/30 * 110 - 100 - 10
    assert row['daily_sales'] == row['monthly_daily_sales'] == 4
    assert row['coverage_days'] == 25 and row['stockout_risk'] == 'STOCKOUT_BEFORE_REPLENISHMENT'
    assert row['api_30d_recommended_qty'] == 0 and row['api_coverage_days'] == 100
    assert row['status'] == row['parameter_status'] == 'SCENARIO_ONLY'
    assert row['procurement_eligible'] is False
    for days in (7, 30, 60, 90):
        assert row[f'sales_{days}d'] == days and row[f'daily_sales_{days}d'] == 1
    assert original == before and rec == rec_before


def test_reserved_unfulfillable_and_inbound_summary_not_double_counted():
    row = plan(api(fulfillable=5, eligible_inbound=10, inventory_inbound=10), record(units=30))
    assert row['recommended_qty'] == 95  # Only fulfillable and one shipment balance deducted.
    assert row['reserved'] == 999 and row['unfulfillable'] == 888


def test_exact_fraction_rounding_moq_and_pack():
    assert plan(api(fulfillable=0, eligible_inbound=0), record(units=33))['recommended_qty'] == 121
    row = plan(api(fulfillable=0, eligible_inbound=0), record(units=1), moq=10, case_pack=6)
    assert row['recommended_qty'] == 12


def test_valid_monthly_zero_remains_zero_with_no_recent_demand():
    row = plan(rec=record(units=0))
    assert row['recommended_qty'] == 0 and row['daily_sales'] == 0
    assert row['stockout_risk'] == 'NO_RECENT_DEMAND' and row['coverage_days'] is None


def test_unknown_or_other_marketplace_sku_cannot_use_us_inventory():
    rows = calculate_monthly_plan([api(marketplace='CA', api_marketplace='A2EUQ1WTGCTBG2')], [record()])
    row = rows[0]
    assert 'SKU_NOT_FOUND_US' in row['anomaly_codes']
    assert row['recommended_qty'] is None and row['asin'] is None
    assert row['fulfillable'] is None and row['eligible_inbound'] is None
    assert all(row[f'sales_{d}d'] is None for d in (7, 30, 60, 90))


@pytest.mark.parametrize('code', ['ORDERS_PENDING_STATUS', 'INBOUND_REVIEW_REQUIRED',
                                 'INBOUND_QUANTITY_CONFLICT', 'SKU_ASIN_IDENTITY_CONFLICT',
                                 'INBOUND_RECONCILIATION_REQUIRED'])
def test_existing_blocker_cannot_be_cured_by_uploading_monthly_sales(code):
    original = api(status='DATA_REVIEW_REQUIRED', planning_data_complete=False,
                   recommended_qty=None, anomaly_codes=[code],
                   anomalies=[dict(code=code, reason='Existing verified blocker', blocking=True)])
    row = plan(original)
    assert row['recommended_qty'] is None and row['status'] == 'DATA_REVIEW_REQUIRED'
    assert code in row['anomaly_codes'] and row['uploaded_monthly_units'] == 60


@pytest.mark.parametrize('changed', [dict(planning_data_complete=False), dict(inventory_complete=False),
                                   dict(inbound_complete=False), dict(fulfillable=None),
                                   dict(eligible_inbound=None), dict(status='UNKNOWN')])
def test_missing_safe_flags_or_quantities_stay_blocked(changed):
    row = plan(api(**changed))
    assert row['recommended_qty'] is None and row['status'] == 'DATA_REVIEW_REQUIRED'


def test_direct_stop_always_zero_even_with_invalid_monthly_sales():
    row = plan(api(status='STOPPED', stop_reason='Do not buy', planning_data_complete=False),
               record(units=None, status='INVALID'))
    assert row['status'] == 'STOPPED' and row['recommended_qty'] == 0
    assert row['stop_reason'] == 'Do not buy' and row['procurement_eligible'] is False


@pytest.mark.parametrize('status', ['INVALID', 'DUPLICATE_CONFLICT'])
def test_invalid_upload_does_not_fall_back_to_api_demand(status):
    row = plan(rec=record(units=60, status=status))
    assert row['uploaded_monthly_units'] is None
    assert row['daily_sales'] is None and row['recommended_qty'] is None
    assert 'MONTHLY_SALES_INVALID' in row['anomaly_codes']


def test_confirmed_parameters_are_explicit_and_default_is_trial():
    assert plan()['parameter_status'] == 'SCENARIO_ONLY'
    row = plan(parameters_confirmed=True, monthly_period_status='USER_CONFIRMED_PERIOD')
    assert row['parameter_status'] == 'CONFIRMED' and row['procurement_eligible'] is True


def test_assumed_month_remains_trial_even_if_other_parameters_confirmed():
    row = plan(parameters_confirmed=True)
    assert row['parameter_status'] == row['status'] == 'SCENARIO_ONLY'
    assert row['parameters_confirmed'] is False and row['procurement_eligible'] is False
    row = plan(monthly_period_status='USER_CONFIRMED_PERIOD')
    assert row['parameter_status'] == 'SCENARIO_ONLY' and row['procurement_eligible'] is False


def test_31_day_month_comparisons_use_standard_30_day_equivalent():
    row = plan(api(sales_30d=15, sales_7d=14), record(units=31), monthly_days=31)
    assert row['monthly_30d_equivalent'] == 30
    assert row['api_30d_ratio'] == 2
    assert row['api_7d_month_equivalent'] == 60
    assert row['reference_review_codes'] == ['MONTHLY_DIFFERS_FROM_API30', 'RECENT_7D_HIGHER_THAN_MONTHLY']
    assert row['daily_sales'] == 1 and row['recommended_qty'] == 0


def test_inbound_horizon_must_match_before_reusing_eligibility():
    with pytest.raises(ValueError, match='eligibility horizon'):
        plan(api(lead_days=30))
    with pytest.raises(ValueError, match='eligibility horizon'):
        plan(api(lead_days=None))
    assert plan(api(lead_days=30), lead_days=30)['recommended_qty'] == 100


def test_reference_ratios_are_explanations_and_do_not_change_monthly_basis():
    row = plan(rec=record(units=120))
    assert row['api_7d_month_equivalent'] == 30
    assert row['api_90d_month_equivalent'] == 30
    assert row['api_30d_ratio'] == 4
    assert row['reference_review_codes'] == ['MONTHLY_DIFFERS_FROM_API30']
    assert row['recommended_qty'] == 330 and row['daily_sales'] == 4
    assert row['reference_review_threshold_ratio'] == 2


def test_reference_zero_denominator_and_missing_windows_stay_unknown():
    row = plan(api(sales_30d=0, sales_complete_7d=False, sales_complete_90d=False))
    assert row['api_30d_ratio'] is None
    assert row['api_7d_month_equivalent'] is None and row['api_90d_month_equivalent'] is None
    assert 'API30_ZERO_RATIO_UNDEFINED' in row['reference_review_codes']
    assert 'MONTHLY_DIFFERS_FROM_API30' in row['reference_review_codes']


def test_recent_7d_acceleration_is_review_only():
    row = plan(api(sales_7d=70), record(units=30))
    assert row['api_7d_month_equivalent'] == 300
    assert row['reference_review_codes'] == ['RECENT_7D_HIGHER_THAN_MONTHLY']
    assert row['recommended_qty'] == 0 and row['daily_sales'] == 1
    assert row['status'] == 'SCENARIO_ONLY' and row['anomaly_codes'] == []


@pytest.mark.parametrize('options', [dict(monthly_days=0), dict(monthly_days=True),
    dict(monthly_days=math.inf), dict(lead_days=-1), dict(target_days=True),
    dict(safety_days=math.nan), dict(moq=0), dict(case_pack=1.5),
    dict(parameters_confirmed=1), dict(monthly_period_status=''),
    dict(monthly_period_status='UNKNOWN'), dict(monthly_period_status='CONFIRMED')])
def test_invalid_parameters_fail_before_calculation(options):
    with pytest.raises(ValueError):
        plan(**options)


def test_duplicate_unprocessed_input_does_not_produce_two_recommendations():
    with pytest.raises(ValueError, match='deduplicated'):
        calculate_monthly_plan([api()], [record(), record()])
    with pytest.raises(ValueError, match='Duplicate US'):
        calculate_monthly_plan([api(), api()], [record()])
