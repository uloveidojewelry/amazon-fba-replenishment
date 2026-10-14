import json
from datetime import datetime, timedelta

from fba_replenishment.core import connect
from fba_replenishment.discovery import discover
from fba_replenishment.review_queue import build_review_queue, naming_candidate

US='ATVPDKIKX0DER'
STAMP='2026-10-14T09:40:00+00:00'


def inventory(sku, n=1):
    return {'sellerSku':sku,'asin':'B000000001','fnSku':'FN-'+sku,'condition':'NewItem',
            'inventoryDetails':{'fulfillableQuantity':n,'reservedQuantity':{'totalReservedQuantity':0},
            'unfulfillableQuantity':{'totalUnfulfillableQuantity':0},'inboundWorkingQuantity':0,
            'inboundShippedQuantity':0,'inboundReceivingQuantity':0}}


def setup(tmp_path, skus, source=None, stock=1):
    con=connect(tmp_path/'review.db')
    discover(con,[inventory(s,stock) for s in skus],'US',US,source_candidates=source,observed_at=STAMP)
    with con:
        con.execute("UPDATE marketplace_binding SET sales_timezone='America/Los_Angeles'")
        con.execute('CREATE TABLE inventory_details(marketplace TEXT,sku TEXT,inbound_working INTEGER,inbound_shipped INTEGER,inbound_receiving INTEGER,observed_at TEXT)')
        for sku in skus:
            con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)',('US',sku,stock,0,0,STAMP))
            con.execute('INSERT INTO inventory_details VALUES(?,?,?,?,?,?)',('US',sku,0,0,0,STAMP))
    return con


def test_fullmatch_candidates_do_not_split_first_hyphen():
    assert naming_candidate('X001-Blue-7')['candidate_family']=='X001'
    assert naming_candidate('LCS-20261001-YA1234-Blue-7')['candidate_family']=='YA1234'
    assert naming_candidate('LCS-20261399-YA1234-Blue-7')['candidate_family'] is None
    assert naming_candidate('arbitrary-one-two')['candidate_family'] is None
    assert naming_candidate('A4-5B-1Q-2W')['candidate_family'] is None
    assert naming_candidate('X001-Blue-7-unrecognized')['candidate_family'] is None


def test_exact_reference_group_reduces_actions_but_never_approves(tmp_path):
    skus=['SKU-1','SKU-2','weird-unknown']
    source={s:{'candidate_family':'Series','basis':'USER_WORKBOOK_EXACT_SKU'} for s in skus[:2]}
    con=setup(tmp_path,skus,source)
    q=build_review_queue(con,observed_at=STAMP,asof='2026-10-13')
    assert q['summary']['exact_match_sku_count']==2
    assert q['summary']['exact_match_group_count']==1
    assert q['summary']['exact_actions_saved']==1
    assert q['summary']['batch_review_count']==2
    assert con.execute('SELECT COUNT(*) FROM discovery_sku WHERE mapping_status=\'APPROVED\'').fetchone()[0]==0
    assert all(r['listing_status']=='UNKNOWN' and r['recommended_qty']==0 for r in q['items'])
    assert [g['batch_eligible'] for g in q['groups']].count(True)==1
    con.close()


def test_format_warning_is_single_exception_not_a_bulk_approval(tmp_path):
    con=setup(tmp_path,['bad','good'],{'bad':{'candidate_family':'F','basis':'USER_WORKBOOK_EXACT_SKU','format_issue':'needs checking'},
                                     'good':{'candidate_family':'F','basis':'USER_WORKBOOK_EXACT_SKU'}})
    q=build_review_queue(con,observed_at=STAMP,asof='2026-10-13')
    assert q['summary']['exact_exception_sku_count']==1
    assert q['summary']['exact_review_action_count']==2
    assert any(g['sku_count']==1 and not g['batch_eligible'] for g in q['groups'])
    con.close()


def test_sales_sample_absence_and_cancelled_lines_are_unknown(tmp_path):
    con=setup(tmp_path,['sold','cancelled','absent'],stock=0)
    sample={'timezone':'America/Los_Angeles','orders':[
        {'AmazonOrderId':'o','MarketplaceId':US,'FulfillmentChannel':'AMAZON','OrderStatus':'SHIPPED','PurchaseDate':'2026-10-07T12:00:00Z'},
        {'AmazonOrderId':'c','MarketplaceId':US,'FulfillmentChannel':'AMAZON','OrderStatus':'CANCELLED','PurchaseDate':'2026-10-07T12:00:00Z'}],
        'items':{'o':[{'SellerSKU':'sold','OrderItemId':'i','QuantityShipped':2}],
                 'c':[{'SellerSKU':'cancelled','OrderItemId':'j','QuantityShipped':0,'QuantityOrdered':5}]}}
    q=build_review_queue(con,sample=sample,observed_at=STAMP,asof='2026-10-13')
    records={r['sku']:r for r in q['items']}
    assert records['sold']['recent_sales_status']=='POSITIVE_SAMPLE_ONLY'
    assert records['sold']['priority']=='P0'
    for sku in ('cancelled','absent'):
        assert records[sku]['recent_sales_status']=='UNKNOWN'
        assert records[sku]['recent_units_observed'] is None
        assert records[sku]['listing_status']=='UNKNOWN'
    con.close()


def test_opaque_formats_group_for_triage_without_workload_claim(tmp_path):
    con=setup(tmp_path,['A4-5B-1Q-2W','AB-CD-EF-GH','Amazon.Found.unrecognized'],stock=0)
    q=build_review_queue(con,observed_at=STAMP,asof='2026-10-13')
    assert q['summary']['batch_review_count']==3
    assert q['summary']['review_actions_saved']==0
    assert all(not r['batch_eligible'] for r in q['groups'])
    assert all(r['history_status']!='INACTIVE' for r in q['items'])
    con.close()


def test_stale_inventory_does_not_claim_current_fba_stock(tmp_path):
    con=setup(tmp_path,['SKU'],stock=10)
    with con:con.execute("UPDATE stock SET observed_at='2020-01-01T00:00:00+00:00'")
    q=build_review_queue(con,observed_at=STAMP,asof='2026-10-13')
    assert q['items'][0]['fba_stock_status']=='UNKNOWN'
    assert q['items'][0]['listing_status']=='UNKNOWN'
    con.close()


def test_old_inbound_detail_does_not_become_current_inbound_risk(tmp_path):
    con=setup(tmp_path,['SKU'],stock=0)
    with con:con.execute("UPDATE inventory_details SET inbound_shipped=50,observed_at='2020-01-01T00:00:00+00:00'")
    q=build_review_queue(con,observed_at=STAMP,asof='2026-10-13')
    assert q['items'][0]['fba_stock_status']=='UNKNOWN'
    assert q['items'][0]['inbound_shipped'] is None
    assert 'POSITIVE_INBOUND_ONLY' not in q['items'][0]['priority_reasons']
    con.close()
