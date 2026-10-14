"""Read-only, independent Seller SKU planning with certified calendar windows.

This opt-in mode leaves family approval and stop configuration untouched. Only
direct SKU stops apply. Inventory summaries reconcile shipment balances; they
are never added to those balances or to fulfillable stock.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

from .core import ACTIVE_INBOUND
from .export import WINDOWS, _quantity
from .spapi import SHIPMENT_STATUSES


IGNORED_FAMILY_BLOCKS = frozenset({
    'MAPPING_UNAPPROVED', 'DISCOVERY_MAPPING_UNAPPROVED',
    'DISCOVERY_FAMILY_CONFLICT', 'DISCOVERY_FAMILY_STOPPED', 'SCHEDULED_STOP_SCOPE_CONFLICT',
})
COMPLETE_SALES_COLLECTION = frozenset({'SUCCESS', 'SUCCESS_WITH_SKU_EXCEPTIONS'})


def _table_names(con):
    return {r['name'] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _site_rows(con, tables, name, marketplace):
    return [dict(r) for r in con.execute('SELECT * FROM ' + name + ' WHERE marketplace=?',
                                       (marketplace,))] if name in tables else []


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _windows(con, marketplace, asof):
    """One grouped query instead of 15,932 individual 90-day reads."""
    columns = ['sku']
    for days in WINDOWS:
        start = (asof - timedelta(days=days - 1)).isoformat()
        valid = f"day>='{start}' AND typeof(units)='integer' AND units>=0"
        columns.extend((f'SUM(CASE WHEN {valid} THEN 1 ELSE 0 END) days_{days}',
                        f'SUM(CASE WHEN {valid} THEN units ELSE 0 END) units_{days}'))
    calendar = [(asof - timedelta(days=i)).isoformat() for i in range(90)]
    sql = ('SELECT ' + ','.join(columns) + ' FROM sales WHERE marketplace=? AND day IN ('
           + ','.join('?' for _ in calendar) + ') GROUP BY sku')
    return {r['sku']: dict(r) for r in con.execute(sql, (marketplace, *calendar))}


def _settings(lead_days, target_days, safety_days, moq, case_pack, max_stock_age_days,
              inbound_rule, parameters_confirmed):
    for value in (lead_days, target_days, safety_days, max_stock_age_days):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError('Planning days must be finite and nonnegative')
    if any(type(value) is not int or value < 1 for value in (moq, case_pack)):
        raise ValueError('MOQ and case pack must be positive integers')
    if inbound_rule not in ('confirmed_eta', 'exclude'):
        raise ValueError('Inbound rule must be confirmed_eta or exclude')
    if type(parameters_confirmed) is not bool:
        raise ValueError('parameters_confirmed must be an explicit boolean')


def calculate_skus(con, marketplace='US', asof=None, lead_days=35, target_days=75,
                   safety_days=0, moq=1, case_pack=1, max_stock_age_days=2,
                   inbound_rule='confirmed_eta', parameters_confirmed=False):
    """Calculate isolated SKU scenarios without altering configuration or data.

    A full 90-day calendar and a successful sales collection are necessary to
    certify demand, including zero demand. Default settings are visibly marked
    SCENARIO_ONLY until explicitly confirmed. Unknown recommendations are None;
    an effective direct SKU stop always produces the deliberate quantity zero.
    """
    _settings(lead_days, target_days, safety_days, moq, case_pack, max_stock_age_days,
              inbound_rule, parameters_confirmed)
    tables = _table_names(con)
    binding = con.execute('SELECT * FROM marketplace_binding WHERE marketplace=?',
                          (marketplace,)).fetchone()
    sales_timezone = (binding['sales_timezone'] if binding else None) or 'UTC'
    zone, instant = ZoneInfo(sales_timezone), datetime.now(timezone.utc)
    end = date.fromisoformat(asof) if isinstance(asof, str) else asof
    end = end or (instant.astimezone(zone).date() - timedelta(days=1))
    if not isinstance(end, date) or isinstance(end, datetime):
        raise ValueError('asof must be an ISO completed calendar date')
    if end >= instant.astimezone(zone).date():
        raise ValueError('asof must be a completed day')
    due = end + timedelta(days=lead_days)
    masters = {r['sku']: r for r in _site_rows(con, tables, 'sku', marketplace)}
    discovered = {r['sku']: r for r in _site_rows(con, tables, 'discovery_sku', marketplace)}
    stocks = {r['sku']: r for r in _site_rows(con, tables, 'stock', marketplace)}
    details = {r['sku']: r for r in _site_rows(con, tables, 'inventory_details', marketplace)}
    sales = _windows(con, marketplace, end)
    incoming, quality = defaultdict(list), defaultdict(list)
    for row in _site_rows(con, tables, 'inbound', marketplace):
        incoming[row['sku']].append(row)
    for row in _site_rows(con, tables, 'data_quality_block', marketplace):
        quality[row['sku']].append(row)
    stops = {r['sku']: r['reason'] for r in _site_rows(con, tables, 'stop_flag', marketplace)
             if r['active'] == 1}
    # Observe due approved direct stops without applying their family side effect
    # or mutating their persisted status. Explicit resumes deactivate stop_flag;
    # historical ACTIVE scheduled rows must not silently reinstate resumed SKUs.
    for row in _site_rows(con, tables, 'batch_scheduled_stop', marketplace):
        if row['status'] not in ('PENDING', 'CONFLICT_PENDING'):
            continue
        try:
            due_stop = date.fromisoformat(row['effective_date']) <= instant.astimezone(
                ZoneInfo(row['effective_timezone'])).date()
        except (ValueError, TypeError):
            due_stop = True  # A damaged approved direct stop cannot permit buying.
        if due_stop:
            stops.setdefault(row['sku'], row['reason'])
    states = {}
    for kind in ('stock', 'sales', 'inbound'):
        last = con.execute('SELECT status FROM sync_log WHERE marketplace=? AND kind=? '
                           'ORDER BY id DESC LIMIT 1', (marketplace, kind)).fetchone()
        states[kind] = last['status'] if last else None
    identities = set(masters) | set(discovered) | set(stocks) | set(sales) | set(incoming) | set(stops)
    result = []
    for sku in sorted(identities):
        master, discovery = masters.get(sku, {}), discovered.get(sku, {})
        stock, detail, totals = stocks.get(sku), details.get(sku), sales.get(sku, {})
        anomalies = []

        def issue(code, reason, blocking=True, source='Planning validation'):
            if not any(a['code'] == code for a in anomalies):
                anomalies.append({'code': code, 'reason': reason, 'blocking': blocking, 'source': source})

        relevant_quality = [r for r in quality.get(sku, []) if r['code'] not in IGNORED_FAMILY_BLOCKS]
        for block in relevant_quality:
            issue(block['code'], block['reason'], source='Persisted data quality block')
        if (master.get('asin') and discovery.get('asin')
                and master['asin'] != discovery['asin']):
            issue('SKU_ASIN_IDENTITY_CONFLICT', 'Stored SKU ASIN identities disagree')
        if discovery.get('inventory_identities') is not None:
            try:
                inventory_identities = json.loads(discovery['inventory_identities'])
                if (not isinstance(inventory_identities, list)
                        or any(not isinstance(identity, dict) or not identity.get('asin')
                               for identity in inventory_identities)):
                    raise ValueError()
                identity_asins = {identity['asin'] for identity in inventory_identities}
                if len(identity_asins) > 1:
                    issue('SKU_ASIN_IDENTITY_CONFLICT', 'One Seller SKU has conflicting inventory ASINs')
            except (ValueError, TypeError, KeyError):
                issue('SKU_IDENTITY_UNKNOWN', 'Stored inventory SKU identity evidence is invalid')
        for kind, state in states.items():
            successful = state in COMPLETE_SALES_COLLECTION if kind == 'sales' else state == 'SUCCESS'
            if not successful:
                issue('SYNC_FAILED' if state == 'FAILED' else 'INCOMPLETE_' + kind.upper(),
                      'Latest ' + kind + ' collection is not verified successful')

        row = {'marketplace': marketplace, 'sku': sku,
               'api_marketplace': binding['api_marketplace'] if binding else None,
               'asin': discovery.get('asin') or master.get('asin') or None,
               'family': master.get('family') or '', 'mode': 'INDEPENDENT_SKU_READ_ONLY',
               'asof': end.isoformat(), 'window_start': (end - timedelta(days=89)).isoformat(),
               'lead_days': lead_days, 'target_days': target_days, 'safety_days': safety_days,
               'moq': moq, 'case_pack': case_pack, 'max_stock_age_days': max_stock_age_days,
               'inbound_rule': inbound_rule, 'sales_timezone': sales_timezone,
               'parameters_confirmed': parameters_confirmed,
               'parameter_status': 'CONFIRMED' if parameters_confirmed else 'SCENARIO_ONLY',
               'sales_source': 'SP-API order daily calendar / latest verified sync',
               'inventory_source': 'FBA Inventory summaries; fulfillable only is available',
               'inbound_source': 'Shipment remaining quantity, counted once; manual ETA only'}
        sales_quality = any(any(word in q['code'] for word in ('SALES', 'ORDER', 'CANCELLATION'))
                            for q in relevant_quality)
        for days in WINDOWS:
            count, observed = totals.get(f'days_{days}', 0), totals.get(f'units_{days}', 0)
            complete = count == days and states['sales'] in COMPLETE_SALES_COLLECTION and not sales_quality
            row[f'observed_sales_days_{days}d'] = count
            row[f'observed_sales_{days}d'] = observed if count else None
            row[f'sales_complete_{days}d'] = complete
            row[f'sales_completeness_{days}d'] = count / days
            row[f'sales_{days}d'] = observed if complete else None
            row[f'daily_sales_{days}d'] = round(observed / days, 6) if complete else None
        if not row['sales_complete_90d']:
            issue('INCOMPLETE_SALES', f"Only {row['observed_sales_days_90d']}/90 verified daily totals; missing dates are unknown")
        row['sales_status'] = ('SOLD' if row['sales_90d'] and row['sales_90d'] > 0
                               else 'NO_SALES' if row['sales_complete_90d'] else 'UNKNOWN')
        row['has_observed_sales'] = bool(row['observed_sales_90d'] is not None and row['observed_sales_90d'] > 0)
        row['daily_sales'] = row['daily_sales_30d']
        row['observed_sales_days'] = row['observed_sales_days_30d']
        inventory_known = stock is not None and all(_quantity(stock.get(k))
                                                   for k in ('fulfillable', 'reserved', 'unfulfillable'))
        for key in ('fulfillable', 'reserved', 'unfulfillable'):
            row[key] = stock.get(key) if stock and _quantity(stock.get(key)) else None
        for key in ('inbound_working', 'inbound_shipped', 'inbound_receiving'):
            row[key] = detail.get(key) if detail and _quantity(detail.get(key)) else None
        inbound_summary_known = all(row[key] is not None for key in
                                    ('inbound_working', 'inbound_shipped', 'inbound_receiving'))
        row['inventory_inbound'] = sum(row[k] for k in ('inbound_working', 'inbound_shipped', 'inbound_receiving')) if inbound_summary_known else None
        observed, detailed_observed = _stamp(stock.get('observed_at')) if stock else None, _stamp(detail.get('observed_at')) if detail else None
        row['stock_observed_at'] = stock.get('observed_at') if stock else None
        row['stock_age_days'] = max(0, (max(end, instant.date()) - observed.date()).days) if observed else None
        if not inventory_known or not inbound_summary_known:
            issue('MISSING_STOCK' if stock is None else 'INCOMPLETE_INVENTORY', 'All six FBA quantity categories must be known')
        stock_fresh = observed is not None and row['stock_age_days'] <= max_stock_age_days
        if not stock_fresh or detailed_observed is None:
            issue('STALE_STOCK' if observed else 'INVALID_STOCK_TIMESTAMP', 'Inventory observation is missing or exceeds its freshness limit')
        elif (abs((observed - detailed_observed).total_seconds()) > 60
              or instant - detailed_observed > timedelta(days=max_stock_age_days + 1)):
            stock_fresh = False
            issue('INVENTORY_SNAPSHOT_MISMATCH', 'Stock and inbound counters are not from the same inventory snapshot')
        if observed and observed > instant + timedelta(minutes=2):
            stock_fresh = False
            issue('FUTURE_STOCK_TIMESTAMP', 'Inventory timestamp is in the future')
        row['inventory_complete'] = bool(inventory_known and inbound_summary_known and stock_fresh
                                         and detailed_observed is not None and states['stock'] == 'SUCCESS')
        if any(any(word in q['code'] for word in ('STOCK', 'INVENTORY')) for q in relevant_quality):
            row['inventory_complete'] = False
        active_units, working_units, eligible, unknown_eta, eta_after_horizon = 0, 0, 0, 0, 0
        inbound_valid = states['inbound'] == 'SUCCESS' and not any(
            any(word in q['code'] for word in ('INBOUND', 'SHIPMENT')) for q in relevant_quality)
        for shipment in incoming.get(sku, []):
            quantity, shipment_status = shipment.get('units'), shipment.get('status')
            if not _quantity(quantity) or shipment_status not in SHIPMENT_STATUSES:
                inbound_valid = False
                issue('INBOUND_STATE_UNKNOWN', 'Inbound quantity or shipment status is invalid or unknown')
                continue
            if shipment_status == 'ERROR' and quantity > 0:
                inbound_valid = False
                issue('INBOUND_STATE_UNKNOWN', 'Outstanding inbound shipment is in ERROR status')
            elif shipment_status == 'WORKING':
                working_units += quantity
            elif shipment_status in ACTIVE_INBOUND:
                active_units += quantity
                if not quantity:
                    continue
                try:
                    eta = date.fromisoformat(shipment['eta']) if shipment.get('eta') else None
                except (TypeError, ValueError):
                    eta = None
                if eta is None:
                    unknown_eta += quantity
                elif eta < instant.astimezone(zone).date():
                    # An overdue ETA without confirmation of arrival is not
                    # reliable availability for a new procurement scenario.
                    unknown_eta += quantity
                    issue('INBOUND_ETA_OVERDUE', 'Active inbound ETA has passed; arrival is unconfirmed', inbound_rule != 'exclude')
                elif eta <= due:
                    eligible += quantity
                else:
                    eta_after_horizon += quantity
        if inbound_summary_known and (row['inbound_shipped'] + row['inbound_receiving'] != active_units
                                      or row['inbound_working'] != working_units):
            inbound_valid = False
            issue('INBOUND_RECONCILIATION_REQUIRED', 'Inventory inbound counters disagree with shipment remaining quantities')
        if unknown_eta:
            issue('INBOUND_REVIEW_REQUIRED', 'Active inbound has unknown, invalid or overdue ETA', inbound_rule != 'exclude')
        row.update({'inbound_complete': bool(inbound_valid and inbound_summary_known),
                    'inbound_eta_complete': bool(inbound_valid and unknown_eta == 0),
                    'shipment_active_inbound': active_units if inbound_valid else None,
                    'shipment_working_inbound': working_units if inbound_valid else None,
                    'eligible_inbound': (eligible if inbound_rule == 'confirmed_eta' else 0) if inbound_valid else None,
                    'unknown_eta_inbound': unknown_eta if inbound_valid else None,
                    'inbound_after_horizon': eta_after_horizon if inbound_valid else None})
        direct_reason = stops.get(sku)
        if master and master.get('enabled') == 0:
            direct_reason = direct_reason or 'SKU is explicitly disabled'
        blockers = [a for a in anomalies if a['blocking']]
        row['recommended_qty'] = None
        if direct_reason is not None:
            row['status'], row['recommended_qty'], row['stop_reason'] = 'STOPPED', 0, direct_reason
        elif blockers:
            row['status'], row['stop_reason'] = 'DATA_REVIEW_REQUIRED', '; '.join(a['code'] + ': ' + a['reason'] for a in blockers)
        else:
            daily = row['sales_30d'] / 30
            raw = max(0, math.ceil(daily * (lead_days + target_days + safety_days)
                                  - row['fulfillable'] - row['eligible_inbound']))
            row['recommended_qty'] = math.ceil(max(raw, moq) / case_pack) * case_pack if raw else 0
            row['status'] = 'ACTIVE' if parameters_confirmed else 'SCENARIO_ONLY'
            row['stop_reason'] = ''
        # Keep observed stockout risk visible for manually stopped SKUs, too.
        daily = row['sales_30d'] / 30 if row['sales_complete_30d'] else None
        row['coverage_days'] = round(row['fulfillable'] / daily, 2) if row['inventory_complete'] and daily is not None and daily > 0 else None
        if not row['inventory_complete'] or daily is None:
            row['stockout_risk'] = 'UNKNOWN_DATA'
        elif daily == 0:
            row['stockout_risk'] = 'NO_RECENT_DEMAND'
        elif row['coverage_days'] < lead_days:
            row['stockout_risk'] = 'STOCKOUT_BEFORE_REPLENISHMENT'
        elif row['coverage_days'] < lead_days + safety_days:
            row['stockout_risk'] = 'SAFETY_STOCK_RISK'
        else:
            row['stockout_risk'] = 'COVERED'
        row['replenishment_status'] = row['status']
        row['anomalies'], row['anomaly_codes'] = anomalies, [a['code'] for a in anomalies]
        row['data_complete'] = bool(row['sales_complete_90d'] and row['inventory_complete']
                                    and row['inbound_complete'] and not relevant_quality
                                    and 'SKU_ASIN_IDENTITY_CONFLICT' not in row['anomaly_codes']
                                    and 'SKU_IDENTITY_UNKNOWN' not in row['anomaly_codes'])
        row['planning_data_complete'] = not blockers
        row['procurement_eligible'] = bool(parameters_confirmed and row['status'] == 'ACTIVE')
        result.append(row)
    return result
