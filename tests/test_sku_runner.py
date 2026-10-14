from hashlib import sha256
import json
from pathlib import Path

import pytest

from fba_replenishment.core import connect
from fba_replenishment.sku_runner import open_sku_database, run_sku_collection, apply_latest_source_stops
from fba_replenishment.collection import CollectionQualityError


def test_independent_database_preserves_original_mapping_and_stop(tmp_path):
    source=tmp_path/'review.db'
    con=connect(source)
    with con:
        con.execute("INSERT INTO sku VALUES('US','sku-a','family-a','B000000001',0)")
        con.execute("INSERT INTO stop_flag VALUES('US','sku-a','manual','2026-01-01',1)")
    con.close()
    before=sha256(source.read_bytes()).hexdigest()
    dest=tmp_path/'independent.db'
    copied=open_sku_database(dest,source)
    assert copied.execute("SELECT family,enabled FROM sku WHERE sku='sku-a'").fetchone()['family']=='family-a'
    assert copied.execute('SELECT active FROM stop_flag').fetchone()['active']==1
    copied.close()
    assert sha256(source.read_bytes()).hexdigest()==before
    # Resuming must retain the current local state rather than recopy old data.
    copied=open_sku_database(dest,source)
    with copied:copied.execute("INSERT INTO sku VALUES('US','sku-b',NULL,NULL,1)")
    copied.close()
    copied=open_sku_database(dest,source)
    assert copied.execute('SELECT COUNT(*) FROM sku').fetchone()[0]==2
    copied.close()


def test_original_database_cannot_be_used_as_independent_store(tmp_path):
    with pytest.raises(ValueError,match='separate'):
        open_sku_database(tmp_path/'same.db',tmp_path/'same.db')
    assert not (tmp_path/'same.db').exists()


class OfflineClient:
    marketplace='ATVPDKIKX0DER'
    session=object()


def test_failed_precheck_never_expands_or_creates_database(tmp_path,monkeypatch):
    import fba_replenishment.sku_collection as collection
    monkeypatch.setattr(collection,'precheck_sku',lambda *args:({'expansion_allowed':False},[],{}))
    def unexpected(*args,**kwargs):
        pytest.fail('Sales collection must not run after a failed precheck')
    monkeypatch.setattr('fba_replenishment.sku_runner.collect_sales_shards',unexpected)
    db=tmp_path/'plan.db';dest=tmp_path/'report'
    with pytest.raises(CollectionQualityError) as error:
        run_sku_collection(OfflineClient(),db,dest,asof='2026-01-01')
    assert error.value.code=='PRECHECK_NOT_READY'
    assert not db.exists()
    failure=json.loads((dest/'failure.json').read_text())
    assert failure['code']=='PRECHECK_NOT_READY' and not failure['full_sales_window_certified']


def test_precheck_only_never_collects_90_days(tmp_path,monkeypatch):
    import fba_replenishment.sku_collection as collection
    monkeypatch.setattr(collection,'precheck_sku',lambda *args:({'expansion_allowed':True,'orders':2,'order_items':2},[],{'skus':3}))
    monkeypatch.setattr('fba_replenishment.sku_runner.collect_sales_shards',lambda *a,**k:pytest.fail('Precheck only cannot collect history'))
    result=run_sku_collection(OfflineClient(),tmp_path/'plan.db',tmp_path/'out',asof='2026-01-01',precheck_only=True)
    assert result['status']=='PRECHECK_PASSED' and not result['full_collection_executed']
    assert not (tmp_path/'plan.db').exists()


def test_invalid_parameters_fail_before_any_request(tmp_path,monkeypatch):
    monkeypatch.setattr('fba_replenishment.sku_collection.precheck_sku',lambda *a:pytest.fail('Invalid settings must not call Amazon'))
    with pytest.raises(ValueError):
        run_sku_collection(OfflineClient(),tmp_path/'plan.db',tmp_path/'out',settings={'lead_days':-1})


def test_cli_live_confirmation_required_before_client_creation(monkeypatch):
    from fba_replenishment.cli import main
    monkeypatch.setattr('sys.argv',['fba-plan','sku-sync-plan'])
    monkeypatch.setattr('fba_replenishment.cli.SPAPIClient',lambda *a,**k:pytest.fail('Cannot create a live client without confirmation'))
    with pytest.raises(SystemExit) as error:main()
    assert error.value.code==2


def test_new_original_sku_stop_is_observed_without_changing_source_or_family(tmp_path):
    source=tmp_path/'review.db';con=connect(source)
    with con:
        con.execute("INSERT INTO sku VALUES('US','a','same-family',NULL,1)")
        con.execute("INSERT INTO sku VALUES('US','b','same-family',NULL,1)")
        con.execute("INSERT INTO stop_flag VALUES('US','a','new explicit stop','2026-01-01',1)")
        con.execute("INSERT INTO family_stop VALUES('US','same-family','a',1)")
    con.close();before=sha256(source.read_bytes()).hexdigest()
    rows=[{'marketplace':'US','sku':s,'status':'SCENARIO_ONLY','recommended_qty':12} for s in ('a','b')]
    result=apply_latest_source_stops(rows,source)
    assert result[0]['status']=='STOPPED' and result[0]['recommended_qty']==0
    assert result[1]['recommended_qty']==12
    assert sha256(source.read_bytes()).hexdigest()==before


def test_source_overlay_never_resumes_an_existing_independent_sku_stop(tmp_path):
    source=tmp_path/'review.db';con=connect(source);con.close()
    rows=[{'marketplace':'US','sku':'stopped','status':'STOPPED','recommended_qty':0}]
    assert apply_latest_source_stops(rows,source)[0]['status']=='STOPPED'
