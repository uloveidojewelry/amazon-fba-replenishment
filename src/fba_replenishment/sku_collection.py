"""Read-only collection acceptance and local reconciliation for independent SKUs.

Family decisions and stops remain untouched. Pending orders invalidate only the
identified SKU's sales calendar; missing entire lines cannot certify any sales.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

from .collection import (
    CachedClient, CollectionQualityError, PENDING_STATUSES, _boundaries,
    _CheckpointClient, _sanitize_shipment_items, _sanitize_shipments, _timestamp,
    _validate_orders, collect_inbound_snapshot, collect_inventory_snapshot, sanitize_inventory, validate_inventory,
)
from .core import log_sync, now, record_snapshot
from .mock import is_mock_database
from .spapi import SHIPMENT_STATUSES, SPAPIError
from .sync import collect_inbound, collect_stock_details

OWNED_BLOCKS = ('ORDERS_PENDING_STATUS', 'SKU_COLLECTION_ASIN_CONFLICT', 'INBOUND_QUANTITY_CONFLICT')
ACTIVE_SHIPMENTS = {'SHIPPED', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN', 'RECEIVING'}
INVENTORY_DETAILS_SCHEMA = '''CREATE TABLE IF NOT EXISTS inventory_details (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL,
 inbound_working INTEGER, inbound_shipped INTEGER, inbound_receiving INTEGER,
 observed_at TEXT NOT NULL, PRIMARY KEY(marketplace,sku))'''
INBOUND_EXCEPTION_SCHEMA = '''CREATE TABLE IF NOT EXISTS sku_inbound_exception (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, shipment_id TEXT NOT NULL,
 fnsku TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
 quantity_shipped INTEGER NOT NULL CHECK(quantity_shipped >= 0),
 quantity_received INTEGER NOT NULL CHECK(quantity_received >= 0),
 remaining INTEGER CHECK(remaining IS NULL), code TEXT NOT NULL,
 PRIMARY KEY(marketplace,sku,shipment_id,fnsku))'''


def collect_sku_inbound_snapshot(client, cache_dir, force=False, ttl_hours=1,
                                include_terminal_items=False):
    """Keep identifiable quantity conflicts for per-SKU UNKNOWN handling."""
    return collect_inbound_snapshot(client, cache_dir, force=force, ttl_hours=ttl_hours,
                                   include_terminal_items=include_terminal_items,
                                   allow_quantity_exceptions=True)


def collect_sku_inventory_snapshot(client, cache_dir, force=False, ttl_hours=1):
    """Restart only an explicitly expired inventory pagination chain.

Each attempt begins at the first page through the existing atomic collector.
Incomplete page sets never mix, and HTTP 400 without the specific expiration
code is never retried. Three attempts permit at most two full restarts.
"""
    for attempt in range(3):
        try:
            inventory, audit = collect_inventory_snapshot(
                client, cache_dir, force=force, ttl_hours=ttl_hours)
            return inventory, dict(audit, restart_count=attempt)
        except SPAPIError as error:
            if error.code != 'INVENTORY_PAGINATION_EXPIRED':
                raise
            if attempt == 2:
                error.restart_count = attempt
                raise


def _pending_skus(orders, items):
    return sorted({item['SellerSKU'] for order in orders
                   if order['OrderStatus'] in PENDING_STATUSES
                   for item in items[order['AmazonOrderId']]})


def precheck_sku(client, cache_dir, asof, timezone_name):
    """Full inventory then bounded two-day orders and one inbound shipment page.

