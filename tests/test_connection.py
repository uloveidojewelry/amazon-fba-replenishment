"""All connection checks use fake transports or in-memory synthetic stores."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from fba_replenishment.cli import main
from fba_replenishment.config import SPAPIConfig, US_MARKETPLACE, NA_ENDPOINT
from fba_replenishment.core import calculate, connect
from fba_replenishment.diagnostics import diagnose, diagnostic_exit_code
from fba_replenishment.mock import MockSPAPIClient, prepare_mock_database, is_mock_database
from fba_replenishment.spapi import SPAPIClient, SPAPIError
from fba_replenishment.sync import sync


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real HTTP request forbidden in connection tests')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    for name in ['SP_API_LWA_CLIENT_ID', 'SP_API_LWA_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN',
                 'SP_API_ENDPOINT', 'SP_API_MARKETPLACE_ID', 'SP_API_TIMEZONE', 'SP_API_ORDERS_VERSION']:
        monkeypatch.delenv(name, raising=False)


def env():
    return {'SP_API_LWA_CLIENT_ID': 'FAKE-CLIENT', 'SP_API_LWA_CLIENT_SECRET': 'DO-NOT-LOG-SECRET',
            'SP_API_REFRESH_TOKEN': 'DO-NOT-LOG-REFRESH'}


def bindings(monkeypatch):
    for key, value in env().items():
        monkeypatch.setenv(key, value)


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self.data, self.headers = status, data, headers or {}

    def json(self):
        return self.data


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def token():
    return Response(200, {'access_token': 'FAKE-ACCESS', 'expires_in': 3600})


def order(order_id='one', cancelled=False):
    return {'orderId': order_id, 'createdTime': '2026-09-01T12:00:00Z',
            'salesChannel': {'marketplaceId': US_MARKETPLACE},
            'fulfillment': {'fulfillmentStatus': 'SHIPPED', 'fulfilledBy': 'AMAZON'},
            'buyer': {'buyerEmail': 'DO-NOT-LOG-PII'}, 'recipient': {'address': 'DO-NOT-LOG-PII'},
            'orderItems': [{'orderItemId': 'item', 'quantityOrdered': 3,
                            'product': {'sellerSku': 'J001-size 5', 'asin': 'TEST-ASIN'},
                            'cancellation': {'cancellationExecution': {'cancelledBy': 'AMAZON'}} if cancelled else {},
                            'fulfillment': {'quantityFulfilled': 3, 'giftMessage': 'DO-NOT-LOG-PII'}}]}


def test_offline_missing_bindings_no_requests_no_database():
    report = diagnose(environ={})
    assert report['mode'] == 'offline' and report['configuration_valid']
    assert not report['configuration_complete'] and not report['authentication_verified']
    assert not report['network_requests_attempted']
    assert set(report['credentials'].values()) == {'missing'}
    assert report['configuration']['endpoint'] == NA_ENDPOINT
    assert diagnostic_exit_code(report) == 0  # Offline validity is not live readiness.


def test_present_credentials_not_printed_or_authenticated():
    report = diagnose(environ=env())
    text = json.dumps(report)
    assert all(secret not in text for secret in env().values())
    assert report['configuration_complete'] and not report['authentication_verified']
    assert set(report['credentials'].values()) == {'present_unverified'}


@pytest.mark.parametrize('field,value', [
    ('SP_API_ENDPOINT', 'https://sellingpartnerapi-eu.amazon.com'),
    ('SP_API_ENDPOINT', 'https://user:DO-NOT-LOG-SECRET@evil.example'),
    ('SP_API_TIMEZONE', 'DO-NOT-LOG-SECRET'),
    ('SP_API_MARKETPLACE_ID', ''),
    ('SP_API_ORDERS_VERSION', 'not-supported'),
])
def test_invalid_configuration_safe(field, value):
    report = diagnose(environ={field: value})
    assert diagnostic_exit_code(report) == 1
    assert not report['network_requests_attempted']
    assert 'DO-NOT-LOG-SECRET' not in json.dumps(report)


def test_us_settings_and_explicit_timezone():
    config = SPAPIConfig.from_env({'SP_API_TIMEZONE': 'America/Los_Angeles'})
    assert (config.marketplace, config.endpoint, config.timezone, config.orders_version) == (
        US_MARKETPLACE, NA_ENDPOINT, 'America/Los_Angeles', '2026-01-01')


def test_mock_diagnostics_without_credentials():
    report = diagnose(mock=True, environ={})
    assert diagnostic_exit_code(report) == 0
    assert not report['authentication_verified'] and not report['network_requests_attempted']
    assert report['checks'][0]['row_counts'] == {'stock': 4, 'sales': 120, 'inbound': 1}
    assert report['checks'][0]['status'] == 'passed'


def test_live_confirmation_and_missing_credentials_block_factory():
    def factory(**kwargs):
        pytest.fail('Client must not be constructed')
    for values, confirmed, expected in [(env(), False, 'CONFIRMATION_REQUIRED'), ({}, True, 'MISSING_CREDENTIALS')]:
        report = diagnose(live=True, confirm_read_only=confirmed, environ=values, client_factory=factory)
        assert report['checks'][-1]['code'] == expected
        assert diagnostic_exit_code(report) == 1 and not report['network_requests_attempted']


def test_client_requires_confirmation_even_with_fake_transport(monkeypatch):
    bindings(monkeypatch)
    transport = Transport([])
    client = SPAPIClient(transport)
    with pytest.raises(SPAPIError) as error:
        client.access_token()
    assert error.value.code == 'CONFIRMATION_REQUIRED' and not transport.calls


def test_orders_2026_pagination_mapping_cancellation_and_no_pii(monkeypatch):
    bindings(monkeypatch)
    transport = Transport([token(), Response(200, {'orders': [order()], 'pagination': {'nextToken': 'next'}}),
                           Response(200, {'orders': [order('two', cancelled=True)]})])
    client = SPAPIClient(transport, allow_live=True)
    result = client.orders('start', 'finish')
    assert len(result) == 2 and result[0]['FulfillmentChannel'] == 'AFN'
    assert client.order_items('one')[0]['QuantityOrdered'] == 3
    assert client.order_items('two')[0]['QuantityOrdered'] == 0
    assert len(transport.calls) == 3  # Items are embedded, no extra HTTP query.
    assert transport.calls[2][2]['params']['paginationToken'] == 'next'
    assert transport.calls[1][2]['params']['createdAfter'] == transport.calls[2][2]['params']['createdAfter']
    assert transport.calls[1][2]['params']['includedData'] == 'FULFILLMENT,CANCELLATION'
    assert 'DO-NOT-LOG-PII' not in json.dumps(result + client.order_items('one'))
    assert all(call[2]['allow_redirects'] is False for call in transport.calls)
    with pytest.raises(SPAPIError):
        client.order_items('unknown')


@pytest.mark.parametrize('body', [{'payload': {'Orders': []}}, {'orders': [{'orderId': 'missing-items'}]},
                                  {'orders': [order()], 'pagination': ['invalid']}])
def test_orders_2026_invalid_response_fails_closed(monkeypatch, body):
    bindings(monkeypatch)
    client = SPAPIClient(Transport([token(), Response(200, body)]), allow_live=True)
    with pytest.raises(SPAPIError):
        client.orders('start', 'finish')


def test_safe_lwa_failure_no_body_tokens_or_pii(monkeypatch):
    bindings(monkeypatch)
    transport = Transport([Response(400, {'error': 'invalid_grant', 'error_description': 'DO-NOT-LOG-REFRESH'})])
    report = diagnose(live=True, confirm_read_only=True, environ=env(),
                      client_factory=lambda **kwargs: SPAPIClient(transport, **kwargs))
    assert report['checks'][-1]['code'] == 'LWA_INVALID_GRANT'
    assert not report['authentication_verified'] and len(transport.calls) == 1
    assert 'DO-NOT-LOG-REFRESH' not in json.dumps(report)


def test_confirmed_smoke_with_fake_transport_one_page_each_no_store_writes(monkeypatch):
    bindings(monkeypatch)
    transport = Transport([token(), Response(200, {'payload': {'inventorySummaries': []}}),
                           Response(403, {'errors': [{'message': 'DO-NOT-LOG-PII'}]}),
                           Response(200, {'payload': {'ShipmentData': []}})])
    report = diagnose(live=True, confirm_read_only=True, environ=env(),
                      client_factory=lambda **kwargs: SPAPIClient(transport, **kwargs))
    assert report['authentication_verified'] and diagnostic_exit_code(report) == 1
    assert [c['status'] for c in report['checks']] == ['passed', 'passed', 'failed', 'passed']
    assert report['checks'][2]['code'] == 'SPAPI_HTTP_403'
    assert len(transport.calls) == 4
    assert [call[0] for call in transport.calls] == ['POST', 'GET', 'GET', 'GET']
    assert 'DO-NOT-LOG-PII' not in json.dumps(report)


@pytest.mark.parametrize('response,code', [
    (Response(302, headers={'Location': 'https://evil.example'}), 'REDIRECT_BLOCKED'),
    (requests.exceptions.SSLError('DO-NOT-LOG-SECRET'), 'TLS_ERROR'),
    (requests.exceptions.ProxyError('DO-NOT-LOG-SECRET'), 'PROXY_ERROR'),
])
def test_redirect_tls_proxy_failures_never_leak_or_retry(monkeypatch, response, code):
    bindings(monkeypatch)
    transport = Transport([response])
    client = SPAPIClient(transport, sleep=lambda _: pytest.fail('Must not retry'), allow_live=True)
    with pytest.raises(SPAPIError) as error:
        client.access_token()
    assert error.value.code == code and len(transport.calls) == 1
    assert 'DO-NOT-LOG-SECRET' not in str(error.value)


def test_mock_isolation_preserves_family_stop_and_marks_history(tmp_path):
    real = connect(tmp_path / 'real.db')
    real.execute("INSERT INTO sku(marketplace,sku,family) VALUES('US','existing','f')")
    real.commit()
    day = datetime.now(timezone.utc).date() - timedelta(days=2)
    with pytest.raises(ValueError, match='empty database'):
        prepare_mock_database(real, 'US', day)
    assert not is_mock_database(real)
    with pytest.raises(ValueError, match='separate database modes'):
        sync(real, MockSPAPIClient(), 'US', asof=day.isoformat())
    assert real.execute('SELECT COUNT(*) FROM sync_log').fetchone()[0] == 0
    assert real.execute('SELECT COUNT(*) FROM sku').fetchone()[0] == 1
    con = connect(tmp_path / 'mock.db')
    prepare_mock_database(con, 'US', day)
    sync(con, MockSPAPIClient(), 'US', asof=day.isoformat())
    sync(con, MockSPAPIClient(), 'US', asof=day.isoformat())
    rows = calculate(con, asof=day.isoformat())
    assert all(r['recommended_qty'] == 0 for r in rows if r['family'] == 'J001')
    assert next(r for r in rows if r['family'] == 'J002')['recommended_qty'] > 0
    assert {r['source'] for r in con.execute('SELECT source FROM snapshots')} == {'MOCK'}
    real.close()
    con.close()


def test_cli_offline_diagnostic_does_not_create_db(monkeypatch, tmp_path, capsys):
    db = tmp_path / 'should-not-exist.db'
    monkeypatch.setattr('sys.argv', ['fba-plan', '--db', str(db), 'diagnose-spapi'])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == 'offline' and not db.exists()


def test_cli_unconfirmed_sync_blocks_before_db_creation(monkeypatch, tmp_path):
    db = tmp_path / 'should-not-exist.db'
    monkeypatch.setattr('sys.argv', ['fba-plan', '--db', str(db), 'sync'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and not db.exists()


def test_cli_cannot_live_sync_mock_database(monkeypatch, tmp_path):
    db = tmp_path / 'mock.db'
    con = connect(db)
    prepare_mock_database(con, 'US', datetime.now(timezone.utc).date() - timedelta(days=2))
    con.close()
    monkeypatch.setattr('sys.argv', ['fba-plan', '--db', str(db), 'sync', '--confirm-read-only'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1


def test_orders_2026_sync_with_real_adapter_fake_transport_preserves_family_stop(monkeypatch, tmp_path):
    bindings(monkeypatch)
    day = datetime.now(timezone.utc).date() - timedelta(days=2)
    con = connect(tmp_path / 'adapter.db')
    skus = ['J001-size 5', 'J001-size 6', 'J002-size 5']
    with con:
        con.executemany('INSERT INTO sku(marketplace,sku,family) VALUES(?,?,?)',
                        [('US', sku, sku.split('-')[0]) for sku in skus])
    from fba_replenishment.core import stop
    stop(con, 'US', skus[0], 'Stopped')
    inventory = [{'sellerSku': sku, 'asin': 'B'+str(index).zfill(9), 'fnSku': 'test-'+sku, 'condition': 'NewItem',
                  'inventoryDetails': {'fulfillableQuantity': 1, 'reservedQuantity': {'totalReservedQuantity': 0},
                                       'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
                                       'inboundWorkingQuantity': 0, 'inboundShippedQuantity': 0,
                                       'inboundReceivingQuantity': 0}} for index, sku in enumerate(skus)]
    from fba_replenishment.discovery import discover, approve_mapping
    discover(con, inventory, 'US', 'ATVPDKIKX0DER')
    for sku in skus:
        approve_mapping(con, 'US', sku, sku.split('-')[0], 'Test manager')
    raw = order()
    raw['createdTime'] = day.isoformat()+'T12:00:00Z'
    raw['orderItems'][0]['product']['sellerSku'] = skus[2]
    raw['orderItems'][0]['quantityOrdered'] = 30
    transport = Transport([token(), Response(200, {'payload': {'inventorySummaries': inventory}}),
                           Response(200, {'orders': [raw]}), Response(200, {'payload': {'ShipmentData': []}})])
    counts = sync(con, SPAPIClient(transport, allow_live=True), 'US', asof=day.isoformat())
    assert counts == {'stock': 3, 'sales': 90, 'inbound': 0}
    rows = {r['sku']: r for r in calculate(con, asof=day.isoformat())}
    assert rows[skus[0]]['recommended_qty'] == rows[skus[1]]['recommended_qty'] == 0
    assert rows[skus[2]]['recommended_qty'] == 109
    assert 'DO-NOT-LOG-PII' not in ''.join(r['data'] for r in con.execute('SELECT data FROM snapshots'))
    con.close()


def test_2026_refresh_does_not_reuse_old_item_cache(monkeypatch):
    bindings(monkeypatch)
    transport = Transport([token(), Response(200, {'orders': [order()]}),
                           Response(200, {'orders': []},), Response(200, {'orders': []})])
    client = SPAPIClient(transport, allow_live=True)
    client.orders('start', 'end')
    assert client.order_items('one')
    client.orders('new-start', 'new-end')
    with pytest.raises(SPAPIError):
        client.order_items('one')


def test_legacy_deprecation_is_reported():
    report = diagnose(environ={'SP_API_ORDERS_VERSION': 'v0'})
    assert any('deprecated' in warning for warning in report['warnings'])
