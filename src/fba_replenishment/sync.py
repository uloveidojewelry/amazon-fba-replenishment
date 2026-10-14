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


def collect_stock(client):
    totals = defaultdict(lambda: [0, 0, 0])
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
        values = [units(details.get('fulfillableQuantity')),
                  units((details.get('reservedQuantity') or {}).get('totalReservedQuantity')),
                  units((details.get('unfulfillableQuantity') or {}).get('totalUnfulfillableQuantity'))]
        for index, value in enumerate(values):
            totals[sku][index] += value
    return totals


def collect_sales(client, skus, asof, timezone_name):
    zone = ZoneInfo(timezone_name)
    start = asof - timedelta(days=29)
    begin = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    finish = datetime.combine(asof + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    if finish > datetime.now(timezone.utc) - timedelta(minutes=2):
        raise ValueError('Sales asof must be a completed day, at least two minutes after its end')
    totals = {(sku, (start + timedelta(days=i)).isoformat()): 0 for sku in skus for i in range(30)}
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
            item_id, sku = required(item, 'OrderItemId'), required(item, 'SellerSKU')
            identity = (order_id, item_id)
            if identity in seen_lines:
                raise SPAPIError('Duplicate order item; sales window rejected')
            seen_lines.add(identity)
            quantity = units(item.get('QuantityOrdered'))
            lines.append((order_id, item_id, sku, day.isoformat(), status, quantity))
            key = (sku, day.isoformat())
            totals[key] = totals.get(key, 0) + quantity
    return totals, lines, start


def collect_inbound(client, previous_eta):
    results, shipments, seen = [], [], set()
    for shipment in client.inbound_shipments():
        shipment_id = required(shipment, 'ShipmentId')
        status = required(shipment, 'ShipmentStatus')
        if shipment_id in seen or status not in SHIPMENT_STATUSES:
            raise SPAPIError('Duplicate shipment or unknown status')
        seen.add(shipment_id)
        shipments.append((shipment_id, status, shipment.get('DestinationFulfillmentCenterId') or ''))
        per_sku = defaultdict(int)
        for item in client.shipment_items(shipment_id):
            if item.get('ShipmentId') != shipment_id:
                raise SPAPIError('Shipment item belongs to a different shipment')
            sku = required(item, 'SellerSKU')
            shipped, received = units(item.get('QuantityShipped')), units(item.get('QuantityReceived'))
            per_sku[sku] += max(0, shipped - received)
        for sku, quantity in per_sku.items():
            # ConfirmedNeedByDate is NOT an ETA. Retain only explicitly imported ETAs.
            results.append((sku, shipment_id, quantity, previous_eta.get((sku, shipment_id), ''), status))
    return results, shipments


def sync(con, client, marketplace, kind='all', asof=None, timezone_name='UTC'):
    if kind not in ('all', 'sales', 'stock', 'inbound'):
        raise ValueError('Unknown sync kind')
    zone = ZoneInfo(timezone_name)
    asof = date.fromisoformat(asof) if asof else datetime.now(zone).date() - timedelta(days=1)
    kinds = ['stock', 'sales', 'inbound'] if kind == 'all' else [kind]
    stock = sales = inbound = None
    data_source = getattr(client, 'data_source', 'SP-API')
    if (data_source == 'MOCK') != is_mock_database(con):
        raise ValueError('Mock and live sources require separate database modes')
    try:
        binding = con.execute('SELECT * FROM marketplace_binding WHERE marketplace=?', (marketplace,)).fetchone()
        if binding and binding['api_marketplace'] != client.marketplace:
            raise ValueError('Local namespace is bound to a different API marketplace')
        if binding and binding['sales_timezone'] and 'sales' in kinds and binding['sales_timezone'] != timezone_name:
            raise ValueError('Sales timezone changed; use a separate database/namespace to avoid mixing daily buckets')
        if 'stock' in kinds:
            stock = collect_stock(client)
        if 'sales' in kinds:
            skus = [r['sku'] for r in con.execute('SELECT sku FROM sku WHERE marketplace=?', (marketplace,))]
            if not skus:
                raise ValueError('Import SKU mapping before sales synchronization')
            sales = collect_sales(client, skus, asof, timezone_name)
        if 'inbound' in kinds:
            previous_eta = {(r['sku'], r['shipment_id']): r['eta'] for r in con.execute('SELECT * FROM inbound WHERE marketplace=?', (marketplace,))}
            inbound = collect_inbound(client, previous_eta)
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
        return counts
    except (SPAPIError, ValueError, KeyError, TypeError):
        # Never persist API bodies or exceptions that might contain customer data.
        with con:
            for source in kinds:
                log_sync(con, marketplace, source, 'FAILED', 0, 'Collection/validation failed; previous data retained')
        raise