No candidate/family mapping is consulted. Known pending SKUs and historical
sales SKUs absent from current inventory are soft exceptions. Malformed or
unidentifiable data and permission errors prevent sales expansion.
"""
    inventory, inventory_audit = collect_sku_inventory_snapshot(client, cache_dir)
    _, _, begin, finish = _boundaries(asof, timezone_name, 2)
    report = {
        'mode': 'independent_sku_precheck', 'begin': begin.isoformat(),
        'finish': finish.isoformat(), 'timezone': timezone_name,
        'inventory': inventory_audit, 'orders': 0, 'order_items': 0,
        'orders_complete': False, 'pending_sku_count': 0,
        'order_skus_without_current_inventory_count': 0, 'issues': {},
        'hard_errors': [], 'expansion_allowed': False,
        'complete_snapshot': False, 'inbound_shipments_probed': 0,
        'inbound_items_probed': 0, 'inbound_items_permission': 'NOT_OBSERVED',
    }
    stage = 'orders'
    try:
        raw = client.orders((begin - timedelta(seconds=1)).isoformat(), finish.isoformat(),
                            max_pages=2, max_results=25)
        orders, items, issues, duplicates = _validate_orders(
            client, raw, begin, finish, {row['sellerSku'] for row in inventory})
        pending = _pending_skus(orders, items)
        seen = {item['SellerSKU'] for rows in items.values() for item in rows}
        missing = sorted(seen - {row['sellerSku'] for row in inventory})
        report.update(orders=len(orders), order_items=sum(map(len, items.values())),
                      issues=issues, deduplicated=duplicates,
                      pending_sku_count=len(pending),
                      order_skus_without_current_inventory_count=len(missing),
                      orders_complete=client.collection_complete.get(
                          '/orders/' + client.orders_version + '/orders', False))
        stage = 'inbound'
        # Existing allowlisted pagination implementation, capped at one page.
        raw_shipments = client._pages('/fba/inbound/v0/shipments', {
            'QueryType': 'SHIPMENT', 'ShipmentStatusList': ','.join(SHIPMENT_STATUSES),
            'MarketplaceId': client.marketplace,
        }, 'ShipmentData', query_type=True, max_pages=1)
        shipments = _sanitize_shipments(raw_shipments)
        report['inbound_shipments_probed'] = len(shipments)
        report['inbound_page_complete'] = client.collection_complete.get(
            '/fba/inbound/v0/shipments', False)
        selected = next((row for row in shipments
                         if row['ShipmentStatus'] in ACTIVE_SHIPMENTS), None)
        if selected:
            rows = _sanitize_shipment_items(selected['ShipmentId'],
                                            client.shipment_items(selected['ShipmentId']),
                                            allow_quantity_exceptions=True)
            if not rows:
                raise CollectionQualityError('INBOUND_ITEMS_INCOMPLETE')
            report['inbound_items_probed'] = len(rows)
            report['inbound_items_permission'] = 'PASSED'
            report['inbound_quantity_exception_rows'] = sum(
                row['QuantityReceived'] > row['QuantityShipped'] for row in rows)
        report['expansion_allowed'] = True
    except (SPAPIError, ValueError, KeyError, TypeError) as error:
        report['hard_errors'].append({'stage': stage, 'code': (
            error.code if isinstance(error, SPAPIError) else 'MALFORMED_PRECHECK_DATA')})
    report['pagination'] = list(getattr(client, 'pagination_audit', []))
    return report, inventory, inventory_audit


def _validate_sales_collection(sales):
    if not sales.complete:
        raise CollectionQualityError('INCOMPLETE_SALES_COLLECTION')
    try:
        start, asof = date.fromisoformat(sales.start), date.fromisoformat(sales.asof)
        days = (asof - start).days + 1
        if not 1 <= days <= 90:
            raise ValueError()
        _, _, begin, finish = _boundaries(asof, sales.timezone_name, days)
    except (ValueError, TypeError):
        raise CollectionQualityError('INVALID_SALES_DATE_WINDOW') from None
    # complete=True alone is insufficient: audit proves contiguous finished pages.
    cursor = begin
    audited_orders = audited_items = 0
    try:
        for row in sales.audit:
            if row.get('complete') is not True or _timestamp(row['begin']) != cursor:
                raise ValueError()
            endpoint = _timestamp(row['finish'])
            if not cursor < endpoint <= finish:
                raise ValueError()
            if type(row['orders']) is not int or type(row['items']) is not int:
                raise ValueError()
            if min(row['orders'], row['items']) < 0:
                raise ValueError()
            audited_orders += row['orders']
            audited_items += row['items']
            cursor = endpoint
        if cursor != finish:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise CollectionQualityError('INCOMPLETE_SALES_SHARD_AUDIT') from None
    orders, items, issues, duplicates = _validate_orders(
        _CheckpointClient(sales.marketplace, sales.items), sales.orders, begin, finish)
    if audited_orders != len(orders) or audited_items != sum(map(len, items.values())):
        raise CollectionQualityError('SALES_AUDIT_COUNTS_MISMATCH')
    return start, asof, orders, items, issues, duplicates


def _identity_asins(inventory, orders, items):
    result = defaultdict(set)
    for row in inventory:
        value = row.get('asin')
        if isinstance(value, str) and value.strip():
            result[row['sellerSku']].add(value)
    for order in orders:
        for item in items[order['AmazonOrderId']]:
            value = item.get('ASIN')
            if isinstance(value, str) and value.strip():
                result[item['SellerSKU']].add(value)
    return result


def reconcile_sku_data(con, inventory, sales, shipments, shipment_items,
                       marketplace='US', inventory_audit=None):
    """Atomically retain complete SKU evidence without approving any mappings.

