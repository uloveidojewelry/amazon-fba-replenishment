"""Read-only SP-API adapters, checked against Amazon's public OpenAPI models.

Only the LWA token exchange uses POST. Business API calls are allowlisted GETs.
No customer names, addresses or financial fields are returned by these adapters.
"""
import os
import re
import time
from urllib.parse import quote

import requests

ENDPOINTS = {
    'https://sellingpartnerapi-na.amazon.com',
    'https://sellingpartnerapi-eu.amazon.com',
    'https://sellingpartnerapi-fe.amazon.com',
}
READ_PATHS = (
    r'/fba/inventory/v1/summaries',
    r'/orders/v0/orders',
    r'/orders/v0/orders/[^/]+/orderItems',
    r'/fba/inbound/v0/shipments',
    r'/fba/inbound/v0/shipments/[^/]+/items',
    r'/fba/inbound/v0/shipmentItems',
)
SHIPMENT_STATUSES = ('WORKING', 'SHIPPED', 'RECEIVING', 'CANCELLED', 'DELETED',
                     'CLOSED', 'ERROR', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN')


class SPAPIError(RuntimeError):
    """Safe error text: never includes a response body, token or customer data."""


class SPAPIClient:
    def __init__(self, session=None, sleep=time.sleep):
        names = ('SP_API_LWA_CLIENT_ID', 'SP_API_LWA_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN')
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise SPAPIError('Missing environment bindings: ' + ', '.join(missing))
        self.client_id, self.secret, self.refresh = (os.environ[name] for name in names)
        self.endpoint = os.environ.get('SP_API_ENDPOINT', 'https://sellingpartnerapi-na.amazon.com').rstrip('/')
        if self.endpoint not in ENDPOINTS:
            raise SPAPIError('SP_API_ENDPOINT must be an official regional HTTPS endpoint')
        self.marketplace = os.environ.get('SP_API_MARKETPLACE_ID', 'ATVPDKIKX0DER')
        self.session = session or requests.Session()
        self.sleep = sleep
        self.token = None
        self.expires = 0

    def _request(self, method, url, **kwargs):
        for attempt in range(5):
            try:
                response = self.session.request(method, url, timeout=45, **kwargs)
            except requests.RequestException:
                if attempt == 4:
                    raise SPAPIError('Network request failed') from None
                self.sleep(min(30, 2 ** attempt))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                try:
                    delay = float(response.headers.get('Retry-After', 2 ** attempt))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                self.sleep(max(0, min(60, delay)))
                continue
            return response
        raise SPAPIError('Retry budget exhausted')

    def access_token(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        response = self._request('POST', 'https://api.amazon.com/auth/o2/token', data={
            'grant_type': 'refresh_token', 'refresh_token': self.refresh,
            'client_id': self.client_id, 'client_secret': self.secret,
        })
        if response.status_code != 200:
            raise SPAPIError(f'LWA HTTP {response.status_code}')
        try:
            data = response.json()
            token = data['access_token']
            lifetime = int(data['expires_in'])
            if not isinstance(token, str) or not token or lifetime <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SPAPIError('Malformed LWA response') from None
        self.token, self.expires = token, time.time() + lifetime
        return token

    def get(self, path, params):
        if not any(re.fullmatch(pattern, path) for pattern in READ_PATHS):
            raise SPAPIError('Operation outside read-only collection allowlist')
        for auth_attempt in range(2):
            response = self._request('GET', self.endpoint + path, params=params, headers={
                'x-amz-access-token': self.access_token(), 'accept': 'application/json',
            })
            if response.status_code == 401 and auth_attempt == 0:
                self.token = None
                continue
            if response.status_code != 200:
                raise SPAPIError(f'SP-API HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise SPAPIError('Malformed SP-API JSON') from None
            if not isinstance(data, dict) or data.get('errors'):
                raise SPAPIError('SP-API returned errors')
            return data
        raise SPAPIError('Authentication failed')

    def _pages(self, path, params, rows_key, token_key='NextToken', inventory=False,
               next_path=None, query_type=False):
        results, seen = [], set()
        params = dict(params)
        while True:
            data = self.get(path, params)
            payload = data.get('payload')
            if not isinstance(payload, dict) or not isinstance(payload.get(rows_key), list):
                raise SPAPIError('Missing collection in API response')
            results.extend(payload[rows_key])
            token = ((data.get('pagination') or {}).get('nextToken') if inventory
                     else payload.get(token_key))
            if not token:
                return results
            if not isinstance(token, str) or token in seen:
                raise SPAPIError('Invalid or repeated pagination token')
            seen.add(token)
            params[token_key] = token
            if query_type:
                params = {'QueryType': 'NEXT_TOKEN', 'NextToken': token,
                          'MarketplaceId': self.marketplace}
            if next_path:
                path = next_path

    def inventory_summaries(self):
        """Full snapshot: no date or SKU filter; consume top-level pagination."""
        return self._pages('/fba/inventory/v1/summaries', {
            'granularityType': 'Marketplace', 'granularityId': self.marketplace,
            'marketplaceIds': self.marketplace, 'details': 'true',
        }, 'inventorySummaries', 'nextToken', inventory=True)

    def orders(self, created_after, created_before):
        raw = self._pages('/orders/v0/orders', {
            'MarketplaceIds': self.marketplace, 'CreatedAfter': created_after,
            'CreatedBefore': created_before, 'FulfillmentChannels': 'AFN',
            'MaxResultsPerPage': 100,
        }, 'Orders')
        keys = ('AmazonOrderId', 'PurchaseDate', 'OrderStatus', 'FulfillmentChannel', 'MarketplaceId')
        return [{key: row.get(key) for key in keys} for row in raw]

    def order_items(self, order_id):
        path = '/orders/v0/orders/' + quote(order_id, safe='') + '/orderItems'
        raw = self._pages(path, {}, 'OrderItems')
        keys = ('OrderItemId', 'SellerSKU', 'ASIN', 'QuantityOrdered', 'QuantityShipped')
        return [{key: row.get(key) for key in keys} for row in raw]

    def inbound_shipments(self):
        raw = self._pages('/fba/inbound/v0/shipments', {
            'QueryType': 'SHIPMENT', 'ShipmentStatusList': ','.join(SHIPMENT_STATUSES),
            'MarketplaceId': self.marketplace,
        }, 'ShipmentData', query_type=True)
        return [{key: row.get(key) for key in ('ShipmentId', 'ShipmentStatus',
                    'DestinationFulfillmentCenterId')} for row in raw]

    def shipment_items(self, shipment_id):
        # The per-shipment endpoint has no NextToken query parameter. Continuations
        # must use the collection endpoint with QueryType=NEXT_TOKEN.
        raw = self._pages('/fba/inbound/v0/shipments/' + quote(shipment_id, safe='') + '/items',
                          {}, 'ItemData', next_path='/fba/inbound/v0/shipmentItems', query_type=True)
        keys = ('ShipmentId', 'SellerSKU', 'QuantityShipped', 'QuantityReceived')
        return [{key: row.get(key) for key in keys} for row in raw]
