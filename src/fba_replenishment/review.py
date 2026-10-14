"""Real inventory review only: candidates never become approved family mappings."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import US_MARKETPLACE
from .core import calculate, inferred_family, log_sync, now, record_snapshot
from .discovery import discover
from .export import export, export_mapping_review
from .mock import is_mock_database
from .spapi import SPAPIError
from .sync import collect_stock_details

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS sku_mapping_review (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, asin TEXT,
 candidate_family TEXT, mapping_basis TEXT NOT NULL,
 review_status TEXT NOT NULL CHECK(review_status='UNAPPROVED'),
 parent_asins TEXT NOT NULL, catalog_status TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(marketplace,sku));
CREATE TABLE IF NOT EXISTS inventory_details (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL,
 inbound_working INTEGER, inbound_shipped INTEGER, inbound_receiving INTEGER,
 observed_at TEXT NOT NULL, PRIMARY KEY(marketplace,sku));
"""


class InventoryClient:
    def __init__(self, rows):
        self.rows = rows

    def inventory_summaries(self):
        return self.rows


def build_review_records(inventory, catalog=None, catalog_status=None, source_candidates=None):
    """Whitelist business fields; missing identities and conflicting ASINs fail closed."""
    details = collect_stock_details(InventoryClient(inventory), strict=True)
    asins = {}
    for row in inventory:
        asin = row.get('asin')
        if not isinstance(asin, str) or not asin.strip():
            raise SPAPIError('Inventory ASIN missing; review rejected', 'MISSING_ASIN')
        asins.setdefault(row['sellerSku'], set()).add(asin)
    result = []
    for sku in sorted(details):
        values = asins[sku]
        asin = next(iter(values)) if len(values) == 1 else None
        name_candidate = inferred_family(sku)
        parents = sorted(set((catalog or {}).get(asin, {}).get('parent_asins', []))) if asin else []
        candidate = None
        basis = 'NO_VERIFIED_RELATIONSHIP_OR_NAMING_RULE'
        if len(values) > 1:
            basis = 'CONFLICTING_SKU_ASIN_IDENTITY'
        elif len(parents) > 1:
            basis = 'MULTIPLE_VARIATION_PARENTS_REQUIRE_REVIEW'
        elif (source_candidates or {}).get(sku, {}).get('conflicting_families'):
            basis = 'USER_WORKBOOK_CONFLICT'
        elif (source_candidates or {}).get(sku, {}).get('candidate_family'):
            candidate = source_candidates[sku]['candidate_family']
            basis = 'USER_WORKBOOK_EXACT_SKU_CANDIDATE'
        elif parents:
            candidate, basis = 'PARENT:' + parents[0], 'AMAZON_CATALOG_VARIATION_PARENT_CANDIDATE'
        elif name_candidate:
            candidate, basis = name_candidate, 'CONSERVATIVE_SKU_NAME_CANDIDATE'
        result.append({'marketplace': 'US', 'sku': sku, 'asin': asin,
                       'candidate_family': candidate, 'mapping_basis': basis,
                       'name_candidate': name_candidate, 'parent_asins': parents,
                       'catalog_status': (catalog_status or {}).get(asin, 'NOT_QUERIED_SAMPLE'),
                       'review_status': 'UNAPPROVED', 'recommended_qty': 0, **details[sku]})
    return result


