"""Fake clocks/transports exercise safe operation pacing without HTTP or waits."""
from collections import deque
import json

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from fba_replenishment.sku_runner import SafeCountSession
from fba_replenishment.spapi import SPAPIError


ORDERS = 'https://sellingpartnerapi-na.amazon.com/orders/2026-01-01/orders'
INVENTORY = 'https://sellingpartnerapi-na.amazon.com/fba/inventory/v1/summaries'
INBOUND = 'https://sellingpartnerapi-na.amazon.com/fba/inbound/v0/shipments'


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, delay):
        assert 0 < delay <= 30
        self.sleeps.append(delay)
        self.value += delay


class Response:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        pytest.fail('Throttle must never read or decode a response body')


def session(monkeypatch, responses):
    clock, events, times = Clock(), [], []
    replies = deque(responses)
    def transport(self, *args, **kwargs):
        times.append(clock())
        return replies.popleft()
    monkeypatch.setattr(requests.Session, 'request', transport)
    api = SafeCountSession(events.append, clock=clock, sleep=clock.sleep)
    return api, clock, events, times


def test_orders_initial_two_seconds_and_inventory_has_independent_budget(monkeypatch):
    api, clock, events, times = session(monkeypatch, [Response() for _ in range(5)])
    api.request('GET', ORDERS)
    api.request('GET', INVENTORY)
    api.request('GET', INVENTORY)
    api.request('GET', ORDERS)
    api.request('GET', ORDERS)
    assert times == [0, 0, 1, 2, 4]
    assert api.http_status_counts == {'200': 5}
    assert api.throttle_audit['ORDERS_SEARCH']['wait_seconds'] == 3
    assert not events


@pytest.mark.parametrize('rate,interval', [('2', .5), ('0.5', 2), ('0.25', 4),
                                         (' 4 ', .25), (5, .2)])
def test_successful_rate_header_controls_one_over_rate(monkeypatch, rate, interval):
    api, clock, events, times = session(monkeypatch, [
        Response(headers={'x-amzn-RateLimit-Limit': rate}), Response(), Response()])
    for _ in range(3):
        api.request('GET', ORDERS)
    assert times == pytest.approx([0, interval, interval * 2])
    assert api.operation_intervals['ORDERS_SEARCH'] == interval
    assert api.throttle_audit['ORDERS_SEARCH']['rate_updates'] == 1


@pytest.mark.parametrize('rate', ['nan', 'NaN', 'inf', '-inf', '0', '-1', 'invalid',
                                 '', '1; private', True, None, '1e-323'])
def test_invalid_or_unusable_rate_header_cannot_change_normal_interval(monkeypatch, rate):
    api, _, _, times = session(monkeypatch, [
        Response(headers={'x-amzn-RateLimit-Limit': rate}), Response()])
    api.request('GET', ORDERS)
    api.request('GET', ORDERS)
    assert times == [0, 2]
    assert api.operation_intervals['ORDERS_SEARCH'] == 2
    assert api.throttle_audit['ORDERS_SEARCH']['rate_updates'] == 0


def test_repeated_429_has_sixty_second_cooldowns_without_permanent_multiplier(monkeypatch):
    api, clock, events, times = session(monkeypatch, [
        Response(429), Response(429), Response(429), Response(), Response()])
    for _ in range(5):
        api.request('GET', ORDERS)
    assert times == [0, 60, 120, 180, 182]
    assert max(clock.sleeps) == 30
    assert api.operation_intervals['ORDERS_SEARCH'] == 2
    assert api.throttle_audit['ORDERS_SEARCH']['rate_limits'] == 3
    assert api.http_status_counts == {'429': 3, '200': 2}
    assert len(events) == 6
    assert all(row['stage'] == 'READ_ONLY_THROTTLE_WAIT' for row in events)
    assert [row['wait_seconds'] for row in events] == [30, 60, 30, 60, 30, 60]


@pytest.mark.parametrize('retry_after,delay', [('5', 5), ('120', 120), ('1000', 120),
                                            ('nan', 60), ('inf', 60), ('-1', 60),
                                            ('0', 60), ('invalid', 60), (None, 60)])
def test_retry_after_is_finite_and_bounded_to_120(monkeypatch, retry_after, delay):
    api, clock, events, times = session(monkeypatch, [
        Response(429, {'Retry-After': retry_after}), Response()])
    api.request('GET', ORDERS)
    api.request('GET', ORDERS)
    assert times == [0, delay]
    assert max(clock.sleeps) <= 30


def test_rate_header_on_429_ignored_and_cooldown_does_not_block_other_operations(monkeypatch):
    api, _, _, times = session(monkeypatch, [
        Response(429, {'x-amzn-RateLimit-Limit': '0.001'}), Response(), Response()])
    api.request('GET', ORDERS)
    api.request('GET', INVENTORY)
    api.request('GET', ORDERS)
    assert times == [0, 0, 60]
    assert api.operation_intervals['ORDERS_SEARCH'] == 2
    assert api.throttle_audit['ORDERS_SEARCH']['rate_updates'] == 0


