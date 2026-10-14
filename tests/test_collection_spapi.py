"""Offline transports verify bounded probes, token-free audit and catalog safety."""
import json

import pytest

from fba_replenishment.spapi import SPAPIClient, SPAPIError


class Response:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Transport:
    def __init__(self, bodies):
        self.responses = iter([{'access_token': 'OFFLINE-ACCESS', 'expires_in': 3600}, *bodies])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(next(self.responses))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ('SP_API_LWA_CLIENT_ID', 'SP_API_LWA_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN'):
        monkeypatch.setenv(name, 'OFFLINE-CREDENTIAL')
    monkeypatch.setenv('SP_API_ORDERS_VERSION', '2026-01-01')
    monkeypatch.delenv('SP_API_ENDPOINT', raising=False)
    monkeypatch.delenv('SP_API_MARKETPLACE_ID', raising=False)


def client(bodies):
    transport = Transport(bodies)
    return SPAPIClient(transport, sleep=lambda _: None, allow_live=True), transport


def order(identity='one', sku='a'):
    return {'orderId': identity, 'createdTime': '2026-09-01T00:00:00Z',
            'salesChannel': {'marketplaceId': 'ATVPDKIKX0DER'},
            'fulfillment': {'fulfillmentStatus': 'SHIPPED', 'fulfilledBy': 'AMAZON'},
            'orderItems': [{'orderItemId': 'line', 'quantityOrdered': 1,
                            'product': {'sellerSku': sku}, 'fulfillment': {'quantityFulfilled': 1}}]}


def test_bounded_inventory_probe_stops_without_persisting_or_reporting_token():
    api, transport = client([{'payload': {'inventorySummaries': [{'sellerSku': 'a'}]},
                              'pagination': {'nextToken': 'DO-NOT-LOG-PAGINATION'}}])
    assert len(api.inventory_summaries(max_pages=1)) == 1
    assert len(transport.calls) == 2
    assert api.collection_complete['/fba/inventory/v1/summaries'] is False
    assert api.pagination_audit[0]['complete'] is False
    assert 'DO-NOT-LOG' not in json.dumps(api.pagination_audit)


def test_bounded_orders_probe_preserves_items_and_marks_partial():
    api, transport = client([{'orders': [order()], 'pagination': {'nextToken': 'DO-NOT-LOG-PAGINATION'}}])
    assert len(api.orders('begin', 'finish', max_pages=1, max_results=25)) == 1
    assert api.order_items('one')[0]['QuantityOrdered'] == 1
    assert api.collection_complete['/orders/2026-01-01/orders'] is False
    assert transport.calls[-1][2]['params']['maxResultsPerPage'] == 25
    assert 'DO-NOT-LOG' not in json.dumps(api.pagination_audit)


def test_consistent_2026_order_duplicates_deduplicate_but_conflicts_reject():
    api, _ = client([{'orders': [order()], 'pagination': {'nextToken': 'next'}},
                      {'orders': [order()]}])
    assert len(api.orders('begin', 'finish')) == 1
    api, _ = client([{'orders': [order()], 'pagination': {'nextToken': 'next'}},
                      {'orders': [order(sku='different')]}])
    with pytest.raises(SPAPIError, match='Conflicting duplicate'):
        api.orders('begin', 'finish')


def test_cancellation_request_does_not_zero_sale_and_execution_is_uncertain():
    first, second = order(), order('two')
    first['orderItems'][0]['cancellation'] = {'cancellationRequest': {'requester': 'BUYER'}}
    second['orderItems'][0]['cancellation'] = {'cancellationExecution': {'cancelledBy': 'AMAZON'}}
    api, _ = client([{'orders': [first, second]}])
    api.orders('begin', 'finish')
    assert api.order_items('one')[0]['QuantityOrdered'] == 1
    assert not api.order_items('one')[0]['CancellationAmbiguous']
    assert api.order_items('two')[0]['CancellationExecuted']
    assert api.order_items('two')[0]['CancellationAmbiguous']
    assert api.order_items('two')[0]['QuantityOrderedBeforeCancellation'] == 1


