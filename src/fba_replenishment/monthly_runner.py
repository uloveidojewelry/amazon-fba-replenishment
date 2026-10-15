"""Offline monthly-upload report using the existing independent SKU validators."""
from pathlib import Path

from .first_batch import readonly_database
from .sku_plan import calculate_skus
from .sku_runner import apply_latest_source_stops


def _positive(value):
    return type(value) in (int, float) and value > 0


def run_monthly_report(input_path, database_path, output_path, *, source_path=None,
                       asof=None, monthly_days=30,
                       monthly_period_status='ASSUMED_30_DAY_MONTH',
                       monthly_scope_confirmed=False, settings=None,
                       collection_audit=None):
    """Read snapshots only; never import uploaded sales or change human decisions.

    API sales remain comparison evidence. Both their safety gates and current
    direct SKU stops remain effective when monthly demand replaces the baseline.
    """
    from .monthly_plan import read_monthly_workbook, calculate_monthly_plan
    from .monthly_export import export_monthly_report

    output = Path(output_path).resolve()
    protected = {Path(input_path).resolve(), Path(database_path).resolve()}
    if source_path:
        protected.add(Path(source_path).resolve())
    if output in protected:
        raise ValueError('Report must not overwrite an input or configuration database')
    config = dict(settings or {})
    config.setdefault('lead_days', 35)
    config.setdefault('target_days', 75)
    config.setdefault('safety_days', 0)
    config.setdefault('moq', 1)
    config.setdefault('case_pack', 1)
    config.setdefault('max_stock_age_days', 2)
    config.setdefault('inbound_rule', 'confirmed_eta')
    config.setdefault('parameters_confirmed', False)
    if type(config['parameters_confirmed']) is not bool or type(monthly_scope_confirmed) is not bool:
        raise ValueError('Monthly scope and parameter confirmations must be explicit booleans')
    config['parameters_confirmed'] = bool(
        config['parameters_confirmed'] and monthly_scope_confirmed
        and monthly_period_status == 'USER_CONFIRMED_PERIOD')
    parsed = read_monthly_workbook(input_path)
    with readonly_database(database_path) as con:
        binding = con.execute('SELECT api_marketplace,sales_timezone FROM '
                              "marketplace_binding WHERE marketplace='US'").fetchone()
        if not binding or binding['api_marketplace'] != 'ATVPDKIKX0DER':
            raise ValueError('A verified US marketplace binding is required')
        # Use the completed reference date, never pretend that local reference
        # data includes a newer business day merely because the report is new.
        end = asof or con.execute(
            "SELECT MAX(day) FROM sales WHERE marketplace='US'").fetchone()[0]
        if not end:
            raise ValueError('A dated US sales reference is required')
        api_rows = calculate_skus(con, marketplace='US', asof=end, **config)
        if source_path:
            api_rows = apply_latest_source_stops(api_rows, source_path)
        rows = calculate_monthly_plan(
            api_rows, parsed['records'], monthly_days=monthly_days,
            monthly_period_status=monthly_period_status,
            **{key: config[key] for key in ('lead_days', 'target_days', 'safety_days',
                                            'moq', 'case_pack', 'parameters_confirmed')})
        if source_path:
            # This second overlay also covers uploaded SKUs absent from the
            # reference cohort but explicitly stopped in the human source.
            rows = apply_latest_source_stops(rows, source_path)
        uploaded = {row['sku'] for row in rows}
        omitted = []
        for row in api_rows:
            if row['sku'] in uploaded:
                continue
            positive = any(_positive(row.get(key)) for key in (
                'observed_sales_7d', 'observed_sales_30d', 'observed_sales_90d',
                'fulfillable', 'reserved', 'unfulfillable', 'inventory_inbound'))
            if not positive and not row.get('anomaly_codes'):
                continue
            value = dict(row)
            value.update(uploaded_monthly_units=None, monthly_daily_sales=None,
                         monthly_status='NOT_IN_UPLOAD',
                         demand_basis='MONTHLY_SALES_MISSING',
                         recommended_qty=0 if row['status'] == 'STOPPED' else None)
            omitted.append(value)
        export_settings = dict(config, asof=end, monthly_days=monthly_days,
                               monthly_period_status=monthly_period_status,
                               marketplace='US', marketplace_id='ATVPDKIKX0DER',
                               sales_timezone=binding['sales_timezone'],
                               window_start=api_rows[0].get('window_start') if api_rows else None,
                               reference_asof=end,
                               monthly_marketplace_status='USER_CONFIRMED_US' if monthly_scope_confirmed else 'ASSUMED_US_UNCONFIRMED',
                               monthly_units_status='USER_CONFIRMED_UNITS' if monthly_scope_confirmed else 'ASSUMED_UNITS_UNCONFIRMED')
        if isinstance(collection_audit, dict) and isinstance(collection_audit.get('summary'), dict):
            for key in ('inventory_observed_at', 'inbound_observed_at'):
                if isinstance(collection_audit['summary'].get(key), str):
                    export_settings[key] = collection_audit['summary'][key]
        result = export_monthly_report(
            rows, output, con, export_settings, input_audit=parsed['audit'],
            collection_audit=collection_audit, omitted_rows=omitted)
    return result