def test_inventory_and_inbound_update_independently_and_continuations_share_item_budget(monkeypatch):
    api, _, _, times = session(monkeypatch, [
        Response(headers={'x-amzn-RateLimit-Limit': '4'}),
        Response(headers={'x-amzn-RateLimit-Limit': '2'}), Response(),
        Response(headers={'x-amzn-RateLimit-Limit': '5'}), Response()])
    api.request('GET', INVENTORY)
    api.request('GET', INBOUND)
    api.request('GET', INVENTORY)
    api.request('GET', INBOUND + '/private-shipment/items')
    api.request('GET', 'https://sellingpartnerapi-na.amazon.com/fba/inbound/v0/shipmentItems')
    assert times == pytest.approx([0, 0, 1, 1, 1.2])
    assert api.operation_intervals['FBA_INVENTORY'] == 1
    assert api.operation_intervals['INBOUND_SHIPMENTS'] == .5
    assert api.throttle_audit['INBOUND_ITEMS']['requests'] == 2


def test_lwa_does_not_consume_order_budget_and_keyword_request_arguments_supported(monkeypatch):
    api, _, _, times = session(monkeypatch, [Response() for _ in range(3)])
    api.request(method='GET', url=ORDERS)
    api.request(method='POST', url='https://api.amazon.com/auth/o2/token',
                data={'test_only': 'BODY-DO-NOT-LOG'})
    api.request(method='GET', url=ORDERS)
    assert times == [0, 0, 2]
    assert api.throttle_audit['LWA']['requests'] == 1


def test_audits_and_heartbeat_never_contain_urls_queries_headers_or_bodies(monkeypatch):
    api, _, events, _ = session(monkeypatch, [Response(429), Response()])
    url = 'https://PRIVATE-HOST.invalid/fba/inbound/v0/shipments/PRIVATE-ID/items?PRIVATE-TOKEN=value'
    for _ in range(2):
        api.request('GET', url, headers={'x-amz-access-token': 'PRIVATE-CREDENTIAL'},
                    data={'refresh_token': 'PRIVATE-REQUEST-BODY'})
    persisted = json.dumps([api.throttle_audit, events, api.http_status_counts])
    assert 'PRIVATE' not in persisted
    assert 'refresh_token' not in persisted and 'access-token' not in persisted
    assert 'http' not in json.dumps(api.throttle_audit)
    assert api.throttle_audit['INBOUND_ITEMS']['rate_limits'] == 1


def test_http_progress_keeps_existing_fifty_response_safe_count_reporting(monkeypatch):
    api, _, events, _ = session(monkeypatch, [Response() for _ in range(50)])
    for _ in range(50):
        api.request('POST', 'https://api.amazon.com/auth/o2/token')
    assert events == [{'stage': 'READ_ONLY_HTTP', 'http_status_counts': {'200': 50}}]


def test_inventory_429_without_retry_after_preserves_fresh_cursor(monkeypatch):
    api, clock, events, times = session(monkeypatch, [Response(), Response(429), Response()])
    api.request('GET', INVENTORY)
    for _ in range(2):
        api.request('GET', INVENTORY, params={'nextToken': 'TEST-ONLY-NOT-PERSISTED'})
    assert times == [0, 1, 3]
    assert 'TEST-ONLY' not in json.dumps([api.throttle_audit, events, api.__dict__], default=str)


def test_inventory_explicit_cooldown_waited_before_expired_cursor_is_rejected(monkeypatch):
    api, clock, events, times = session(monkeypatch, [
        Response(), Response(429, {'Retry-After': '60'}), Response()])
    api.request('GET', INVENTORY)
    api.request('GET', INVENTORY, params={'nextToken': 'TEST-ONLY'})
    with pytest.raises(SPAPIError) as error:
        api.request('GET', INVENTORY, params={'nextToken': 'TEST-ONLY'})
    assert error.value.code == 'INVENTORY_PAGINATION_EXPIRED'
    assert times == [0, 1] and clock() == 61
    # Retry starts at the first page, respecting the previous server deadline.
    api.request('GET', INVENTORY)
    assert times == [0, 1, 61]
    assert api.throttle_audit['FBA_INVENTORY']['expired_cursor_restarts'] == 1


def test_inventory_cursor_age_not_refreshed_by_other_operations(monkeypatch):
    api, clock, _, times = session(monkeypatch, [Response(), Response()])
    api.request('GET', INVENTORY)
    clock.sleep(25)
    api.request('GET', INBOUND)
    with pytest.raises(SPAPIError) as error:
        api.request('GET', INVENTORY, params={'nextToken': 'TEST-ONLY'})
    assert error.value.code == 'INVENTORY_PAGINATION_EXPIRED' and times == [0, 25]


def test_inventory_respects_lower_rate_and_does_not_reclassify_http400(monkeypatch):
    api, clock, _, times = session(monkeypatch, [
        Response(headers={'x-amzn-RateLimit-Limit': '.5'}), Response(400)])
    api.request('GET', INVENTORY)
    response = api.request('GET', INVENTORY, params={'nextToken': 'TEST-ONLY'})
    assert response.status_code == 400 and times == [0, 2]
    assert api.operation_intervals['FBA_INVENTORY'] == 2