def test_catalog_variation_relationships_only_no_seller_or_customer_content():
    api, transport = client([{'items': [{'asin': 'B000000001', 'summaries': [{'itemName': 'DO-NOT-PERSIST'}],
                                       'relationships': [{'marketplaceId': 'ATVPDKIKX0DER', 'relationships': [
                                           {'type': 'VARIATION', 'parentAsins': ['B000000002'], 'variationTheme': 'SIZE'},
                                           {'type': 'PACKAGE_HIERARCHY', 'parentAsins': ['B000000003']}]}]}]}])
    assert api.catalog_relationships(['B000000001']) == [
        {'asin': 'B000000001', 'parent_asins': ['B000000002'], 'child_asins': []}]
    assert transport.calls[-1][2]['params']['includedData'] == 'relationships'
    assert all(method == 'GET' for method, _, _ in transport.calls[1:])


@pytest.mark.parametrize('body', [
    {'items': [{'asin': 'B000000001'}]},
    {'items': [{'asin': 'B000000009', 'relationships': []}]},
    {'items': [], 'pagination': {'nextToken': 'DO-NOT-PERSIST'}},
])
def test_catalog_missing_dataset_unexpected_identity_or_pagination_rejects(body):
    api, _ = client([body])
    with pytest.raises(SPAPIError):
        api.catalog_relationships(['B000000001'])


@pytest.mark.parametrize('asins', [[], ['bad'], ['B000000001'] * 2,
                                     ['B' + str(i).zfill(9) for i in range(21)]])
def test_invalid_catalog_batch_blocked_before_network(asins):
    api, transport = client([])
    with pytest.raises(ValueError):
        api.catalog_relationships(asins)
    assert not transport.calls


@pytest.mark.parametrize('pagination', [[], False, 0, ''])
@pytest.mark.parametrize('kind', ['inventory', 'orders'])
def test_explicit_falsey_malformed_pagination_never_marks_complete(pagination, kind):
    body = ({'payload': {'inventorySummaries': []}, 'pagination': pagination}
            if kind == 'inventory' else {'orders': [], 'pagination': pagination})
    api, _ = client([body])
    with pytest.raises(SPAPIError) as error:
        api.inventory_summaries() if kind == 'inventory' else api.orders('begin', 'finish')
    assert error.value.code == 'MALFORMED_PAGINATION'
    assert api.pagination_audit[-1]['complete'] is False
    assert api.pagination_audit[-1]['code'] == 'MALFORMED_PAGINATION'


@pytest.mark.parametrize('continuation', [False, 0, '', '   ', [], {}])
@pytest.mark.parametrize('kind', ['inventory', 'orders', 'legacy'])
def test_explicit_falsey_malformed_next_token_never_marks_complete(continuation, kind):
    if kind == 'inventory':
        body = {'payload': {'inventorySummaries': []}, 'pagination': {'nextToken': continuation}}
    elif kind == 'orders':
        body = {'orders': [], 'pagination': {'nextToken': continuation}}
    else:
        body = {'payload': {'Orders': [], 'NextToken': continuation}}
    api, _ = client([body])
    if kind == 'legacy':
        api.orders_version = 'v0'
    with pytest.raises(SPAPIError) as error:
        api.inventory_summaries() if kind == 'inventory' else api.orders('begin', 'finish')
    assert error.value.code == 'INVALID_PAGINATION_TOKEN'
    assert api.pagination_audit[-1]['complete'] is False
    assert api.pagination_audit[-1]['code'] == 'INVALID_PAGINATION_TOKEN'


def test_repeated_token_failure_audit_has_counts_without_token_value():
    token_marker = 'OFFLINE-DO-NOT-PERSIST-PAGINATION'
    api, _ = client([{'orders': [], 'pagination': {'nextToken': token_marker}}] * 2)
    with pytest.raises(SPAPIError, match='repeated'):
        api.orders('begin', 'finish')
    assert api.pagination_audit[-1] == {
        'path': '/orders/2026-01-01/orders', 'pages': 2, 'rows': 0,
        'continuations': 1, 'complete': False, 'code': 'INVALID_PAGINATION_TOKEN'}
    assert token_marker not in json.dumps(api.pagination_audit)


def test_429_retry_never_reads_or_reports_error_body():
    class RateLimited:
        status_code = 429
        headers = {'Retry-After': '0'}
        def json(self):
            pytest.fail('429 response body must never be read')
    api, transport = client([])
    replies = iter([Response({'access_token': 'OFFLINE-ACCESS', 'expires_in': 3600}),
                    *[RateLimited() for _ in range(5)]])
    transport.request = lambda *args, **kwargs: next(replies)
    with pytest.raises(SPAPIError) as error:
        api.inventory_summaries()
    assert error.value.code == 'SPAPI_HTTP_429'
    assert str(error.value) == 'SP-API HTTP 429'