def run_review(con, client, output_dir, marketplace='US', catalog_sample_size=20,
               sample_summary=None, source_candidates=None, inventory=None, inventory_audit=None,
               catalog_probe=None):
    if client.marketplace != US_MARKETPLACE or marketplace != 'US':
        raise ValueError('This acceptance review is bound to the US marketplace')
    if is_mock_database(con):
        raise ValueError('Real review cannot use a mock database')
    if not 0 <= catalog_sample_size <= 20:
        raise ValueError('Catalog review probe accepts at most 20 ASINs')
    if inventory is not None and (not inventory_audit or inventory_audit.get('complete') is not True
                                 or inventory_audit.get('rows') != len(inventory)):
        raise SPAPIError('External inventory requires a completed validated snapshot', 'PARTIAL_INVENTORY_COLLECTION')
    # Existing formal mappings and stops are preserved by discovery, never inferred.
    raw = inventory if inventory is not None else client.inventory_summaries()
    if getattr(client, 'collection_complete', {}).get('/fba/inventory/v1/summaries') is False:
        raise SPAPIError('Inventory pagination incomplete; previous snapshot retained', 'PARTIAL_INVENTORY_COLLECTION')
    if not raw:
        raise SPAPIError('Full FBA inventory returned no SKU; review cannot certify coverage', 'EMPTY_INVENTORY')
    records = build_review_records(raw, source_candidates=source_candidates)
    catalog, statuses, audits = {}, {}, []
    selected = sorted({r['asin'] for r in records if r['asin']})[:catalog_sample_size]
    if catalog_probe is not None:
        selected = [asin for asin in catalog_probe.get('queried_asins', []) if asin in {r['asin'] for r in records}]
        catalog = {row['asin']: row for row in catalog_probe.get('rows', [])}
        code = catalog_probe.get('status', 'NOT_QUERIED')
        statuses = {asin: ('PASSED' if asin in catalog else 'ASIN_NOT_RETURNED') if code == 'PASSED' else code for asin in selected}
        audits.append({'stage': 'SAMPLE', 'kind': 'catalog_relationships',
                       'status': 'PASSED' if code == 'PASSED' else 'FAILED',
                       'row_count': len(catalog), 'pages': 1,
                       'source': 'Catalog Items 2022-04-01 / relationships',
                       'detail': 'Previously executed real read probe: ' + code})
    elif selected:
        try:
            catalog = {row['asin']: row for row in client.catalog_relationships(selected)}
            for asin in selected:
                statuses[asin] = 'PASSED' if asin in catalog else 'ASIN_NOT_RETURNED'
            audits.append({'stage': 'SAMPLE', 'kind': 'catalog_relationships', 'status': 'PASSED',
                           'row_count': len(catalog), 'pages': 1,
                           'source': 'Catalog Items 2022-04-01 / relationships',
                           'detail': 'One ASIN batch only; candidates remain unapproved'})
        except SPAPIError as error:
            for asin in selected:
                statuses[asin] = error.code
            audits.append({'stage': 'SAMPLE', 'kind': 'catalog_relationships', 'status': 'FAILED',
                           'row_count': 0, 'pages': 1,
                           'source': 'Catalog Items 2022-04-01 / relationships',
                           'detail': error.code})
    records = build_review_records(raw, catalog, statuses, source_candidates)
    timestamp = (inventory_audit or {}).get('observed_at') or now()
    discovery = discover(con, raw, marketplace, client.marketplace,
                         source_candidates=source_candidates, catalog_candidates=catalog,
                         observed_at=timestamp, complete=True,
                         business_day=datetime.now(ZoneInfo((sample_summary or {}).get('timezone') or 'UTC')).date().isoformat())
    discovered = {row['sku']: row for row in discovery['records']}
    con.executescript(REVIEW_SCHEMA)
    with con:
        con.execute('INSERT INTO marketplace_binding VALUES(?,?,?) ON CONFLICT(marketplace) DO NOTHING',
                    (marketplace, client.marketplace, (sample_summary or {}).get('timezone')))
        con.execute('DELETE FROM stock WHERE marketplace=?', (marketplace,))
        con.execute('DELETE FROM inventory_details WHERE marketplace=?', (marketplace,))
        for row in records:
            sku = row['sku']
            con.execute('''INSERT INTO sku(marketplace,sku,family,asin) VALUES(?,?,NULL,?)
                ON CONFLICT(marketplace,sku) DO UPDATE SET asin=excluded.asin''',
                        (marketplace, sku, row['asin']))
            con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)',
                        (marketplace, sku, row['fulfillable'], row['reserved'], row['unfulfillable'], timestamp))
            con.execute('INSERT INTO inventory_details VALUES(?,?,?,?,?,?)',
                        (marketplace, sku, row['inbound_working'], row['inbound_shipped'], row['inbound_receiving'], timestamp))
            approved = discovered[sku]['mapping_status'] == 'APPROVED'
            if approved:
                con.execute('DELETE FROM sku_mapping_review WHERE marketplace=? AND sku=?', (marketplace, sku))
                con.execute("DELETE FROM data_quality_block WHERE marketplace=? AND sku=? AND code='MAPPING_UNAPPROVED'", (marketplace, sku))
            else:
                con.execute('INSERT OR REPLACE INTO sku_mapping_review VALUES(?,?,?,?,?,?,?,?,?)',
                            (marketplace, sku, row['asin'], row['candidate_family'], row['mapping_basis'],
                             'UNAPPROVED', json.dumps(row['parent_asins']), row['catalog_status'], timestamp))
            for code, reason in ([] if approved else [('MAPPING_UNAPPROVED', 'Family mapping requires human approval')]):
                con.execute('INSERT OR REPLACE INTO data_quality_block VALUES(?,?,?,?)',
                            (marketplace, sku, code, reason))
        record_snapshot(con, marketplace, 'stock', 'SP-API FBA Inventory v1 FULL')
        log_sync(con, marketplace, 'stock', 'SUCCESS', len(records), 'Full current US FBA inventory; no history sync')
        # Discovery does not claim success/failure for sources it did not fetch,
        # and never invalidates a previously successful sales/inbound snapshot.
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'REVIEW_GATE', marketplace, '', 'Unapproved mappings and stops; all recommendations zero'))
    candidates = {r['candidate_family'] for r in records if r['candidate_family']}
    sales_sample_skus = (sample_summary or {}).get('sales_sample_skus', [])
    inventory_skus = {r['sku'] for r in records}
    summary = {'collection_status': 'SAMPLE_ONLY', 'orders_collected': (sample_summary or {}).get('orders_collected', 0),
               'sku_count': len(records), 'inventory_coverage': 1.0, 'sales_completeness': None,
               'stopped_family_count': None, 'calculable_sku_count': 0, 'blocked_sku_count': len(records),
               'mapping_candidate_family_count': len(candidates),
               'mapping_unknown_sku_count': sum(not r['candidate_family'] and discovered[r['sku']]['mapping_status'] != 'APPROVED' for r in records),
               'mapping_pending_sku_count': sum(discovered[r['sku']]['mapping_status'] != 'APPROVED' for r in records),
               'catalog_success_count': sum(s == 'PASSED' for s in statuses.values()),
               'catalog_failed_count': sum(s != 'PASSED' for s in statuses.values()),
               'catalog_status': audits[0]['status'] if audits else 'NOT_QUERIED',
               'inbound_v2024_status': (sample_summary or {}).get('inbound_v2024_status', 'NOT_QUERIED'),
               'order_item_count': (sample_summary or {}).get('order_items_collected', 0),
               'sales_sample_skus_in_full_inventory': len(set(sales_sample_skus) & inventory_skus) if isinstance(sales_sample_skus, list) else None,
               'sales_sample_skus_absent_from_full_inventory': len(set(sales_sample_skus) - inventory_skus) if isinstance(sales_sample_skus, list) else None,
               'asof': (sample_summary or {}).get('asof'), 'observed_at': timestamp,
               'stop_list_review_status': 'UNAPPROVED', 'inventory_coverage_denominator': 'All SKU in completed FBA Inventory v1 response'}
    summary.update({'new_sku_count': discovery['daily_new_count'],
                    'latest_run_new_sku_count': discovery['new_count'],
                    'pending_review_count': sum(r['mapping_status'] != 'APPROVED' for r in discovery['records']),
                    'stop_inherited_count': sum(r['replenishment_status'] == 'STOPPED' for r in discovery['records']),
                    'suspected_inactive_count': discovery['missing_count'],
                    'discovery_run_id': discovery['run_id']})
    audits.insert(0, {'stage': 'CURRENT_INVENTORY', 'kind': 'inventory', 'status': 'PASSED',
                      'row_count': len(records), 'source': 'SP-API FBA Inventory v1',
                      'detail': 'All pages consumed; fulfillable, reserved, unfulfillable and inbound quantities validated'})
    acceptance = {'summary': summary, 'audit': audits, 'anomalies': [
        {'sku': r['sku'], 'family': r['candidate_family'], 'code': 'MAPPING_AND_STOP_REVIEW_REQUIRED',
         'severity': 'BLOCKING', 'message': 'Unapproved family/stop policy; recommendation remains zero', 'source': r['mapping_basis']}
        for r in records if discovered[r['sku']]['mapping_status'] != 'APPROVED'], 'per_sku': {r['sku']: {
        'review_status': 'APPROVED' if discovered[r['sku']]['mapping_status'] == 'APPROVED' else 'UNAPPROVED',
        'inventory_complete': True, 'source': 'SP-API FBA Inventory v1; sales SAMPLE_ONLY',
        'inventory_source': 'SP-API FBA Inventory v1 FULL',
        'sales_source': 'SAMPLE_ONLY; 90-day collection paused',
        'inbound_source': 'Inventory summary only; shipment reconciliation not collected',
        'inbound_working': r['inbound_working'], 'inbound_shipped': r['inbound_shipped'],
        'inbound_receiving': r['inbound_receiving'],
        **{f'sales_complete_{days}d': False for days in (7,30,60,90)}} for r in records}}
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    mapping_path, stop_path = dest / 'sku_series_mapping_review.xlsx', dest / 'replenishment_stop_list.xlsx'
    export_mapping_review([row for row in records if discovered[row['sku']]['mapping_status'] != 'APPROVED'],
                          mapping_path, stop_path, summary=acceptance)
    rows = calculate(con, marketplace, asof=summary.get('asof'))
    for row in rows:
        row['recommended_qty'] = 0
        row['eligible_inbound'] = None
        row['unknown_eta_inbound'] = None
    plan_path = dest / 'replenishment_review_blocked.xlsx'
    export(rows, plan_path, con, marketplace, acceptance=acceptance, discovery=discovery['records'])
    with (dest / 'review-summary.json').open('w', encoding='utf-8') as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)
    return {'summary': summary, 'mapping_path': str(mapping_path), 'stop_path': str(stop_path),
            'blocked_plan_path': str(plan_path)}
