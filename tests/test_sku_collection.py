"""Offline acceptance of SKU-isolated sales and atomic local snapshots."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from fba_replenishment.collection import CollectionQualityError, SalesCollection, _boundaries
from fba_replenishment.core import connect
from fba_replenishment.sku_collection import precheck_sku, reconcile_sku_data

MARKETPLACE = 'ATVPDKIKX0DER'
ASOF = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Real HTTP forbidden in SKU collection tests')
    monkeypatch.setattr(requests.Session, 'request', denied)


def inventory(sku='a', asin='B000000001'):
    return {'sellerSku': sku, 'fnSku': 'fn-' + sku, 'condition': 'NewItem', 'asin': asin,
            'BuyerName': 'PRIVATE-DO-NOT-PERSIST', 'inventoryDetails': {
                'fulfillableQuantity': 5, 'reservedQuantity': {'totalReservedQuantity': 2},
                'unfulfillableQuantity': {'totalUnfulfillableQuantity': 1},
                'inboundWorkingQuantity': 2, 'inboundShippedQuantity': 3,
                'inboundReceivingQuantity': 4}}


def order(identity='o', status='Shipped', marketplace=MARKETPLACE):
    return {'AmazonOrderId': identity, 'PurchaseDate': ASOF + 'T12:00:00Z',
            'OrderStatus': status, 'FulfillmentChannel': 'AFN', 'MarketplaceId': marketplace,
            'BuyerEmail': 'PRIVATE-DO-NOT-PERSIST'}


def item(sku='a', quantity=3, identity='i'):
    return {'OrderItemId': identity, 'SellerSKU': sku, 'ASIN': 'B000000001',
            'QuantityOrdered': quantity, 'QuantityShipped': quantity,
            'BuyerInfo': 'PRIVATE-DO-NOT-PERSIST'}


def sales(orders=None, items=None, days=90, marketplace=MARKETPLACE):
    orders = [order()] if orders is None else orders
    items = {'o': [item()]} if items is None else items
    begin_day, _, begin, finish = _boundaries(ASOF, 'UTC', days)
    identities = {row['AmazonOrderId'] for row in orders}
    noncanceled = {row['AmazonOrderId'] for row in orders if row['OrderStatus'] != 'Canceled'}
    audit = [{'begin': begin.isoformat(), 'finish': finish.isoformat(), 'complete': True,
              'orders': len(identities), 'items': sum(len({row['OrderItemId'] for row in items[identity]})
                                                    for identity in noncanceled)}]
    return SalesCollection(marketplace, begin_day.isoformat(), ASOF, 'UTC', orders, items, audit)


def inventory_audit(rows):
    return {'complete': True, 'rows': len(rows), 'skus': len({row['sellerSku'] for row in rows}),
            'observed_at': datetime.now(timezone.utc).isoformat()}


def reconcile(con, stock=None, collection=None, shipments=None, shipment_items=None, marketplace='US'):
    stock = [inventory(), inventory('b')] if stock is None else stock
    collection = sales() if collection is None else collection
    return reconcile_sku_data(con, stock, collection, shipments or [], shipment_items or {},
                              marketplace, inventory_audit(stock))


@pytest.fixture
def con(tmp_path):
    db = connect(tmp_path / 'test.db')
    yield db
    db.close()


def test_complete_calendar_zero_is_proven_and_family_not_required(con):
    summary = reconcile(con)
    assert summary['sku_count'] == 2 and summary['sales_daily_rows'] == 180
    assert summary['sales_complete_rate'] == 1
    assert con.execute("SELECT SUM(units),COUNT(*) FROM sales WHERE sku='b'").fetchone()[:] == (0, 90)
    assert con.execute("SELECT SUM(units),COUNT(*) FROM sales WHERE sku='a'").fetchone()[:] == (3, 90)
    assert all(row['family'] is None for row in con.execute('SELECT * FROM sku'))
    assert con.execute('SELECT inbound_working,inbound_shipped,inbound_receiving FROM inventory_details').fetchone()[:] == (2, 3, 4)


def test_cancelled_orders_removed_and_exact_duplicates_not_double_counted(con):
    data = sales([order(), order(), order('c', 'Canceled')],
                 {'o': [item(), item()], 'c': []})
    summary = reconcile(con, collection=data)
    assert summary['orders'] == 2 and summary['order_items'] == 1
    assert summary['deduplicated'] == 2
    assert con.execute('SELECT SUM(units) FROM sales').fetchone()[0] == 3
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 1
    reconcile(con, collection=sales([order(status='Canceled')], {'o': []}))
    assert con.execute('SELECT SUM(units) FROM sales').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 0


def test_pending_affects_its_sku_only_and_fresh_resolved_data_clears_owned_block(con):
    data = sales([order('p', 'Pending'), order('s')],
                 {'p': [item('a', 2)], 's': [item('b', 4)]})
    summary = reconcile(con, collection=data)
    assert summary['pending_sku_count'] == 1 and summary['sales_complete_rate'] == .5
    assert con.execute("SELECT COUNT(*) FROM sales WHERE sku='a'").fetchone()[0] == 0
    assert con.execute("SELECT SUM(units),COUNT(*) FROM sales WHERE sku='b'").fetchone()[:] == (4, 90)
    assert con.execute("SELECT COUNT(*) FROM order_line WHERE sku='a'").fetchone()[0] == 1
    assert con.execute("SELECT code FROM data_quality_block WHERE sku='a'").fetchone()[0] == 'ORDERS_PENDING_STATUS'
    assert con.execute("SELECT status FROM sync_log WHERE kind='sales'").fetchone()[0] == 'SUCCESS_WITH_SKU_EXCEPTIONS'
    reconcile(con)
    assert con.execute("SELECT COUNT(*) FROM data_quality_block WHERE code='ORDERS_PENDING_STATUS'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM sales WHERE sku='a'").fetchone()[0] == 90


def test_formal_mapping_manual_stops_and_other_blocks_survive(con):
    with con:
        con.execute("INSERT INTO sku VALUES('US','a','human-family','B000000001',0)")
        con.execute("INSERT INTO stop_flag VALUES('US','a','manual','2020-01-01',1)")
        con.execute("INSERT INTO family_stop VALUES('US','human-family','a',1)")
        con.execute("INSERT INTO data_quality_block VALUES('US','a','DISCOVERY_FAMILY_STOPPED','retain')")
    reconcile(con)
    assert con.execute("SELECT family,enabled FROM sku WHERE sku='a'").fetchone()[:] == ('human-family', 0)
    assert con.execute('SELECT active FROM stop_flag').fetchone()[0] == 1
    assert con.execute('SELECT active FROM family_stop').fetchone()[0] == 1
    assert con.execute('SELECT code FROM data_quality_block').fetchone()[0] == 'DISCOVERY_FAMILY_STOPPED'


def test_historical_sku_without_current_stock_kept_and_not_given_zero_stock(con):
    with con:
        con.execute("INSERT INTO sku VALUES('US','historical','historic-family',NULL,1)")
    data = sales([order()], {'o': [item('sold-before', 6)]})
    summary = reconcile(con, collection=data)
    assert summary['sku_count'] == 4 and summary['stock_missing_sku_count'] == 2
    assert con.execute("SELECT COUNT(*) FROM sku WHERE sku='historical'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM stock WHERE sku IN ('historical','sold-before')").fetchone()[0] == 0
    assert con.execute("SELECT SUM(units) FROM sales WHERE sku='sold-before'").fetchone()[0] == 6
    assert con.execute("SELECT SUM(units) FROM sales WHERE sku='historical'").fetchone()[0] == 0


def test_cross_marketplace_snapshots_and_pending_blocks_are_isolated(con):
    with con:
        con.execute("INSERT INTO sku VALUES('CA','a','ca-family',NULL,1)")
        con.execute("INSERT INTO stock VALUES('CA','a',99,0,0,'2020-01-01T00:00:00Z')")
        con.execute("INSERT INTO sales VALUES('CA','a','2020-01-01',99)")
        con.execute("INSERT INTO data_quality_block VALUES('CA','a','ORDERS_PENDING_STATUS','retain')")
    reconcile(con)
    assert con.execute("SELECT fulfillable FROM stock WHERE marketplace='CA'").fetchone()[0] == 99
    assert con.execute("SELECT units FROM sales WHERE marketplace='CA'").fetchone()[0] == 99
    assert con.execute("SELECT COUNT(*) FROM data_quality_block WHERE marketplace='CA'").fetchone()[0] == 1
    with pytest.raises(ValueError, match='another API marketplace'):
        reconcile(con, collection=sales([order(marketplace='A2EUQ1WTGCTBG2')], marketplace='A2EUQ1WTGCTBG2'))


def test_write_failure_rolls_back_previous_snapshots(con):
    reconcile(con)
    previous_snapshots = con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
    previous_lines = [tuple(row) for row in con.execute('SELECT * FROM order_line')]
    with con:
        con.execute("CREATE TRIGGER reject_sales BEFORE INSERT ON sales BEGIN SELECT RAISE(ABORT,'PRIVATE-ERROR'); END")
    changed = inventory()
    changed['inventoryDetails']['fulfillableQuantity'] = 500
    with pytest.raises(CollectionQualityError) as error:
        reconcile(con, stock=[changed])
    assert error.value.code == 'LOCAL_RECONCILIATION_FAILED' and 'PRIVATE' not in str(error.value)
    assert con.execute("SELECT fulfillable FROM stock WHERE sku='a'").fetchone()[0] == 5
    assert con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == previous_snapshots
    assert [tuple(row) for row in con.execute('SELECT * FROM order_line')] == previous_lines


@pytest.mark.parametrize('kind', ['partial', 'no_audit', 'audit_gap', 'missing_items', 'missing_sku', 'ambiguous', 'duplicate_conflict'])
def test_incomplete_or_unusable_sales_cannot_replace_existing_data(con, kind):
    reconcile(con)
    data = sales()
    if kind == 'partial':
        data.complete = False
    elif kind == 'no_audit':
        data.audit = []
    elif kind == 'audit_gap':
        data.audit[0]['begin'] = '2000-01-01T00:00:00Z'
    elif kind == 'missing_items':
        data.items['o'] = []
    elif kind == 'missing_sku':
        data.items['o'][0].pop('SellerSKU')
    elif kind == 'ambiguous':
        data.items['o'][0]['CancellationAmbiguous'] = True
    elif kind == 'duplicate_conflict':
        data.items['o'].append(item('other'))
    with pytest.raises(CollectionQualityError):
        reconcile(con, collection=data)
    assert con.execute('SELECT SUM(units) FROM sales').fetchone()[0] == 3


def test_asin_identity_conflict_never_overwrites_human_asin(con):
    with con:
        con.execute("INSERT INTO sku VALUES('US','a','formal','B000000099',1)")
    result = reconcile(con)
    assert result['asin_conflict_sku_count'] == 1
    assert con.execute("SELECT asin FROM sku WHERE sku='a'").fetchone()[0] == 'B000000099'
    assert con.execute("SELECT code FROM data_quality_block WHERE sku='a'").fetchone()[0] == 'SKU_COLLECTION_ASIN_CONFLICT'


def test_snapshot_and_audit_whitelists_exclude_customer_fields(con):
    reconcile(con)
    persisted = '\n'.join(row['data'] for row in con.execute('SELECT data FROM snapshots'))
    persisted += '\n'.join(row['detail'] for row in con.execute('SELECT detail FROM audit'))
    assert 'PRIVATE-DO-NOT-PERSIST' not in persisted
    assert 'Buyer' not in persisted


class ProbeAPI:
    marketplace = MARKETPLACE
    endpoint = 'https://sellingpartnerapi-na.amazon.com'
    orders_version = '2026-01-01'

    def __init__(self):
        self.collection_complete = {}
        self.pagination_audit = []
        self.items = {'o': [item('historical')]}
        self.pending = True
        self.calls = []

    def inventory_summaries(self):
        self.calls.append('full_inventory')
        self.collection_complete['/fba/inventory/v1/summaries'] = True
        return [inventory()]

    def orders(self, begin, finish, **kwargs):
        self.calls.append('orders')
        assert kwargs == {'max_pages': 2, 'max_results': 25}
        self.collection_complete['/orders/2026-01-01/orders'] = False
        return [order(status='Pending' if self.pending else 'Shipped')]

    def order_items(self, identity):
        return deepcopy(self.items[identity])

    def _pages(self, path, params, rows_key, **kwargs):
        self.calls.append('inbound_one_page')
        assert path == '/fba/inbound/v0/shipments' and rows_key == 'ShipmentData'
        assert kwargs == {'query_type': True, 'max_pages': 1}
        self.collection_complete[path] = False
        return [{'ShipmentId': 's', 'ShipmentStatus': 'SHIPPED'}]

    def shipment_items(self, identity):
        self.calls.append('inbound_items_one_shipment')
        return [{'ShipmentId': identity, 'SellerSKU': 'a', 'QuantityShipped': 7,
                 'QuantityReceived': 3, 'FulfillmentNetworkSKU': 'fn-a'}]


def test_precheck_known_pending_and_historical_inventory_missing_are_soft(tmp_path):
    api = ProbeAPI()
    report, rows, audit = precheck_sku(api, tmp_path, ASOF, 'UTC')
    assert report['expansion_allowed'] and not report['hard_errors']
    assert report['pending_sku_count'] == 1
    assert report['order_skus_without_current_inventory_count'] == 1
    assert not report['orders_complete'] and not report['complete_snapshot']
    assert report['inbound_items_permission'] == 'PASSED'
    assert api.calls == ['full_inventory', 'orders', 'inbound_one_page', 'inbound_items_one_shipment']
    assert audit['complete'] and len(rows) == 1
    assert not any(key in json.dumps(report) for key in ('PRIVATE-DO-NOT-PERSIST', 'AmazonOrderId', 'ShipmentId'))


@pytest.mark.parametrize('kind,code', [('missing_items', 'ORDER_ITEMS_MISSING'),
                                      ('missing_sku', 'MISSING_ORDER_ITEM_SKU'),
                                      ('ambiguous', 'AMBIGUOUS_ITEM_CANCELLATION')])
def test_precheck_hard_order_errors_prevent_expansion_and_inbound_calls(tmp_path, kind, code):
    api = ProbeAPI()
    if kind == 'missing_items':
        api.items['o'] = []
    elif kind == 'missing_sku':
        api.items['o'][0].pop('SellerSKU')
    else:
        api.items['o'][0]['CancellationAmbiguous'] = True
    report, _, _ = precheck_sku(api, tmp_path, ASOF, 'UTC')
    assert not report['expansion_allowed']
    assert report['hard_errors'] == [{'stage': 'orders', 'code': code}]
    assert 'inbound_one_page' not in api.calls


def test_inbound_missing_quantity_prevents_precheck_expansion(tmp_path):
    api = ProbeAPI()
    api.shipment_items = lambda identity: [{'ShipmentId': identity, 'SellerSKU': 'a', 'QuantityShipped': 7}]
    report, _, _ = precheck_sku(api, tmp_path, ASOF, 'UTC')
    assert not report['expansion_allowed']
    assert report['hard_errors'][0]['code'] == 'MISSING_SHIPMENT_RECEIVED_QUANTITY'


def test_active_inbound_lines_eta_preserved_without_counting_inventory_counters(con):
    with con:
        con.execute("INSERT INTO inbound VALUES('US','a','s',100,'2030-01-01','SHIPPED')")
    summary = reconcile(con, shipments=[{'ShipmentId': 's', 'ShipmentStatus': 'SHIPPED'}],
                        shipment_items={'s': [{'ShipmentId': 's', 'SellerSKU': 'a',
                                               'QuantityShipped': 8, 'QuantityReceived': 3}]})
    assert summary['inbound_rows'] == 1
    assert con.execute('SELECT units,eta FROM inbound').fetchone()[:] == (5, '2030-01-01')
    assert con.execute("SELECT fulfillable,reserved FROM stock WHERE sku='a'").fetchone()[:] == (5, 2)
