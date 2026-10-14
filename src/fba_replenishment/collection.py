"""Token-free, resumable read-only collection of complete sales date shards.

Only an atomically completed shard is a checkpoint. Failed shards are fetched
again from their date boundary; pagination credentials never reach the cache.
CachedClient exposes the same read interface as SPAPIClient for reconciliation.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .spapi import SPAPIError, SHIPMENT_STATUSES

ORDER_KEYS = ('AmazonOrderId', 'PurchaseDate', 'OrderStatus', 'FulfillmentChannel', 'MarketplaceId')
ITEM_KEYS = ('OrderItemId', 'SellerSKU', 'ASIN', 'QuantityOrdered', 'QuantityShipped',
             'QuantityOrderedBeforeCancellation', 'CancellationExecuted', 'CancellationAmbiguous')
INVENTORY_KEYS = ('sellerSku', 'fnSku', 'asin', 'condition', 'totalQuantity', 'lastUpdatedTime')
INVENTORY_QUANTITIES = ('fulfillableQuantity', 'inboundWorkingQuantity',
                        'inboundShippedQuantity', 'inboundReceivingQuantity')
SHIPMENT_KEYS = ('ShipmentId', 'ShipmentStatus', 'DestinationFulfillmentCenterId')
SHIPMENT_ITEM_KEYS = ('ShipmentId', 'SellerSKU', 'FulfillmentNetworkSKU', 'QuantityInCase',
                      'QuantityShipped', 'QuantityReceived')
RESOLVED_STATUSES = {'Canceled', 'Shipped', 'Unshipped', 'PartiallyShipped', 'InvoiceUnconfirmed'}
PENDING_STATUSES = {'Pending', 'PendingAvailability', 'Unfulfillable'}


class CollectionQualityError(SPAPIError):
    """A safe diagnostic: codes and counts, without API payload or customer data."""

    def __init__(self, code, count=1):
        super().__init__('Collection quality gate failed: ' + code, code,
                         'Review collection audit before expanding or calculating')
        self.count = count


def _text(value, code):
    if not isinstance(value, str) or not value.strip():
        raise CollectionQualityError(code)
    return value


def _quantity(value, code):
    # bool is an int subclass and must not become inventory/sales units.
    if type(value) is not int or value < 0:
        raise CollectionQualityError(code)
    return value


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(_text(value, 'MISSING_PURCHASE_DATE').replace('Z', '+00:00'))
    except ValueError:
        raise CollectionQualityError('INVALID_PURCHASE_DATE') from None
    if parsed.tzinfo is None:
        raise CollectionQualityError('PURCHASE_DATE_WITHOUT_TIMEZONE')
    return parsed


def sanitize_order(row):
    if not isinstance(row, dict):
        raise CollectionQualityError('MALFORMED_ORDER')
    return {key: row.get(key) for key in ORDER_KEYS}


def sanitize_item(row):
    if not isinstance(row, dict):
        raise CollectionQualityError('MALFORMED_ORDER_ITEM')
    return {key: row[key] for key in ITEM_KEYS if key in row}


def sanitize_inventory(rows):
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionQualityError('MALFORMED_INVENTORY')
        item = {key: row[key] for key in INVENTORY_KEYS if key in row}
        details = row.get('inventoryDetails')
        if isinstance(details, dict):
            item['inventoryDetails'] = {key: details[key] for key in INVENTORY_QUANTITIES if key in details}
            for group, total in (('reservedQuantity', 'totalReservedQuantity'),
                                 ('unfulfillableQuantity', 'totalUnfulfillableQuantity')):
                if isinstance(details.get(group), dict):
                    item['inventoryDetails'][group] = {total: details[group].get(total)}
        clean.append(item)
    return clean


def validate_inventory(rows):
    """Reject missing quantities; never silently coerce unavailable details to zero."""
    seen = {}
    for row in rows:
        sku = _text(row.get('sellerSku'), 'MISSING_INVENTORY_SKU')
        identity = (sku, _text(row.get('fnSku'), 'MISSING_FNSKU'),
                    _text(row.get('condition'), 'MISSING_INVENTORY_CONDITION'))
        if identity in seen:
            raise CollectionQualityError('DUPLICATE_INVENTORY_IDENTITY')
        seen[identity] = row
        details = row.get('inventoryDetails')
        if not isinstance(details, dict):
            raise CollectionQualityError('MISSING_INVENTORY_DETAILS')
        for key in INVENTORY_QUANTITIES:
            _quantity(details.get(key), 'INCOMPLETE_INVENTORY_QUANTITIES')
        for group, total in (('reservedQuantity', 'totalReservedQuantity'),
                             ('unfulfillableQuantity', 'totalUnfulfillableQuantity')):
            values = details.get(group)
            if not isinstance(values, dict):
                raise CollectionQualityError('INCOMPLETE_INVENTORY_QUANTITIES')
            _quantity(values.get(total), 'INCOMPLETE_INVENTORY_QUANTITIES')
    return {'rows': len(rows), 'skus': len({row['sellerSku'] for row in rows}), 'complete_quantities': True}


def _boundaries(asof, timezone_name, days):
    if type(days) is not int or days < 1:
        raise ValueError('days must be a positive integer')
    asof = date.fromisoformat(asof) if isinstance(asof, str) else asof
    zone = ZoneInfo(timezone_name)
    start = asof - timedelta(days=days - 1)
    begin = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    finish = datetime.combine(asof + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    if finish > datetime.now(timezone.utc) - timedelta(minutes=2):
        raise ValueError('Sales asof must be a completed day, at least two minutes after its end')
    return start, asof, begin, finish


def _validate_orders(client, raw_orders, begin, finish, known_skus=None):
    if not isinstance(raw_orders, list):
        raise CollectionQualityError('MALFORMED_ORDER_COLLECTION')
    orders, items, issues = {}, {}, Counter()
    duplicates = 0
    for raw in raw_orders:
        order = sanitize_order(raw)
        identity = _text(order['AmazonOrderId'], 'MISSING_ORDER_ID')
        timestamp = _timestamp(order['PurchaseDate'])
        if timestamp == finish or begin - timedelta(seconds=1) <= timestamp < begin:
            continue  # Explicit half-open local date shard, including midnight.
        if not begin <= timestamp < finish:
            raise CollectionQualityError('ORDER_OUTSIDE_REQUESTED_WINDOW')
        if order['MarketplaceId'] != client.marketplace or order['FulfillmentChannel'] != 'AFN':
            raise CollectionQualityError('UNEXPECTED_ORDER_MARKETPLACE_OR_CHANNEL')
        status = _text(order['OrderStatus'], 'MISSING_ORDER_STATUS')
        if status not in RESOLVED_STATUSES | PENDING_STATUSES:
            raise CollectionQualityError('UNKNOWN_ORDER_STATUS')
        if identity in orders:
            if order != orders[identity]:
                raise CollectionQualityError('CONFLICTING_DUPLICATE_ORDER')
            duplicates += 1
            continue
        orders[identity] = order
        if status == 'Canceled':
            items[identity] = []
            continue
        clean_items = [sanitize_item(row) for row in client.order_items(identity)]
        if not clean_items:
            raise CollectionQualityError('ORDER_ITEMS_MISSING')
        seen_lines = {}
        for item in clean_items:
            item_id = _text(item.get('OrderItemId'), 'MISSING_ORDER_ITEM_ID')
            sku = _text(item.get('SellerSKU'), 'MISSING_ORDER_ITEM_SKU')
            if item_id in seen_lines:
                if item != seen_lines[item_id]:
                    raise CollectionQualityError('CONFLICTING_DUPLICATE_ORDER_ITEM')
                duplicates += 1
                continue
            _quantity(item.get('QuantityOrdered'), 'INVALID_ORDERED_QUANTITY')
            shipped = item.get('QuantityShipped')
            if shipped is not None:
                _quantity(shipped, 'INVALID_SHIPPED_QUANTITY')
            if item.get('CancellationAmbiguous'):
                raise CollectionQualityError('AMBIGUOUS_ITEM_CANCELLATION')
            original = item.get('QuantityOrderedBeforeCancellation', item['QuantityOrdered'])
            _quantity(original, 'INVALID_ORIGINAL_ORDERED_QUANTITY')
            if shipped is not None and shipped > original:
                raise CollectionQualityError('SHIPPED_EXCEEDS_ORDERED_QUANTITY')
            if status in PENDING_STATUSES:
                issues['UNRESOLVED_ORDER_STATUS'] += 1
            if known_skus is not None and sku not in known_skus:
                issues['ORDER_SKU_MISSING_FROM_INVENTORY'] += 1
            seen_lines[item_id] = item
        items[identity] = list(seen_lines.values())
    return list(orders.values()), items, dict(issues), duplicates


@dataclass
class SalesCollection:
    marketplace: str
    start: str
    asof: str
    timezone_name: str
    orders: list
    items: dict
    audit: list = field(default_factory=list)
    issues: dict = field(default_factory=dict)
    complete: bool = True

    @property
    def order_count(self):
        return len(self.orders)

    @property
    def item_count(self):
        return sum(len(rows) for rows in self.items.values())


def _binding(client, timezone_name):
    return {'marketplace': client.marketplace, 'endpoint': getattr(client, 'endpoint', ''),
            'orders_version': getattr(client, 'orders_version', ''), 'timezone': timezone_name}


def _write_checkpoint(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    # Restrict the sanitized operational data even though it contains no PII.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sales_digest(orders, items):
    return hashlib.sha256(json.dumps({'orders': orders, 'items': items},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def collect_sales_shards(client, cache_dir, asof, timezone_name='UTC', days=90,
                         shard_days=7, force=False, ttl_hours=6, known_skus=None):
    """Collect complete date shards and resume fresh validated checkpoints.

    ttl_hours prevents indefinitely reusing orders later cancelled by Amazon.
    force=True reconciles every shard again. A partial/failed shard is never saved.
    No caller-provided API bodies or pagination tokens are persisted.
    """
    if type(shard_days) is not int or shard_days < 1 or ttl_hours <= 0:
        raise ValueError('Positive shard_days and ttl_hours are required')
    start, asof, _, _ = _boundaries(asof, timezone_name, days)
    zone, binding = ZoneInfo(timezone_name), _binding(client, timezone_name)
    binding_id = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:20]
    directory = Path(cache_dir) / binding_id
    all_orders, all_items, audit, issues = {}, {}, [], Counter()
    cursor = start
    while cursor <= asof:
        last = min(asof, cursor + timedelta(days=shard_days - 1))
        begin = datetime.combine(cursor, time.min, zone).astimezone(timezone.utc)
        finish = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        path = directory / (cursor.isoformat() + '_' + last.isoformat() + '.json')
        cached = None
        if path.exists() and not force:
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                collected_at = _timestamp(value['collected_at'])
                if (value.get('format') == 2 and value.get('binding') == binding
                        and value.get('begin') == begin.isoformat() and value.get('finish') == finish.isoformat()
                        and value.get('complete') is True
                        and timedelta(0) <= datetime.now(timezone.utc) - collected_at <= timedelta(hours=ttl_hours)):
                    cached = value
            except (ValueError, KeyError, TypeError, SPAPIError):
                raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT') from None
        if cached is not None:
            if (not isinstance(cached.get('orders'), list) or not isinstance(cached.get('items'), dict)
                    or cached.get('data_sha256') != _sales_digest(cached['orders'], cached['items'])):
                raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
            clean = _CheckpointClient(client.marketplace, cached.get('items'))
            orders, items, shard_issues, duplicates = _validate_orders(
                clean, cached.get('orders', []), begin, finish, known_skus)
            source = 'checkpoint'
        else:
            # Query one second before the midnight boundary to tolerate an API
            # that defines createdAfter as exclusive, then filter locally.
            raw = client.orders((begin - timedelta(seconds=1)).isoformat(), finish.isoformat())
            path_key = '/orders/' + getattr(client, 'orders_version', 'v0') + '/orders'
            if getattr(client, 'collection_complete', {}).get(path_key) is False:
                raise CollectionQualityError('PARTIAL_SALES_COLLECTION')
            orders, items, shard_issues, duplicates = _validate_orders(client, raw, begin, finish, known_skus)
            _write_checkpoint(path, {'format': 2, 'binding': binding, 'begin': begin.isoformat(),
                                    'finish': finish.isoformat(), 'collected_at': datetime.now(timezone.utc).isoformat(),
                                    'complete': True, 'orders': orders, 'items': items,
                                    'data_sha256': _sales_digest(orders, items)})
            source = 'SP-API'
        for order in orders:
            identity = order['AmazonOrderId']
            if identity in all_orders:
                if order != all_orders[identity] or items[identity] != all_items[identity]:
                    raise CollectionQualityError('CONFLICTING_DUPLICATE_ORDER_ACROSS_SHARDS')
                continue
            all_orders[identity], all_items[identity] = order, items[identity]
        issues.update(shard_issues)
        audit.append({'begin': begin.isoformat(), 'finish': finish.isoformat(), 'source': source,
                      'orders': len(orders), 'items': sum(map(len, items.values())),
                      'deduplicated': duplicates, 'complete': True, 'issues': shard_issues})
        cursor = last + timedelta(days=1)
    return SalesCollection(client.marketplace, start.isoformat(), asof.isoformat(), timezone_name,
                           list(all_orders.values()), all_items, audit, dict(issues))


class _CheckpointClient:
    def __init__(self, marketplace, items):
        self.marketplace, self.items = marketplace, items
        if not isinstance(items, dict):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')

    def order_items(self, identity):
        values = self.items.get(identity)
        if not isinstance(values, list):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        return values


class CachedClient:
    """Read interface backed by already collected, validated, sanitized snapshots."""
    data_source = 'SP-API'

    def __init__(self, marketplace, sales=None, inventory=None, shipments=None, shipment_items=None):
        self.marketplace, self.sales = marketplace, sales
        if sales is not None and (not sales.complete or sales.marketplace != marketplace):
            raise CollectionQualityError('INCOMPLETE_OR_MISMATCHED_SALES_CACHE')
        self.inventory = sanitize_inventory(inventory) if inventory is not None else None
        self.shipments = None if shipments is None else [
            {key: row.get(key) for key in SHIPMENT_KEYS} for row in shipments]
        self.inbound_items = None if shipment_items is None else {
            identity: [{key: row.get(key) for key in SHIPMENT_ITEM_KEYS} for row in values]
            for identity, values in shipment_items.items()}

    def orders(self, begin, finish):
        if self.sales is None:
            raise CollectionQualityError('MISSING_SALES_CACHE')
        requested_begin, requested_finish = _timestamp(begin), _timestamp(finish)
        _, _, available_begin, available_finish = _boundaries(
            self.sales.asof, self.sales.timezone_name,
            (date.fromisoformat(self.sales.asof) - date.fromisoformat(self.sales.start)).days + 1)
        if requested_begin < available_begin or requested_finish > available_finish:
            raise CollectionQualityError('REQUEST_OUTSIDE_CACHED_SALES_WINDOW')
        return [dict(row) for row in self.sales.orders
                if requested_begin <= _timestamp(row['PurchaseDate']) < requested_finish]

    def order_items(self, identity):
        if self.sales is None or identity not in self.sales.items:
            raise CollectionQualityError('MISSING_ORDER_ITEMS_CACHE')
        return [dict(row) for row in self.sales.items[identity]]

    def inventory_summaries(self):
        if self.inventory is None:
            raise CollectionQualityError('MISSING_INVENTORY_CACHE')
        return json.loads(json.dumps(self.inventory))

    def inbound_shipments(self):
        if self.shipments is None:
            raise CollectionQualityError('MISSING_SHIPMENT_CACHE')
        return [dict(row) for row in self.shipments]

    def shipment_items(self, identity):
        if self.inbound_items is None or identity not in self.inbound_items:
            raise CollectionQualityError('MISSING_SHIPMENT_ITEMS_CACHE')
        return [dict(row) for row in self.inbound_items[identity]]


def quality_probe(client, asof, timezone_name='UTC', days=2, max_pages=2,
                  max_results=25, inventory_pages=2):
    """Small bounded probe. Its result is explicitly unsuitable for complete sync."""
    _, _, begin, finish = _boundaries(asof, timezone_name, days)
    inventory = sanitize_inventory(client.inventory_summaries(max_pages=inventory_pages))
    quality = validate_inventory(inventory)
    raw = client.orders((begin - timedelta(seconds=1)).isoformat(), finish.isoformat(),
                        max_pages=max_pages, max_results=max_results)
    known = {row['sellerSku'] for row in inventory}
    orders, items, issues, duplicates = _validate_orders(client, raw, begin, finish, known)
    inventory_complete = client.collection_complete.get('/fba/inventory/v1/summaries', False)
    orders_path = '/orders/' + client.orders_version + '/orders'
    orders_complete = client.collection_complete.get(orders_path, False)
    report = {'mode': 'bounded_probe', 'begin': begin.isoformat(), 'finish': finish.isoformat(),
              'orders': len(orders), 'order_items': sum(map(len, items.values())),
              'inventory': quality, 'inventory_complete': inventory_complete,
              'orders_complete': orders_complete, 'deduplicated': duplicates,
              'issues': issues, 'pagination': list(client.pagination_audit),
              'expansion_allowed': not issues, 'complete_snapshot': False}
    # Missing matching SKUs in a bounded inventory page needs a full inventory
    # check before it is classified as a definitive mapping failure.
    if issues.get('ORDER_SKU_MISSING_FROM_INVENTORY') and not inventory_complete:
        count = report['issues'].pop('ORDER_SKU_MISSING_FROM_INVENTORY')
        report['issues']['ORDER_SKU_NOT_OBSERVED_IN_PARTIAL_INVENTORY'] = count
    return report, inventory, SalesCollection(client.marketplace, begin.astimezone(ZoneInfo(timezone_name)).date().isoformat(),
                                             asof.isoformat() if isinstance(asof, date) else asof,
                                             timezone_name, orders, items, issues=issues, complete=False)


def _snapshot_path(client, cache_dir, kind):
    binding = _binding(client, '')
    identity = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:20]
    return Path(cache_dir) / identity / (kind + '.json'), binding


def _read_snapshot(path, binding, force, ttl_hours):
    if force or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        # Older snapshots did not attest row counts or body integrity. Fetch a
        # new snapshot rather than certifying an old cache as current data.
        if value.get('format') != 2:
            return None
        age = datetime.now(timezone.utc) - _timestamp(value['collected_at'])
        if (value.get('binding') == binding
                and value.get('complete') is True and timedelta(0) <= age <= timedelta(hours=ttl_hours)):
            return value
    except (ValueError, KeyError, TypeError, SPAPIError):
        raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT') from None
    return None


def _snapshot_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def collect_inventory_snapshot(client, cache_dir, force=False, ttl_hours=1):
    """Collect every FBA inventory page independently of sales expansion."""
    if ttl_hours <= 0:
        raise ValueError('Positive ttl_hours is required')
    path, binding = _snapshot_path(client, cache_dir, 'inventory')
    checkpoint = _read_snapshot(path, binding, force, ttl_hours)
    if checkpoint is None:
        rows = sanitize_inventory(client.inventory_summaries())
        if getattr(client, 'collection_complete', {}).get('/fba/inventory/v1/summaries') is False:
            raise CollectionQualityError('PARTIAL_INVENTORY_COLLECTION')
        if not rows:
            raise CollectionQualityError('EMPTY_INVENTORY_REQUIRES_REVIEW')
        quality = validate_inventory(rows)
        observed_at = datetime.now(timezone.utc).isoformat()
        _write_checkpoint(path, {'format': 2, 'binding': binding, 'complete': True,
                                'collected_at': observed_at, 'rows': rows,
                                'row_count': len(rows), 'sku_count': quality['skus'],
                                'data_sha256': _snapshot_digest(rows)})
        source = 'SP-API'
    else:
        if not isinstance(checkpoint.get('rows'), list):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        if (type(checkpoint.get('row_count')) is not int
                or checkpoint['row_count'] != len(checkpoint['rows'])
                or checkpoint.get('data_sha256') != _snapshot_digest(checkpoint['rows'])):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        rows = sanitize_inventory(checkpoint['rows'])
        if not rows:
            raise CollectionQualityError('EMPTY_INVENTORY_REQUIRES_REVIEW')
        quality = validate_inventory(rows)
        if type(checkpoint.get('sku_count')) is not int or checkpoint['sku_count'] != quality['skus']:
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        observed_at = checkpoint['collected_at']
        source = 'checkpoint'
    return rows, dict(quality, source=source, complete=True, observed_at=observed_at)


def _sanitize_shipments(rows):
    clean, seen = [], {}
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionQualityError('MALFORMED_SHIPMENT')
        item = {key: row.get(key) for key in SHIPMENT_KEYS}
        identity = _text(item['ShipmentId'], 'MISSING_SHIPMENT_ID')
        if item['ShipmentStatus'] not in SHIPMENT_STATUSES:
            raise CollectionQualityError('UNKNOWN_SHIPMENT_STATUS')
        if identity in seen:
            if item != seen[identity]:
                raise CollectionQualityError('CONFLICTING_DUPLICATE_SHIPMENT')
            continue
        seen[identity] = item
        clean.append(item)
    return clean


def _sanitize_shipment_items(shipment_id, rows, allow_quantity_exceptions=False):
    clean, seen = [], {}
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionQualityError('MALFORMED_SHIPMENT_ITEM')
        item = {key: row.get(key) for key in SHIPMENT_ITEM_KEYS}
        if item['ShipmentId'] != shipment_id:
            raise CollectionQualityError('SHIPMENT_ITEM_ID_MISMATCH')
        sku = _text(item['SellerSKU'], 'MISSING_SHIPMENT_ITEM_SKU')
        shipped = _quantity(item['QuantityShipped'], 'MISSING_SHIPMENT_SHIPPED_QUANTITY')
        received = _quantity(item['QuantityReceived'], 'MISSING_SHIPMENT_RECEIVED_QUANTITY')
        if received > shipped and not allow_quantity_exceptions:
            raise CollectionQualityError('RECEIVED_EXCEEDS_SHIPPED_QUANTITY')
        identity = (sku, item['FulfillmentNetworkSKU'])
        if identity in seen:
            if item != seen[identity]:
                raise CollectionQualityError('CONFLICTING_DUPLICATE_SHIPMENT_ITEM')
            continue
        seen[identity] = item
        clean.append(item)
    return clean


def collect_inbound_snapshot(client, cache_dir, force=False, ttl_hours=1,
                             include_terminal_items=False, allow_quantity_exceptions=False):
    """Collect current shipment identities plus active/working lines, read-only.

    Terminal shipment lines are unnecessary for in-transit quantities and are
    omitted by default; their statuses remain in the shipment audit.
    Opt-in SKU isolation retains received>shipped as original quantities, never
    as a fabricated zero balance. The default strict mode remains unchanged.
    """
    if ttl_hours <= 0:
        raise ValueError('Positive ttl_hours is required')
    path, binding = _snapshot_path(client, cache_dir, 'inbound')
    binding = dict(binding, include_terminal_items=include_terminal_items)
    if allow_quantity_exceptions:
        path = path.with_name('inbound_sku_exceptions.json')
        binding = dict(binding, allow_quantity_exceptions=True)
    checkpoint = _read_snapshot(path, binding, force, ttl_hours)
    active = {'WORKING', 'SHIPPED', 'RECEIVING', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN'}
    if checkpoint is None:
        shipments = _sanitize_shipments(client.inbound_shipments())
        items = {}
        for shipment in shipments:
            identity = shipment['ShipmentId']
            rows = (client.shipment_items(identity)
                    if include_terminal_items or shipment['ShipmentStatus'] in active else [])
            items[identity] = _sanitize_shipment_items(identity, rows, allow_quantity_exceptions)
            if not items[identity] and shipment['ShipmentStatus'] in active - {'WORKING'}:
                raise CollectionQualityError('INBOUND_ITEMS_INCOMPLETE')
        observed_at = datetime.now(timezone.utc).isoformat()
        _write_checkpoint(path, {'format': 2, 'binding': binding, 'complete': True,
                                'collected_at': observed_at,
                                'shipments': shipments, 'shipment_items': items,
                                'shipment_count': len(shipments),
                                'item_count': sum(map(len, items.values())),
                                'data_sha256': _snapshot_digest({'shipments': shipments, 'shipment_items': items})})
        source = 'SP-API'
    else:
        if not isinstance(checkpoint.get('shipments'), list):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        shipments = _sanitize_shipments(checkpoint['shipments'])
        raw_items = checkpoint.get('shipment_items')
        if not isinstance(raw_items, dict):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        if (type(checkpoint.get('shipment_count')) is not int
                or checkpoint['shipment_count'] != len(shipments)
                or type(checkpoint.get('item_count')) is not int
                or any(not isinstance(values, list) for values in raw_items.values())
                or checkpoint['item_count'] != sum(map(len, raw_items.values()))
                or set(raw_items) != {row['ShipmentId'] for row in shipments}
                or checkpoint.get('data_sha256') != _snapshot_digest(
                    {'shipments': checkpoint['shipments'], 'shipment_items': raw_items})):
            raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
        items = {}
        for shipment in shipments:
            identity = shipment['ShipmentId']
            if not isinstance(raw_items.get(identity), list):
                raise CollectionQualityError('INVALID_COLLECTION_CHECKPOINT')
            items[identity] = _sanitize_shipment_items(identity, raw_items[identity], allow_quantity_exceptions)
            if not items[identity] and shipment['ShipmentStatus'] in active - {'WORKING'}:
                raise CollectionQualityError('INBOUND_ITEMS_INCOMPLETE')
        source = 'checkpoint'
        observed_at = checkpoint['collected_at']
    exceptions = [row for rows in items.values() for row in rows
                  if row['QuantityReceived'] > row['QuantityShipped']]
    audit = {'shipments': len(shipments), 'shipment_items': sum(map(len, items.values())),
             'statuses': dict(Counter(row['ShipmentStatus'] for row in shipments)),
             'source': source, 'complete': True, 'observed_at': observed_at,
             'terminal_items_collected': include_terminal_items,
             'allow_quantity_exceptions': allow_quantity_exceptions,
             'quantity_exception_rows': len(exceptions),
             'quantity_exception_sku_count': len({row['SellerSKU'] for row in exceptions}),
             'quantity_exceptions': {'RECEIVED_EXCEEDS_SHIPPED_QUANTITY': len(exceptions)} if exceptions else {},
             'quantity_status': 'SUCCESS_WITH_SKU_EXCEPTIONS' if exceptions else 'SUCCESS',
             'eta_source': 'manual_only; API arrival/need-by dates are not verified ETA'}
    return shipments, items, audit
