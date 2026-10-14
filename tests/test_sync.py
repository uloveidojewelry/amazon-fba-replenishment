from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fba_replenishment.core import calculate, connect
from fba_replenishment.spapi import SPAPIError
from fba_replenishment.sync import sync

ASOF = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()


def inventory(sku='a', fulfillable=5, fnsku='fn1'):
    return {'sellerSku': sku, 'fnSku': fnsku, 'condition': 'NewItem', 'inventoryDetails': {
        'fulfillableQuantity': fulfillable,
        'reservedQuantity': {'totalReservedQuantity': 2},
        'unfulfillableQuantity': {'totalUnfulfillableQuantity': 1},
        'inboundShippedQuantity': 99,
    }}


class API:
    marketplace = 'ATVPDKIKX0DER'
    pending = False
    stock = None
    shipments = None

    def inventory_summaries(self):
        return self.stock if self.stock is not None else [inventory(), inventory(fnsku='fn2', fulfillable=3)]

    def orders(self, begin, end):
        self.window = (begin, end)
        return [
            {'AmazonOrderId': 'order', 'PurchaseDate': ASOF + 'T12:00:00Z',
             'OrderStatus': 'Pending' if self.pending else 'Shipped',
             'FulfillmentChannel': 'AFN', 'MarketplaceId': self.marketplace},
            {'AmazonOrderId': 'cancelled', 'PurchaseDate': ASOF + 'T12:00:00Z',
             'OrderStatus': 'Canceled', 'FulfillmentChannel': 'AFN', 'MarketplaceId': self.marketplace},
        ]

    def order_items(self, order):
        assert order == 'order'
        return [{'OrderItemId': 'item', 'SellerSKU': 'a', 'QuantityOrdered': 30,
                 'QuantityShipped': 30, 'BuyerInfo': 'never-persist'}]

    def inbound_shipments(self):
        return self.shipments if self.shipments is not None else [
            {'ShipmentId': 's1', 'ShipmentStatus': 'SHIPPED'},
            {'ShipmentId': 's2', 'ShipmentStatus': 'CLOSED'},
        ]

    def shipment_items(self, shipment):
        return [{'ShipmentId': shipment, 'SellerSKU': 'a', 'QuantityShipped': 10, 'QuantityReceived': 4}]


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / 'sync.db')
    with c:
        c.execute("INSERT INTO sku(marketplace,sku,family) VALUES('US','a','f')")
        c.execute("INSERT INTO sku(marketplace,sku,family) VALUES('US','b','f')")
        c.execute("INSERT INTO stock VALUES('US','absent',99,0,0,'2020-01-01T00:00:00Z')")
        c.execute("INSERT INTO stock VALUES('CA','a',99,0,0,'2020-01-01T00:00:00Z')")
    yield c
    c.close()


def test_full_sync_idempotent_zeros_history_and_eta(con):
    api = API()
    with con:
        con.execute('INSERT INTO inbound VALUES(?,?,?,?,?,?)', ('US', 'a', 's1', 10, ASOF, 'SHIPPED'))
    counts = sync(con, api, 'US', asof=ASOF)
    assert counts == {'stock': 1, 'sales': 60, 'inbound': 2}
    stock = con.execute("SELECT * FROM stock WHERE marketplace='US'").fetchone()
    assert stock['fulfillable'] == 8 and stock['reserved'] == 4
    assert con.execute("SELECT COUNT(*) FROM sales WHERE marketplace='US'").fetchone()[0] == 60
    assert con.execute("SELECT SUM(units) FROM sales WHERE marketplace='US' AND sku='a'").fetchone()[0] == 30
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 1
    assert con.execute("SELECT fulfillable FROM stock WHERE marketplace='CA'").fetchone()[0] == 99
    results = {r['sku']: r for r in calculate(con, asof=ASOF)}
    assert results['a']['eligible_inbound'] == 6
    assert results['a']['recommended_qty'] == 96
    assert results['b']['status'] == 'MISSING_STOCK'
    sync(con, api, 'US', asof=ASOF)
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 1
    assert con.execute("SELECT SUM(units) FROM sales WHERE marketplace='US'").fetchone()[0] == 30
    assert con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 10
    assert 'never-persist' not in ''.join(r['data'] for r in con.execute('SELECT data FROM snapshots'))


def test_failure_preserves_all_previous_data_and_blocks_plan(con):
    api = API()
    sync(con, api, 'US', asof=ASOF)
    api.pending = True
    api.stock = [inventory(fulfillable=500)]
    with pytest.raises(SPAPIError, match='Unresolved'):
        sync(con, api, 'US', asof=ASOF)
    assert con.execute("SELECT fulfillable FROM stock WHERE marketplace='US'").fetchone()[0] == 8
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 1
    assert all(r['status'] == 'SYNC_FAILED' and r['recommended_qty'] == 0 for r in calculate(con, asof=ASOF))
    api.pending = False
    sync(con, api, 'US', asof=ASOF)
    assert calculate(con, asof=ASOF)[0]['status'] == 'INBOUND_REVIEW_REQUIRED'


