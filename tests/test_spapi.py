import pytest
import requests

from fba_replenishment.spapi import SPAPIClient, SPAPIError


class Response:
    def __init__(self, code, data, headers=None):
        self.status_code, self.data, self.headers = code, data, headers or {}

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for key in ['SP_API_LWA_CLIENT_ID', 'SP_API_LWA_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN']:
        monkeypatch.setenv(key, 'fake-offline-test-value')
    monkeypatch.delenv('SP_API_ENDPOINT', raising=False)
    monkeypatch.setenv('SP_API_ORDERS_VERSION', 'v0')


def client(responses):
    s = Session([Response(200, {'access_token': 'offline-token', 'expires_in': 3600}), *responses])
    delays = []
    return SPAPIClient(s, sleep=delays.append, allow_live=True), s, delays


def test_inventory_full_snapshot_pagination():
    c, session, _ = client([
        Response(200, {'payload': {'inventorySummaries': [{'sellerSku': 'a'}]}, 'pagination': {'nextToken': 'next'}}),
        Response(200, {'payload': {'inventorySummaries': [{'sellerSku': 'b'}]}}),
    ])
    assert len(c.inventory_summaries()) == 2
    assert len([call for call in session.calls if call[0] == 'POST']) == 1
    for method, url, kwargs in session.calls[1:]:
        assert method == 'GET'
        assert 'startDateTime' not in kwargs['params']
    assert session.calls[-1][2]['params']['nextToken'] == 'next'


def test_orders_and_items_paginate_and_remove_pii():
    c, session, _ = client([
        Response(200, {'payload': {'Orders': [{'AmazonOrderId': 'id', 'BuyerInfo': {'BuyerEmail': 'secret'}}], 'NextToken': 'order-next'}}),
        Response(200, {'payload': {'Orders': []}}),
        Response(200, {'payload': {'OrderItems': [{'OrderItemId': '1', 'BuyerInfo': 'secret'}], 'NextToken': 'item-next'}}),
        Response(200, {'payload': {'OrderItems': [{'OrderItemId': '2'}]}}),
    ])
    orders = c.orders('start', 'end')
    assert 'BuyerInfo' not in orders[0]
    assert len(c.order_items('id')) == 2
    assert session.calls[2][2]['params']['NextToken'] == 'order-next'
    assert session.calls[-1][2]['params']['NextToken'] == 'item-next'


def test_inbound_continuation_uses_collection_endpoint():
    c, session, _ = client([
        Response(200, {'payload': {'ShipmentData': [{'ShipmentId': 's', 'ShipFromAddress': 'secret'}], 'NextToken': 'ship-next'}}),
        Response(200, {'payload': {'ShipmentData': []}}),
        Response(200, {'payload': {'ItemData': [{'SellerSKU': 'a'}], 'NextToken': 'items-next'}}),
        Response(200, {'payload': {'ItemData': [{'SellerSKU': 'b'}]}}),
    ])
    assert 'ShipFromAddress' not in c.inbound_shipments()[0]
    assert session.calls[2][2]['params']['QueryType'] == 'NEXT_TOKEN'
    assert len(c.shipment_items('s')) == 2
    assert session.calls[-1][1].endswith('/fba/inbound/v0/shipmentItems')
    assert session.calls[-1][2]['params'] == {'QueryType': 'NEXT_TOKEN', 'NextToken': 'items-next', 'MarketplaceId': c.marketplace}


def test_retry_auth_refresh_and_safe_error():
    c, session, delays = client([
        Response(429, {}, {'Retry-After': '3'}),
        Response(401, {}),
        Response(200, {'access_token': 'refreshed', 'expires_in': 3600}),
        Response(200, {'payload': {'inventorySummaries': []}}),
    ])
    assert c.inventory_summaries() == []
    assert delays == [3]
    assert session.calls[-1][2]['headers']['x-amz-access-token'] == 'refreshed'
    c, _, _ = client([Response(403, {'secret': 'must-not-be-in-error'})])
    with pytest.raises(SPAPIError, match='HTTP 403') as error:
        c.inventory_summaries()
    assert 'must-not-be' not in str(error.value)


def test_missing_collection_and_repeated_token_fail_closed():
    c, _, _ = client([Response(200, {'payload': {}})])
    with pytest.raises(SPAPIError):
        c.inventory_summaries()
    c, _, _ = client([Response(200, {'payload': {'inventorySummaries': []}, 'pagination': {'nextToken': 'same'}})] * 2)
    with pytest.raises(SPAPIError, match='repeated'):
        c.inventory_summaries()


def test_read_only_allowlist_and_endpoint(monkeypatch):
    c, session, _ = client([])
    for path in ['/orders/v0/orders/id/address', '/fba/inbound/v0/shipments/id', '//evil.example', '/orders/v0/orders/id/shipmentConfirmation']:
        with pytest.raises(SPAPIError, match='allowlist'):
            c.get(path, {})
    assert not session.calls
    monkeypatch.setenv('SP_API_ENDPOINT', 'http://evil.example')
    with pytest.raises(SPAPIError, match='official'):
        SPAPIClient()


def test_network_retry_budget_and_missing_credentials(monkeypatch):
    c, _, delays = client([requests.Timeout('sensitive URL')] * 5)
    with pytest.raises(SPAPIError, match='Network request failed'):
        c.inventory_summaries()
    assert delays == [1, 2, 4, 8]
    monkeypatch.delenv('SP_API_REFRESH_TOKEN')
    with pytest.raises(SPAPIError, match='SP_API_REFRESH_TOKEN'):
        SPAPIClient()
