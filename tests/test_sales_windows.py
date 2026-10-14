from datetime import datetime, timedelta, timezone

from fba_replenishment.core import calculate, connect
from fba_replenishment.mock import MockSPAPIClient
from fba_replenishment.sync import collect_sales
from fba_replenishment.spapi import SPAPIError
import pytest


def test_complete_90_day_calendar_does_not_double_count_orders():
    asof = datetime.now(timezone.utc).date() - timedelta(days=2)
    totals, lines, start = collect_sales(MockSPAPIClient(), ['J001-size 5'], asof, 'UTC', days=90)
    assert start == asof - timedelta(days=89)
    assert len(lines) == 4 * 90
    assert sum(n for (sku, _), n in totals.items() if sku == 'J001-size 5') == 270


def test_partial_sales_never_show_partial_total_as_30_day_demand(tmp_path):
    con = connect(tmp_path / 'quality.db')
    asof = datetime.now(timezone.utc).date() - timedelta(days=2)
    with con:
        con.execute("INSERT INTO sku(marketplace,sku,family) VALUES('US','a','f')")
        con.execute('INSERT INTO sales VALUES(?,?,?,?)', ('US', 'a', asof.isoformat(), 9))
        con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)', ('US','a',0,0,0,datetime.now(timezone.utc).isoformat()))
    row = calculate(con, asof=asof.isoformat())[0]
    assert row['sales_30d'] is None and row['daily_sales'] is None and row['coverage_days'] is None
    assert row['recommended_qty'] == 0
    con.close()


@pytest.mark.parametrize('extra,code', [({'CancellationAmbiguous': True}, 'AMBIGUOUS_ITEM_CANCELLATION'),
                                      ({'QuantityShipped': 5}, 'INVALID_ORDER_QUANTITIES')])
def test_direct_sales_sync_rejects_ambiguous_or_inconsistent_quantities(extra, code):
    asof = datetime.now(timezone.utc).date() - timedelta(days=2)
    class Client:
        marketplace = 'ATVPDKIKX0DER'
        def orders(self, begin, end):
            return [{'AmazonOrderId':'test','PurchaseDate':asof.isoformat()+'T12:00:00Z',
                     'OrderStatus':'Shipped','FulfillmentChannel':'AFN','MarketplaceId':self.marketplace}]
        def order_items(self, oid):
            return [{'OrderItemId':'i','SellerSKU':'a','QuantityOrdered':2, **extra}]
    with pytest.raises(SPAPIError) as error:
        collect_sales(Client(),['a'],asof,'UTC')
    assert error.value.code == code
