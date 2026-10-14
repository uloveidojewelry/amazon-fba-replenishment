"""Offline quality gates and resumable date shards; no Amazon calls or PII."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from fba_replenishment.collection import (
    CachedClient, CollectionQualityError, collect_inbound_snapshot,
    collect_inventory_snapshot, collect_sales_shards, quality_probe, sanitize_inventory,
)

ASOF = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
MARKETPLACE = 'ATVPDKIKX0DER'


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real HTTP is forbidden in collection tests')
    monkeypatch.setattr(requests.Session, 'request', forbidden)


def inventory(sku='a'):
    return {'sellerSku': sku, 'fnSku': 'fn-' + sku, 'asin': 'B000000001', 'condition': 'NewItem',
            'customerName': 'DO-NOT-PERSIST-PII',
            'inventoryDetails': {'fulfillableQuantity': 3, 'inboundWorkingQuantity': 0,
                                 'inboundShippedQuantity': 0, 'inboundReceivingQuantity': 0,
                                 'reservedQuantity': {'totalReservedQuantity': 1},
                                 'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
                                 'unrelated': 'DO-NOT-PERSIST-PII'}}


class API:
    marketplace = MARKETPLACE
    endpoint = 'https://sellingpartnerapi-na.amazon.com'
    orders_version = '2026-01-01'

    def __init__(self):
        self.calls = []
        self.items = {}
        self.collection_complete = {}
        self.pagination_audit = []
        self.fail_call = None
        self.stock = [inventory()]
        self.pending = False
        self.ambiguous_cancellation = False
        self.item_quantity = 2
        self.shipment_status = 'SHIPPED'

    def orders(self, begin, finish, **kwargs):
        self.calls.append((begin, finish))
        if self.fail_call == len(self.calls):
            raise CollectionQualityError('OFFLINE_FAILURE')
        day = datetime.fromisoformat(begin).date() + timedelta(days=1)
        identity = day.isoformat()
        self.items = {identity: [{'OrderItemId': 'line', 'SellerSKU': 'a',
                                 'QuantityOrdered': self.item_quantity, 'QuantityShipped': 2,
                                 'CancellationAmbiguous': self.ambiguous_cancellation,
                                 'customerName': 'DO-NOT-PERSIST-PII'}]}
        self.collection_complete['/orders/2026-01-01/orders'] = not kwargs
        return [{'AmazonOrderId': identity, 'PurchaseDate': day.isoformat() + 'T00:00:00Z',
                 'OrderStatus': 'Pending' if self.pending else 'Shipped',
                 'MarketplaceId': MARKETPLACE, 'FulfillmentChannel': 'AFN',
                 'BuyerEmail': 'DO-NOT-PERSIST-PII'}]

    def order_items(self, identity):
        return deepcopy(self.items[identity])

    def inventory_summaries(self, **kwargs):
        self.collection_complete['/fba/inventory/v1/summaries'] = not kwargs
        return deepcopy(self.stock)

    def inbound_shipments(self):
        return [{'ShipmentId': 'shipment', 'ShipmentStatus': self.shipment_status,
                 'ShipFromAddress': 'DO-NOT-PERSIST-PII'}]

    def shipment_items(self, identity):
        return [{'ShipmentId': identity, 'SellerSKU': 'a', 'QuantityShipped': 7,
                 'QuantityReceived': 3, 'FulfillmentNetworkSKU': 'fn-a', 'QuantityInCase': 1,
                 'ShipFromAddress': 'DO-NOT-PERSIST-PII'}]


def test_ninety_complete_days_resume_and_force_refresh_without_tokens_or_pii(tmp_path):
    api = API()
    first = collect_sales_shards(api, tmp_path, ASOF, days=90, shard_days=7)
    assert len(api.calls) == 13
    assert first.complete and first.order_count == first.item_count == 13
    assert (datetime.fromisoformat(first.asof) - datetime.fromisoformat(first.start)).days == 89
    persisted = '\n'.join(path.read_text() for path in tmp_path.rglob('*.json'))
    assert 'DO-NOT-PERSIST' not in persisted
    assert not any(word in persisted for word in ('paginationToken', 'nextToken', 'refresh_token', 'access_token'))
    second = collect_sales_shards(api, tmp_path, ASOF, days=90)
    assert len(api.calls) == 13 and all(row['source'] == 'checkpoint' for row in second.audit)
    assert first.orders == second.orders
    collect_sales_shards(api, tmp_path, ASOF, days=90, force=True)
    assert len(api.calls) == 26


def test_interrupted_shard_is_not_saved_and_resume_reuses_completed_shards(tmp_path):
    api = API()
    api.fail_call = 2
    with pytest.raises(CollectionQualityError, match='OFFLINE_FAILURE'):
        collect_sales_shards(api, tmp_path, ASOF, days=15)
    assert len(list(tmp_path.rglob('*.json'))) == 1
    api.fail_call = None
    result = collect_sales_shards(api, tmp_path, ASOF, days=15)
    assert len(api.calls) == 4
    assert result.audit[0]['source'] == 'checkpoint' and len(result.audit) == 3


@pytest.mark.parametrize('change,code', [
    ('ambiguous_cancellation', 'AMBIGUOUS_ITEM_CANCELLATION'),
    ('item_quantity', 'INVALID_ORDERED_QUANTITY'),
])
def test_unusable_quantity_or_partial_cancellation_never_certified(tmp_path, change, code):
    api = API()
    setattr(api, change, True if change == 'ambiguous_cancellation' else None)
    with pytest.raises(CollectionQualityError) as error:
        collect_sales_shards(api, tmp_path, ASOF, days=7)
    assert error.value.code == code and not list(tmp_path.rglob('*.json'))


def test_pending_is_identified_without_becoming_zero(tmp_path):
    api = API()
    api.pending = True
    data = collect_sales_shards(api, tmp_path, ASOF, days=7)
    assert data.issues == {'UNRESOLVED_ORDER_STATUS': 1}
    assert data.items[data.orders[0]['AmazonOrderId']][0]['QuantityOrdered'] == 2


def test_cached_client_refuses_partial_and_outside_window(tmp_path):
    api = API()
    sales = collect_sales_shards(api, tmp_path, ASOF, days=90)
    cached = CachedClient(MARKETPLACE, sales, api.stock, api.inbound_shipments(),
                          {'shipment': api.shipment_items('shipment')})
    begin, finish = api.calls[0]
    actual_begin = (datetime.fromisoformat(begin) + timedelta(seconds=1)).isoformat()
    assert len(cached.orders(actual_begin, finish)) == 1
    with pytest.raises(CollectionQualityError, match='OUTSIDE_CACHED'):
        cached.orders('2020-01-01T00:00:00Z', finish)
    sales.complete = False
    with pytest.raises(CollectionQualityError, match='INCOMPLETE_OR_MISMATCHED'):
        CachedClient(MARKETPLACE, sales)
    assert 'DO-NOT-PERSIST' not in json.dumps(cached.inventory_summaries())


def test_small_probe_distinguishes_partial_inventory_from_missing_inventory():
    api = API()
    api.stock = [inventory('other')]
    report, _, sample = quality_probe(api, ASOF)
    assert report['orders'] == report['order_items'] == 1
    assert report['issues'] == {'ORDER_SKU_NOT_OBSERVED_IN_PARTIAL_INVENTORY': 1}
    assert not report['complete_snapshot'] and not sample.complete
    assert not report['expansion_allowed']


def test_inventory_quantity_absence_rejects_snapshot_instead_of_zero(tmp_path):
    api = API()
    del api.stock[0]['inventoryDetails']['inboundReceivingQuantity']
    with pytest.raises(CollectionQualityError, match='INCOMPLETE_INVENTORY_QUANTITIES'):
        collect_inventory_snapshot(api, tmp_path)
    assert not list(tmp_path.rglob('*.json'))
    assert 'inboundReceivingQuantity' not in sanitize_inventory(api.stock)[0]['inventoryDetails']


def test_stock_and_inbound_can_collect_without_expanding_orders(tmp_path):
    api = API()
    stock, audit = collect_inventory_snapshot(api, tmp_path)
    shipments, items, inbound_audit = collect_inbound_snapshot(api, tmp_path)
    assert not api.calls
    assert audit['skus'] == 1 and inbound_audit['shipment_items'] == 1
    assert items['shipment'][0]['FulfillmentNetworkSKU'] == 'fn-a'
    assert 'DO-NOT-PERSIST' not in json.dumps([stock, shipments, items])
    _, second = collect_inventory_snapshot(api, tmp_path)
    assert second['source'] == 'checkpoint'


def test_terminal_inbound_items_are_skipped_and_uncertain_quantities_rejected(tmp_path):
    api = API()
    api.shipment_status = 'CLOSED'
    _, items, audit = collect_inbound_snapshot(api, tmp_path)
    assert items == {'shipment': []} and not audit['terminal_items_collected']
    api.shipment_status = 'SHIPPED'
    api.shipment_items = lambda identity: [{'ShipmentId': identity, 'SellerSKU': 'a',
                                          'QuantityShipped': 3, 'QuantityReceived': 4}]
    with pytest.raises(CollectionQualityError, match='RECEIVED_EXCEEDS_SHIPPED_QUANTITY'):
        collect_inbound_snapshot(api, tmp_path, force=True)


def test_expired_checkpoint_is_refetched_for_later_cancellations(tmp_path):
    api = API()
    collect_sales_shards(api, tmp_path, ASOF, days=7)
    path = next(tmp_path.rglob('*.json'))
    content = json.loads(path.read_text())
    content['collected_at'] = '2020-01-01T00:00:00Z'
    path.write_text(json.dumps(content))
    collect_sales_shards(api, tmp_path, ASOF, days=7)
    assert len(api.calls) == 2


def test_conflicting_item_duplicate_is_rejected(tmp_path):
    api = API()
    original = api.order_items
    api.order_items = lambda identity: original(identity) + [dict(original(identity)[0], QuantityOrdered=3)]
    with pytest.raises(CollectionQualityError, match='CONFLICTING_DUPLICATE_ORDER_ITEM'):
        collect_sales_shards(api, tmp_path, ASOF, days=7)
    assert not list(tmp_path.rglob('*.json'))


def test_corrupted_completed_checkpoint_cannot_turn_unknown_orders_into_zero(tmp_path):
    api = API()
    collect_sales_shards(api, tmp_path, ASOF, days=7)
    path = next(tmp_path.rglob('*.json'))
    content = json.loads(path.read_text())
    content['orders'] = []
    content['items'] = {}
    path.write_text(json.dumps(content))
    with pytest.raises(CollectionQualityError, match='INVALID_COLLECTION_CHECKPOINT'):
        collect_sales_shards(api, tmp_path, ASOF, days=7)
    assert len(api.calls) == 1


def test_stopped_family_mapping_is_never_inferred_by_collection(tmp_path):
    api = API()
    stock, _ = collect_inventory_snapshot(api, tmp_path)
    assert all('family' not in row and 'enabled' not in row for row in stock)
    assert 'family' not in '\n'.join(path.read_text() for path in tmp_path.rglob('*.json'))


def test_inventory_snapshot_corruption_or_missing_counts_cannot_clear_known_skus(tmp_path):
    api = API()
    collect_inventory_snapshot(api, tmp_path)
    path = next(tmp_path.rglob('inventory.json'))
    content = json.loads(path.read_text())
    content['rows'] = []
    path.write_text(json.dumps(content))
    with pytest.raises(CollectionQualityError, match='INVALID_COLLECTION_CHECKPOINT'):
        collect_inventory_snapshot(api, tmp_path)


def test_snapshot_observed_at_remains_original_collection_time(tmp_path):
    api = API()
    _, first = collect_inventory_snapshot(api, tmp_path)
    _, second = collect_inventory_snapshot(api, tmp_path)
    assert second['source'] == 'checkpoint'
    assert second['observed_at'] == first['observed_at']
    _, _, inbound_first = collect_inbound_snapshot(api, tmp_path)
    _, _, inbound_second = collect_inbound_snapshot(api, tmp_path)
    assert inbound_second['observed_at'] == inbound_first['observed_at']


def test_legacy_snapshot_without_integrity_attestation_is_refetched(tmp_path):
    api = API()
    collect_inventory_snapshot(api, tmp_path)
    path = next(tmp_path.rglob('inventory.json'))
    content = json.loads(path.read_text())
    content['format'] = 1
    del content['data_sha256']
    content['rows'] = []
    path.write_text(json.dumps(content))
    rows, audit = collect_inventory_snapshot(api, tmp_path)
    assert len(rows) == 1 and audit['source'] == 'SP-API'
    assert json.loads(path.read_text())['format'] == 2


def test_empty_full_inventory_needs_review_and_is_never_checkpointed(tmp_path):
    api = API()
    api.stock = []
    with pytest.raises(CollectionQualityError, match='EMPTY_INVENTORY_REQUIRES_REVIEW'):
        collect_inventory_snapshot(api, tmp_path)
    assert not list(tmp_path.rglob('*.json'))


def test_inbound_snapshot_removed_lines_fails_checksum_before_zero_transit(tmp_path):
    api = API()
    collect_inbound_snapshot(api, tmp_path)
    path = next(tmp_path.rglob('inbound.json'))
    content = json.loads(path.read_text())
    content['shipment_items']['shipment'] = []
    path.write_text(json.dumps(content))
    with pytest.raises(CollectionQualityError, match='INVALID_COLLECTION_CHECKPOINT'):
        collect_inbound_snapshot(api, tmp_path)
