from datetime import datetime, timezone

import pytest

from fba_replenishment.core import connect
from fba_replenishment.discovery import discover
from fba_replenishment.discovery_runner import run_discovery
from fba_replenishment.spapi import SPAPIError
from fba_replenishment.sync import sync


def inventory(asin='B000000001'):
    return {'sellerSku':'known','asin':asin,'fnSku':'FN1','condition':'NewItem',
            'inventoryDetails': {'fulfillableQuantity':1,'reservedQuantity':{'totalReservedQuantity':0},
            'unfulfillableQuantity':{'totalUnfulfillableQuantity':0},'inboundWorkingQuantity':0,
            'inboundShippedQuantity':0,'inboundReceivingQuantity':0}}


def test_daily_command_skips_without_network_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv('SP_API_MARKETPLACE_ID','ATVPDKIKX0DER')
    monkeypatch.setenv('SP_API_ENDPOINT','https://sellingpartnerapi-na.amazon.com')
    monkeypatch.setenv('SP_API_TIMEZONE','UTC')
    monkeypatch.setenv('SP_API_ORDERS_VERSION','2026-01-01')
    con = connect(tmp_path / 'daily.db')
    discover(con,[inventory()],'US','ATVPDKIKX0DER',business_day=datetime.now(timezone.utc).date().isoformat())
    class NoRequests:
        def inventory_summaries(self): raise AssertionError('Daily guard must precede network')
    result = run_discovery(con,tmp_path,daily=True,client=NoRequests())
    assert result['status']=='SKIPPED_ALREADY_COMPLETED'
    assert con.execute('SELECT COUNT(*) FROM discovery_run').fetchone()[0] == 1
    con.close()


def test_failed_discovery_rolls_back_sync_stock_and_retains_history(tmp_path):
    con = connect(tmp_path / 'sync.db')
    discover(con,[inventory()],'US','ATVPDKIKX0DER')
    with con:
        con.execute('INSERT INTO stock VALUES(?,?,?,?,?,?)',('US','known',99,0,0,datetime.now(timezone.utc).isoformat()))
    class Broken:
        supports_discovery=True
        marketplace='ATVPDKIKX0DER'
        def inventory_summaries(self): return [inventory(None)]
    with pytest.raises(ValueError,match='ASIN'):
        sync(con,Broken(),'US',kind='stock')
    assert con.execute('SELECT fulfillable FROM stock').fetchone()[0] == 99
    assert con.execute('SELECT COUNT(*) FROM discovery_sku').fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert con.execute("SELECT status FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()[0] == 'FAILED'
    con.close()
