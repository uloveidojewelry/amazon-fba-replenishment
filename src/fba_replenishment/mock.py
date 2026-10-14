"""Synthetic store fixtures. No credentials, HTTP transport or real seller data."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import US_MARKETPLACE
from .core import stop

MOCK_SKUS = ('J001-size 5', 'J001-size 6', 'J001-size 7', 'J002-size 5')


class MockSPAPIClient:
    marketplace = US_MARKETPLACE
    data_source = 'MOCK'

    def __init__(self, timezone_name='UTC'):
        self.zone = ZoneInfo(timezone_name)
        self._items = {}

    def inventory_summaries(self):
        return [{'sellerSku': sku, 'fnSku': 'SIMULATED-' + str(index), 'condition': 'NewItem',
                 'inventoryDetails': {'fulfillableQuantity': 8,
                                      'reservedQuantity': {'totalReservedQuantity': 2},
                                      'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0}}}
                for index, sku in enumerate(MOCK_SKUS)]

    def orders(self, begin, end):
        first = datetime.fromisoformat(begin).astimezone(self.zone).date()
        finish = datetime.fromisoformat(end).astimezone(self.zone).date()
        result, self._items = [], {}
        for offset in range((finish - first).days):
            day = first + timedelta(days=offset)
            for index, sku in enumerate(MOCK_SKUS):
                order_id = f'SIMULATED-{day}-{index}'
                result.append({'AmazonOrderId': order_id, 'PurchaseDate': datetime.combine(day, time(12), self.zone).isoformat(),
                               'OrderStatus': 'Shipped', 'FulfillmentChannel': 'AFN', 'MarketplaceId': self.marketplace})
                self._items[order_id] = [{'OrderItemId': 'SIMULATED-ITEM', 'SellerSKU': sku,
                                         'QuantityOrdered': 3, 'QuantityShipped': 3}]
        return result

    def order_items(self, order_id):
        return self._items[order_id]

    def inbound_shipments(self):
        return [{'ShipmentId': 'SIMULATED-SHIPMENT', 'ShipmentStatus': 'SHIPPED',
                 'DestinationFulfillmentCenterId': 'SIMULATED-FC'}]

    def shipment_items(self, shipment_id):
        return [{'ShipmentId': shipment_id, 'SellerSKU': 'J002-size 5',
                 'QuantityShipped': 12, 'QuantityReceived': 6}]


def is_mock_database(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='environment_mode'").fetchone():
        return False
    return con.execute("SELECT 1 FROM environment_mode WHERE mode='mock'").fetchone() is not None


def prepare_mock_database(con, marketplace, asof):
    if is_mock_database(con):
        return
    for table in ('sku', 'sales', 'stock', 'inbound', 'audit', 'sync_log', 'snapshots', 'order_line', 'marketplace_binding'):
        if con.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
            raise ValueError('Mock data requires a new empty database or an existing mock database')
    with con:
        con.execute("CREATE TABLE environment_mode (mode TEXT PRIMARY KEY CHECK(mode='mock'))")
        con.execute("INSERT INTO environment_mode VALUES('mock')")
        for sku in MOCK_SKUS:
            con.execute('INSERT INTO sku(marketplace,sku,family,asin) VALUES(?,?,?,?)',
                        (marketplace, sku, sku.split('-')[0], ''))
        con.execute('INSERT INTO inbound VALUES(?,?,?,?,?,?)',
                    (marketplace, 'J002-size 5', 'SIMULATED-SHIPMENT', 6,
                     (asof + timedelta(days=4)).isoformat(), 'SHIPPED'))
    stop(con, marketplace, 'J001-size 5', 'SIMULATED fixture: family stopped')