The SalesCollection must attest contiguous complete date shards. All known
SKUs receive zero days only after that attestation; a SKU with any unresolved
order receives no certified daily rows. Observed non-canceled lines remain
available for audit. Manual ASIN/family/enabled/stop decisions are preserved.
"""
    if is_mock_database(con):
        raise ValueError('Real collection requires a non-mock database')
    if con.in_transaction:
        raise ValueError('Reconciliation requires a completed caller transaction')
    inventory = sanitize_inventory(inventory)
    inventory_quality = validate_inventory(inventory)
    if not inventory:
        raise CollectionQualityError('EMPTY_INVENTORY_REQUIRES_REVIEW')
    if (not inventory_audit or inventory_audit.get('complete') is not True
            or inventory_audit.get('rows') != len(inventory)
            or inventory_audit.get('skus') != inventory_quality['skus']):
        raise CollectionQualityError('INCOMPLETE_INVENTORY_ATTESTATION')
    observed_at = _timestamp(inventory_audit.get('observed_at')).astimezone(timezone.utc).isoformat()
    start, asof, orders, items, issues, duplicates = _validate_sales_collection(sales)
    binding = con.execute('SELECT * FROM marketplace_binding WHERE marketplace=?',
                          (marketplace,)).fetchone()
    if binding and binding['api_marketplace'] != sales.marketplace:
        raise ValueError('Namespace belongs to another API marketplace')
    if binding and binding['sales_timezone'] and binding['sales_timezone'] != sales.timezone_name:
        raise ValueError('Sales timezone differs from the existing namespace')
    shipments = _sanitize_shipments(shipments)
    if (not isinstance(shipment_items, dict)
            or set(shipment_items) != {row['ShipmentId'] for row in shipments}
            or any(not isinstance(values, list) for values in shipment_items.values())):
        raise CollectionQualityError('INCOMPLETE_SHIPMENT_ITEMS')
    shipment_items = {identity: _sanitize_shipment_items(identity, values, allow_quantity_exceptions=True)
                      for identity, values in shipment_items.items()}
    inbound_exceptions = []
    for shipment in shipments:
        identity = shipment['ShipmentId']
        if not shipment_items[identity] and shipment['ShipmentStatus'] in ACTIVE_SHIPMENTS:
            raise CollectionQualityError('INBOUND_ITEMS_INCOMPLETE')
        for item in shipment_items[identity]:
            if item['QuantityReceived'] > item['QuantityShipped']:
                fnsku = item.get('FulfillmentNetworkSKU')
                inbound_exceptions.append((marketplace, item['SellerSKU'], identity,
                    fnsku if isinstance(fnsku, str) else '', shipment['ShipmentStatus'],
                    item['QuantityShipped'], item['QuantityReceived'], None, 'INBOUND_QUANTITY_CONFLICT'))
    conflict_skus = {row[1] for row in inbound_exceptions}
    # Exclude *every* shipment balance for a conflicted SKU, rather than
    # allowing one good shipment to hide another unknown quantity.
    certified_shipment_items = {identity: [item for item in values if item['SellerSKU'] not in conflict_skus]
                                for identity, values in shipment_items.items()}
    cached = CachedClient(sales.marketplace, sales, inventory, shipments, shipment_items)
    stock = collect_stock_details(cached, strict=True)
    old_eta = {(row['sku'], row['shipment_id']): row['eta'] for row in con.execute(
        'SELECT sku,shipment_id,eta FROM inbound WHERE marketplace=?', (marketplace,))}
    inbound_client = CachedClient(sales.marketplace, sales, inventory, shipments, certified_shipment_items)
    # Every raw active shipment has already proved it has usable lines. It may
    # now have only conflict SKUs, so omit that shipment from the strict balance
    # adapter while retaining its identity and status in the shipment snapshot.
    inbound_client.shipments = [row for row in inbound_client.shipments
                               if certified_shipment_items[row['ShipmentId']]
                               or row['ShipmentStatus'] not in ACTIVE_SHIPMENTS]
    inbound, _ = collect_inbound(inbound_client, old_eta, strict=True)
    shipment_rows = [(row['ShipmentId'], row['ShipmentStatus'], row.get('DestinationFulfillmentCenterId') or '')
                     for row in shipments]
    known_skus = {row['sku'] for row in con.execute('SELECT sku FROM sku WHERE marketplace=?', (marketplace,))}
    known_skus.update(row['sku'] for row in con.execute(
        'SELECT DISTINCT sku FROM order_line WHERE marketplace=?', (marketplace,)))
    known_skus.update(stock)
    known_skus.update(row[0] for row in inbound)
    known_skus.update(item['SellerSKU'] for values in shipment_items.values() for item in values)
    pending_skus = set(_pending_skus(orders, items))
    asins = _identity_asins(inventory, orders, items)
    observed_daily = Counter()
    lines = []
    zone = ZoneInfo(sales.timezone_name)
    for order in orders:
        if order['OrderStatus'] == 'Canceled':
            continue
        day = _timestamp(order['PurchaseDate']).astimezone(zone).date().isoformat()
        for item in items[order['AmazonOrderId']]:
            sku, quantity = item['SellerSKU'], item['QuantityOrdered']
            known_skus.add(sku)
            lines.append((marketplace, order['AmazonOrderId'], item['OrderItemId'],
                          sku, day, order['OrderStatus'], quantity))
            if order['OrderStatus'] not in PENDING_STATUSES:
                observed_daily[sku, day] += quantity
    days = (asof - start).days + 1
    complete_skus = sorted(known_skus - pending_skus)
    asin_conflicts = set()
    timestamp = now()
    summary = {
        'mode': 'independent_sku_read_only', 'api_marketplace': sales.marketplace,
        'marketplace': marketplace, 'timezone': sales.timezone_name,
        'start': start.isoformat(), 'asof': asof.isoformat(), 'days': days,
        'sku_count': len(known_skus), 'inventory_sku_count': len(stock),
        'orders': len(orders), 'order_items': len(lines),
        'pending_sku_count': len(pending_skus), 'certified_sales_sku_count': len(complete_skus),
        'sales_daily_rows': len(complete_skus) * days,
        'sales_complete_rate': len(complete_skus) / len(known_skus) if known_skus else 0.0,
        'inbound_rows': len(inbound), 'shipment_count': len(shipments),
        'inbound_quantity_exception_rows': len(inbound_exceptions),
        'inbound_quantity_conflict_sku_count': len(conflict_skus),
        'stock_missing_sku_count': len(known_skus - set(stock)),
        'deduplicated': duplicates, 'issues': issues,
        'family_mappings_changed': 0, 'stop_decisions_changed': 0,
    }
    try:
        with con:
            con.execute(INVENTORY_DETAILS_SCHEMA)
            con.execute(INBOUND_EXCEPTION_SCHEMA)
            con.execute('''INSERT INTO marketplace_binding VALUES(?,?,?)
                ON CONFLICT(marketplace) DO UPDATE SET sales_timezone=excluded.sales_timezone''',
                        (marketplace, sales.marketplace, sales.timezone_name))
            for sku in sorted(known_skus):
                previous = con.execute('SELECT asin FROM sku WHERE marketplace=? AND sku=?',
                                       (marketplace, sku)).fetchone()
                values = asins[sku]
                if len(values) > 1 or (previous and previous['asin'] and values
                                      and values != {previous['asin']}):
                    asin_conflicts.add(sku)
                asin = next(iter(values)) if len(values) == 1 else None
                con.execute('''INSERT INTO sku(marketplace,sku,family,asin) VALUES(?,?,NULL,?)
                    ON CONFLICT(marketplace,sku) DO UPDATE SET
                    asin=CASE WHEN sku.asin IS NULL OR sku.asin='' THEN excluded.asin ELSE sku.asin END''',
                            (marketplace, sku, asin))
            con.execute('DELETE FROM stock WHERE marketplace=?', (marketplace,))
            con.execute('DELETE FROM inventory_details WHERE marketplace=?', (marketplace,))
            con.executemany('INSERT INTO stock VALUES(?,?,?,?,?,?)',
                            ((marketplace, sku, row['fulfillable'], row['reserved'],
                              row['unfulfillable'], observed_at) for sku, row in stock.items()))
            con.executemany('INSERT INTO inventory_details VALUES(?,?,?,?,?,?)',
                            ((marketplace, sku, row['inbound_working'], row['inbound_shipped'],
                              row['inbound_receiving'], observed_at) for sku, row in stock.items()))
            con.execute('DELETE FROM sales WHERE marketplace=? AND day>=? AND day<=?',
                        (marketplace, start.isoformat(), asof.isoformat()))
            con.execute('DELETE FROM order_line WHERE marketplace=? AND day>=? AND day<=?',
                        (marketplace, start.isoformat(), asof.isoformat()))
            con.executemany('INSERT INTO order_line VALUES(?,?,?,?,?,?,?)', lines)
            con.executemany('INSERT INTO sales VALUES(?,?,?,?)',
                            ((marketplace, sku, (start + timedelta(days=offset)).isoformat(),
                              observed_daily[sku, (start + timedelta(days=offset)).isoformat()])
                             for sku in complete_skus for offset in range(days)))
            con.execute('DELETE FROM inbound WHERE marketplace=?', (marketplace,))
            con.executemany('INSERT INTO inbound VALUES(?,?,?,?,?,?)',
                            ((marketplace, *row) for row in inbound))
            con.execute('DELETE FROM sku_inbound_exception WHERE marketplace=?', (marketplace,))
            con.executemany('INSERT INTO sku_inbound_exception VALUES(?,?,?,?,?,?,?,?,?)', inbound_exceptions)
            # Only collection-owned blocks may be cleared by fresh evidence.
            con.execute('DELETE FROM data_quality_block WHERE marketplace=? AND code IN (?,?,?)',
                        (marketplace, *OWNED_BLOCKS))
            con.executemany('INSERT INTO data_quality_block VALUES(?,?,?,?)',
                            ((marketplace, sku, 'ORDERS_PENDING_STATUS',
                              'Unresolved order: certified sales calendar unavailable')
                             for sku in sorted(pending_skus)))
            con.executemany('INSERT INTO data_quality_block VALUES(?,?,?,?)',
                            ((marketplace, sku, 'SKU_COLLECTION_ASIN_CONFLICT',
                              'SKU evidence conflicts with current or approved ASIN identity')
                             for sku in sorted(asin_conflicts)))
            con.executemany('INSERT INTO data_quality_block VALUES(?,?,?,?)',
                            ((marketplace, sku, 'INBOUND_QUANTITY_CONFLICT',
                              'Received quantity exceeds shipped; all shipment balances for this SKU are UNKNOWN')
                             for sku in sorted(conflict_skus)))
            summary['asin_conflict_sku_count'] = len(asin_conflicts)
            for kind in ('stock', 'inventory_details', 'order_line', 'inbound', 'sku_inbound_exception'):
                record_snapshot(con, marketplace, kind, 'SP-API')
            for kind, value in (('shipments', shipment_rows), ('sku_sales_completeness', summary)):
                con.execute('INSERT INTO snapshots(at,marketplace,kind,source,data) VALUES(?,?,?,?,?)',
                            (timestamp, marketplace, kind, 'SP-API', json.dumps(value)))
            log_sync(con, marketplace, 'stock', 'SUCCESS', len(stock),
                     'Complete independent SKU inventory; no mapping or stop changes')
            log_sync(con, marketplace, 'sales', 'SUCCESS_WITH_SKU_EXCEPTIONS' if pending_skus else 'SUCCESS',
                     summary['sales_daily_rows'],
                     'Complete order pages; unresolved SKU calendars excluded: ' + str(len(pending_skus)))
            log_sync(con, marketplace, 'inbound', 'SUCCESS_WITH_SKU_EXCEPTIONS' if conflict_skus else 'SUCCESS',
                     len(inbound), 'Complete shipment pages; UNKNOWN quantity SKU count: ' + str(len(conflict_skus)))
            con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                        (timestamp, 'SKU_INDEPENDENT_SYNC', marketplace, '', json.dumps(summary)))
    except sqlite3.Error:
        # Snapshot rollback precedes this token-free failure audit.
        try:
            with con:
                log_sync(con, marketplace, 'sales', 'FAILED', 0,
                         'Local SKU reconciliation failed; previous snapshots retained')
        except sqlite3.Error:
            # Even a failing audit writer must not expose a database error body.
            pass
        raise CollectionQualityError('LOCAL_RECONCILIATION_FAILED') from None
    return summary
