"""Collect first, validate, then atomically reconcile local snapshots. No API writes."""
from collections import defaultdict
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .core import log_sync, now, record_snapshot, units
from .spapi import SPAPIError, SHIPMENT_STATUSES
from .mock import is_mock_database


def required(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SPAPIError('Missing required field: ' + key)
    return value


def collect_stock_details(client, strict=True):
    """Retain inventory inbound counters without treating unknown values as zero.

    The counters are for reconciliation, not an additional demand deduction.
    Legacy callers may omit these counters; strict acceptance requires all three.
    """
    totals = {}
    identities = set()
    for row in client.inventory_summaries():
        sku = required(row, 'sellerSku')
        identity = (sku, row.get('fnSku'), row.get('condition'))
        if identity in identities:
            raise SPAPIError('Duplicate inventory identity; snapshot rejected')
        identities.add(identity)
        details = row.get('inventoryDetails')
        if not isinstance(details, dict):
            raise SPAPIError('Inventory details missing; snapshot rejected')
        fulfillable = units(details.get('fulfillableQuantity'))
        reserved = details.get('reservedQuantity')
        unfulfillable = details.get('unfulfillableQuantity')
        if not isinstance(reserved, dict) or not isinstance(unfulfillable, dict):
            raise SPAPIError('Inventory quantity details missing; snapshot rejected')
        values = {
            'fulfillable': fulfillable,
            'reserved': units(reserved.get('totalReservedQuantity')),
            'unfulfillable': units(unfulfillable.get('totalUnfulfillableQuantity')),
        }
        for name, field in (
            ('inbound_working', 'inboundWorkingQuantity'),
            ('inbound_shipped', 'inboundShippedQuantity'),
            ('inbound_receiving', 'inboundReceivingQuantity'),
        ):
            value = details.get(field)
            values[name] = units(value) if strict or value is not None else None
        if sku not in totals:
            totals[sku] = values
        else:
            for name, value in values.items():
                previous = totals[sku][name]
                totals[sku][name] = previous + value if previous is not None and value is not None else None
    return totals


def collect_stock(client):
    """Compatible stock snapshot; detailed inbound counters remain available separately."""
    return {sku: [row['fulfillable'], row['reserved'], row['unfulfillable']]
            for sku, row in collect_stock_details(client, strict=False).items()}


def collect_sales(client, skus, asof, timezone_name, days=30):
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        raise ValueError('Sales window must contain 1 to 90 days')
    zone = ZoneInfo(timezone_name)
    start = asof - timedelta(days=days - 1)
    begin = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    finish = datetime.combine(asof + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    if finish > datetime.now(timezone.utc) - timedelta(minutes=2):
        raise ValueError('Sales asof must be a completed day, at least two minutes after its end')
    totals = {(sku, (start + timedelta(days=i)).isoformat()): 0 for sku in skus for i in range(days)}
    lines, seen_orders, seen_lines = [], {}, set()
    for order in client.orders(begin.isoformat(), finish.isoformat()):
        order_id = required(order, 'AmazonOrderId')
        if order_id in seen_orders:
            if order != seen_orders[order_id]:
                raise SPAPIError('Conflicting duplicate order')
            continue
        seen_orders[order_id] = order
        if order.get('MarketplaceId') != client.marketplace or order.get('FulfillmentChannel') != 'AFN':
            raise SPAPIError('Unexpected marketplace or fulfillment channel')
        status = required(order, 'OrderStatus')
        if status == 'Canceled':
            continue
        if status not in ('Unshipped', 'PartiallyShipped', 'Shipped', 'InvoiceUnconfirmed'):
            raise SPAPIError('Unresolved order status; cannot certify complete sales window')
        timestamp = datetime.fromisoformat(required(order, 'PurchaseDate').replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise SPAPIError('PurchaseDate timezone missing')
        day = timestamp.astimezone(zone).date()
        if not start <= day <= asof:
            continue  # CreatedBefore can include the exact end boundary.
        items = client.order_items(order_id)
        if not items:
            raise SPAPIError('Order has no usable items; sales window incomplete')
        for item in items:
            if item.get('CancellationAmbiguous'):
                raise SPAPIError('Ambiguous item cancellation; sales snapshot rejected', 'AMBIGUOUS_ITEM_CANCELLATION')
            item_id, sku = required(item, 'OrderItemId'), required(item, 'SellerSKU')
            identity = (order_id, item_id)
            if identity in seen_lines:
                raise SPAPIError('Duplicate order item; sales window rejected')
            seen_lines.add(identity)
            quantity = units(item.get('QuantityOrdered'))
            original_quantity = units(item.get('QuantityOrderedBeforeCancellation', quantity))
            if item.get('QuantityShipped') is not None and units(item['QuantityShipped']) > original_quantity:
                raise SPAPIError('Shipped quantity exceeds ordered quantity; sales snapshot rejected', 'INVALID_ORDER_QUANTITIES')
            lines.append((order_id, item_id, sku, day.isoformat(), status, quantity))
            key = (sku, day.isoformat())
            totals[key] = totals.get(key, 0) + quantity
    return totals, lines, start


def collect_inbound(client, previous_eta, strict=False):
    results, shipments, seen = [], [], set()
    for shipment in client.inbound_shipments():
        shipment_id = required(shipment, 'ShipmentId')
        status = required(shipment, 'ShipmentStatus')
        if shipment_id in seen or status not in SHIPMENT_STATUSES:
            raise SPAPIError('Duplicate shipment or unknown status')
        seen.add(shipment_id)
        shipments.append((shipment_id, status, shipment.get('DestinationFulfillmentCenterId') or ''))
        per_sku, seen_items = defaultdict(int), {}
        item_count = 0
        for item in client.shipment_items(shipment_id):
            item_count += 1
            if item.get('ShipmentId') != shipment_id:
                raise SPAPIError('Shipment item belongs to a different shipment')
            sku = required(item, 'SellerSKU')
            shipped, received = units(item.get('QuantityShipped')), units(item.get('QuantityReceived'))
            if strict:
                if received > shipped:
                    raise SPAPIError('Inbound received quantity exceeds shipped quantity; snapshot rejected',
                                     'INBOUND_QUANTITY_CONFLICT')
                identity = (sku, item.get('FulfillmentNetworkSKU'))
                quantity = (shipped, received)
                if identity in seen_items:
                    if seen_items[identity] != quantity:
                        raise SPAPIError('Conflicting duplicate shipment item; snapshot rejected',
                                         'INBOUND_DUPLICATE_CONFLICT')
                    continue
                seen_items[identity] = quantity
            per_sku[sku] += max(0, shipped - received)
        if strict and not item_count and status in ('SHIPPED', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN', 'RECEIVING'):
            raise SPAPIError('Active inbound shipment has no usable items; snapshot rejected',
                             'INBOUND_ITEMS_INCOMPLETE')
        for sku, quantity in per_sku.items():
            # ConfirmedNeedByDate is NOT an ETA. Retain only explicitly imported ETAs.
            results.append((sku, shipment_id, quantity, previous_eta.get((sku, shipment_id), ''), status))
    return results, shipments


def sync(con, client, marketplace, kind='all', asof=None, timezone_name='UTC', sales_days=30):
    if kind not in ('all', 'sales', 'stock', 'inbound'):
        raise ValueError('Unknown sync kind')
    zone = ZoneInfo(timezone_name)
    asof = date.fromisoformat(asof) if asof else datetime.now(zone).date() - timedelta(days=1)
    kinds = ['stock', 'sales', 'inbound'] if kind == 'all' else [kind]
    stock = sales = inbound = None
    discovery_inventory = None
    data_source = getattr(client, 'data_source', 'SP-API')
    if (data_source == 'MOCK') != is_mock_database(con):
        raise ValueError('Mock and live sources require separate database modes')
    try:
        binding = con.execute('SELECT * FROM marketplace_binding WHERE marketplace=?', (marketplace,)).fetchone()
        if binding and binding['api_marketplace'] != client.marketplace:
            raise ValueError('Local namespace is bound to a different API marketplace')
        if binding and binding['sales_timezone'] and 'sales' in kinds and binding['sales_timezone'] != timezone_name:
            raise ValueError('Sales timezone changed; use a separate database/namespace to avoid mixing daily buckets')
        if getattr(client, 'supports_discovery', False):
            # One complete current listing of FBA inventory for every real sync.
            # Discovery is committed only after all requested source validation.
            discovery_inventory = client.inventory_summaries()
            from .collection import validate_inventory
            validate_inventory(discovery_inventory)
            if not discovery_inventory or getattr(client, 'collection_complete', {}).get('/fba/inventory/v1/summaries') is False:
                raise SPAPIError('Complete nonempty inventory required for discovery', 'PARTIAL_INVENTORY_COLLECTION')
        if 'stock' in kinds:
            if discovery_inventory is None:
                stock = collect_stock(client)
            else:
                class InventorySnapshot:
                    def inventory_summaries(self):
                        return discovery_inventory
                stock = collect_stock(InventorySnapshot())
        if 'sales' in kinds:
            skus = [r['sku'] for r in con.execute('SELECT sku FROM sku WHERE marketplace=?', (marketplace,))]
            if discovery_inventory is not None:
                skus = sorted(set(skus) | {r['sellerSku'] for r in discovery_inventory})
            if not skus:
                raise ValueError('Import SKU mapping before sales synchronization')
            sales = collect_sales(client, skus, asof, timezone_name, days=sales_days)
        if 'inbound' in kinds:
            previous_eta = {(r['sku'], r['shipment_id']): r['eta'] for r in con.execute('SELECT * FROM inbound WHERE marketplace=?', (marketplace,))}
            inbound = collect_inbound(client, previous_eta, strict=getattr(client, 'supports_discovery', False))
        source_candidates = None
        if discovery_inventory is not None:
            from .discovery import discover, ensure_schema
            ensure_schema(con)
            setting_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_settings'").fetchone()
            if setting_exists:
                source = con.execute('SELECT reference_path FROM discovery_settings WHERE api_marketplace=?',
                                     (client.marketplace,)).fetchone()
                if source and source['reference_path']:
                    from .sku_reference import load_sku_reference
                    source_candidates, _ = load_sku_reference(source['reference_path'])
        timestamp, counts = now(), {}
        with con:
            con.execute('''INSERT INTO marketplace_binding VALUES(?,?,?) ON CONFLICT(marketplace)
                DO UPDATE SET sales_timezone=COALESCE(excluded.sales_timezone,marketplace_binding.sales_timezone)''',
                        (marketplace, client.marketplace, timezone_name if 'sales' in kinds else None))
            if stock is not None:
                con.execute('DELETE FROM stock WHERE marketplace=?', (marketplace,))
                con.executemany('INSERT INTO stock VALUES(?,?,?,?,?,?)',
                                [(marketplace, sku, *quantities, timestamp) for sku, quantities in stock.items()])
                counts['stock'] = len(stock)
            if sales is not None:
                totals, lines, start = sales
                con.execute('DELETE FROM sales WHERE marketplace=? AND day>=? AND day<=?', (marketplace, start.isoformat(), asof.isoformat()))
                con.execute('DELETE FROM order_line WHERE marketplace=? AND day>=? AND day<=?', (marketplace, start.isoformat(), asof.isoformat()))
                con.executemany('INSERT INTO sales VALUES(?,?,?,?)', [(marketplace, sku, day, quantity) for (sku, day), quantity in totals.items()])
                con.executemany('INSERT INTO order_line VALUES(?,?,?,?,?,?,?)', [(marketplace, *line) for line in lines])
                counts['sales'] = len(totals)
                record_snapshot(con, marketplace, 'order_line', data_source)
            if inbound is not None:
                rows, shipments = inbound
                con.execute('DELETE FROM inbound WHERE marketplace=?', (marketplace,))
                con.executemany('INSERT INTO inbound VALUES(?,?,?,?,?,?)', [(marketplace, *row) for row in rows])
                counts['inbound'] = len(rows)
                con.execute('INSERT INTO snapshots(at,marketplace,kind,source,data) VALUES(?,?,?,?,?)',
                            (timestamp, marketplace, 'shipments', data_source, json.dumps(shipments)))
            for source in kinds:
                record_snapshot(con, marketplace, source, data_source)
                log_sync(con, marketplace, source, 'SUCCESS', counts[source],
                         f'{data_source}: Full collection reconciled; API marketplace={client.marketplace}; timezone={timezone_name}; asof={asof}')
            con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                        (timestamp, 'SYNC', marketplace, '', ','.join(kinds)))
            if discovery_inventory is not None:
                discover(con, discovery_inventory, marketplace, client.marketplace,
                         source_candidates=source_candidates, manage_transaction=False,
                         business_day=datetime.now(zone).date().isoformat())
        return counts
    except (SPAPIError, ValueError, KeyError, TypeError):
        # Never persist API bodies or exceptions that might contain customer data.
        with con:
            for source in kinds:
                log_sync(con, marketplace, source, 'FAILED', 0, 'Collection/validation failed; previous data retained')
        raise