def test_invalid_inventory_never_assumes_zero(con):
    api = API()
    api.stock = [{'sellerSku': 'a', 'inventoryDetails': {'reservedQuantity': {}}}]
    with pytest.raises(ValueError):
        sync(con, api, 'US', kind='stock')
    assert con.execute("SELECT sku FROM stock WHERE marketplace='US'").fetchone()[0] == 'absent'
    api.stock = [inventory(), inventory()]
    with pytest.raises(SPAPIError, match='Duplicate inventory'):
        sync(con, api, 'US', kind='stock')


def test_empty_full_stock_clears_only_selected_marketplace(con):
    api = API()
    api.stock = []
    sync(con, api, 'US', kind='stock')
    assert con.execute("SELECT COUNT(*) FROM stock WHERE marketplace='US'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM stock WHERE marketplace='CA'").fetchone()[0] == 1


def test_shipment_reconciliation_removes_disappeared_and_terminal_not_counted(con):
    api = API()
    sync(con, api, 'US', asof=ASOF)
    assert calculate(con, asof=ASOF)[0]['status'] == 'INBOUND_REVIEW_REQUIRED'
    api.shipments = [{'ShipmentId': 's2', 'ShipmentStatus': 'CLOSED'}]
    sync(con, api, 'US', asof=ASOF)
    row = calculate(con, asof=ASOF)[0]
    assert row['status'] == 'ACTIVE' and row['eligible_inbound'] == 0
    assert row['recommended_qty'] == 102
    assert con.execute("SELECT COUNT(*) FROM inbound WHERE shipment_id='s1'").fetchone()[0] == 0


def test_marketplace_day_buckets_and_createdbefore_boundary(con):
    class ZonedAPI(API):
        def orders(self, begin, end):
            self.window = begin, end
            day = datetime.fromisoformat(ASOF).replace(tzinfo=ZoneInfo('America/Los_Angeles'))
            return [dict(AmazonOrderId='order', PurchaseDate=(day + timedelta(hours=23)).astimezone(timezone.utc).isoformat(), OrderStatus='Shipped', FulfillmentChannel='AFN', MarketplaceId=self.marketplace)]
    api = ZonedAPI()
    sync(con, api, 'US', kind='sales', asof=ASOF, timezone_name='America/Los_Angeles')
    assert con.execute('SELECT day FROM order_line').fetchone()[0] == ASOF
    assert api.window[1].endswith('+00:00')
    with pytest.raises(ValueError, match='completed day'):
        sync(con, api, 'US', kind='sales', asof=(datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(), timezone_name='America/Los_Angeles')


def test_namespace_binding_prevents_cross_marketplace_and_timezone_mix(con):
    api = API()
    sync(con, api, 'US', asof=ASOF)
    api.marketplace = 'A2EUQ1WTGCTBG2'
    with pytest.raises(ValueError, match='different API marketplace'):
        sync(con, api, 'US', kind='stock')
    api.marketplace = 'ATVPDKIKX0DER'
    with pytest.raises(ValueError, match='timezone changed'):
        sync(con, api, 'US', kind='sales', asof=ASOF, timezone_name='America/Los_Angeles')
    assert con.execute('SELECT sales_timezone FROM marketplace_binding').fetchone()[0] == 'UTC'


def test_canceled_orders_reconcile_existing_sales(con):
    api = API()
    sync(con, api, 'US', kind='sales', asof=ASOF)
    class CanceledAPI(API):
        def orders(self, begin, end):
            return [dict(AmazonOrderId='order', PurchaseDate=ASOF+'T12:00:00Z', OrderStatus='Canceled', FulfillmentChannel='AFN', MarketplaceId=self.marketplace)]
    sync(con, CanceledAPI(), 'US', kind='sales', asof=ASOF)
    assert con.execute('SELECT SUM(units) FROM sales').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM order_line').fetchone()[0] == 0


def test_missing_received_quantity_and_duplicate_item_roll_back(con):
    class MissingInbound(API):
        def shipment_items(self, shipment):
            return [dict(ShipmentId=shipment, SellerSKU='a', QuantityShipped=5)]
    with pytest.raises(ValueError, match='Missing'):
        sync(con, MissingInbound(), 'US', kind='inbound')
    class DuplicateItem(API):
        def order_items(self, order):
            return super().order_items(order) * 2
    with pytest.raises(SPAPIError, match='Duplicate order item'):
        sync(con, DuplicateItem(), 'US', kind='sales', asof=ASOF)
    assert con.execute('SELECT COUNT(*) FROM sales').fetchone()[0] == 0
