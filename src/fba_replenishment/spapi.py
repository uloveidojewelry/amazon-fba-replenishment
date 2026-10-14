"""Read-only SP-API adapters, checked against Amazon's public OpenAPI models.

Only the LWA token exchange uses POST. Business API calls are allowlisted GETs.
No customer names, addresses or financial fields are returned by these adapters.
"""
import os
import re
import time
from urllib.parse import quote

import requests

from .config import CREDENTIAL_NAMES, ENDPOINTS, SPAPIConfig, credential_presence

READ_PATHS = (
    r'/fba/inventory/v1/summaries',
    r'/orders/2026-01-01/orders',
    r'/orders/2026-01-01/orders/[^/]+',
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

    def __init__(self, message, code='SPAPI_ERROR', hint='Check configuration and seller authorization'):
        super().__init__(message)
        self.code = code
        self.hint = hint


class SPAPIClient:
    def __init__(self, session=None, sleep=time.sleep, allow_live=False):
        try:
            config = SPAPIConfig.from_env()
        except ValueError as error:
            raise SPAPIError(str(error), 'INVALID_CONFIGURATION') from None
        missing = [name for name, present in credential_presence().items() if not present]
        if missing:
            raise SPAPIError('Missing environment bindings: ' + ', '.join(missing),
                             'MISSING_CREDENTIALS', 'Set bindings securely in environment settings')
        self.client_id, self.secret, self.refresh = (os.environ[name] for name in CREDENTIAL_NAMES)
        self.endpoint, self.marketplace = config.endpoint, config.marketplace
        self.orders_version = config.orders_version
        self.session = session or requests.Session()
        self.sleep = sleep
        self.allow_live = allow_live
        self.token = None
        self.expires = 0
        self._order_items = {}

    def _request(self, method, url, **kwargs):
        if not self.allow_live:
            raise SPAPIError('Live requests require explicit read-only confirmation', 'CONFIRMATION_REQUIRED')
        for attempt in range(5):
            try:
                response = self.session.request(method, url, timeout=45, allow_redirects=False, **kwargs)
            except requests.exceptions.SSLError:
                raise SPAPIError('TLS verification failed', 'TLS_ERROR', 'Repair CA trust; never disable TLS verification') from None
            except requests.exceptions.ProxyError:
                raise SPAPIError('Network proxy refused or failed the connection', 'PROXY_ERROR', 'Check allowed domains and managed proxy readiness') from None
            except requests.RequestException:
                if attempt == 4:
                    raise SPAPIError('Network request failed', 'NETWORK_ERROR', 'Check network reachability and timeout') from None
                self.sleep(min(30, 2 ** attempt))
                continue
            if 300 <= response.status_code < 400:
                raise SPAPIError('Redirect rejected to protect credentials', 'REDIRECT_BLOCKED')
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
            code = 'LWA_HTTP_ERROR'
            try:
                body = response.json()
                if isinstance(body, dict) and body.get('error') in ('invalid_grant', 'invalid_client'):
                    code = 'LWA_' + body['error'].upper()
            except ValueError:
                pass
            raise SPAPIError(f'LWA HTTP {response.status_code}', code,
                             'Check matching app credentials, seller authorization, rotation/revocation; reauthorize if needed')
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
                hints = {401: 'Check token validity and app/seller authorization',
                         403: 'Check approved roles, seller consent, marketplace access and proxy policy',
                         429: 'Rate limit exhausted; wait before another confirmed attempt'}
                raise SPAPIError(f'SP-API HTTP {response.status_code}', f'SPAPI_HTTP_{response.status_code}',
                                 hints.get(response.status_code, 'Check API version, request parameters and service status'))
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
            data = self.get(path, dict(params))
            payload = data.get('payload')
            if not isinstance(payload, dict) or not isinstance(payload.get(rows_key), list):
                raise SPAPIError('Missing collection in API response')
            if not all(isinstance(row, dict) for row in payload[rows_key]):
                raise SPAPIError('Malformed collection record')
            results.extend(payload[rows_key])
            pagination = data.get('pagination') or {}
            if inventory and not isinstance(pagination, dict):
                raise SPAPIError('Malformed inventory pagination')
            token = (pagination.get('nextToken') if inventory
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
        if self.orders_version == '2026-01-01':
            return self._orders_2026(created_after, created_before)
        raw = self._pages('/orders/v0/orders', {
            'MarketplaceIds': self.marketplace, 'CreatedAfter': created_after,
            'CreatedBefore': created_before, 'FulfillmentChannels': 'AFN',
            'MaxResultsPerPage': 100,
        }, 'Orders')
        keys = ('AmazonOrderId', 'PurchaseDate', 'OrderStatus', 'FulfillmentChannel', 'MarketplaceId')
        return [{key: row.get(key) for key in keys} for row in raw]

    def order_items(self, order_id):
        if self.orders_version == '2026-01-01':
            if order_id not in self._order_items:
                raise SPAPIError('Order items must come from the current searchOrders snapshot')
            return [dict(item) for item in self._order_items[order_id]]
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

    def _orders_2026(self, created_after, created_before):
        # 2026 includes complete orderItems in searchOrders; request only the
        # fulfillment/cancellation datasets, never BUYER or RECIPIENT.
        self._order_items = {}
        params = {'marketplaceIds': self.marketplace, 'createdAfter': created_after,
                  'createdBefore': created_before, 'fulfilledBy': 'AMAZON',
                  'maxResultsPerPage': 100, 'includedData': 'FULFILLMENT,CANCELLATION'}
        orders, items_by_order, seen = [], {}, set()
        while True:
            data = self.get('/orders/2026-01-01/orders', dict(params))
            if not isinstance(data.get('orders'), list):
                raise SPAPIError('Missing orders in 2026 response')
            for row in data['orders']:
                if not isinstance(row, dict) or not isinstance(row.get('orderItems'), list):
                    raise SPAPIError('Missing order items in 2026 response')
                fulfillment = row.get('fulfillment') or {}
                channel = row.get('salesChannel') or {}
                if not isinstance(fulfillment, dict) or not isinstance(channel, dict):
                    raise SPAPIError('Malformed 2026 order structure')
                status = fulfillment.get('fulfillmentStatus')
                statuses = {'CANCELLED': 'Canceled', 'PENDING_AVAILABILITY': 'PendingAvailability',
                            'PENDING': 'Pending', 'UNSHIPPED': 'Unshipped',
                            'PARTIALLY_SHIPPED': 'PartiallyShipped', 'SHIPPED': 'Shipped',
                            'UNFULFILLABLE': 'Unfulfillable'}
                order_id = row.get('orderId')
                if not isinstance(order_id, str) or not order_id or order_id in items_by_order:
                    raise SPAPIError('Missing or duplicate 2026 order identity')
                orders.append({'AmazonOrderId': order_id, 'PurchaseDate': row.get('createdTime'),
                               'OrderStatus': statuses.get(status),
                               'FulfillmentChannel': 'AFN' if fulfillment.get('fulfilledBy') == 'AMAZON' else 'MFN',
                               'MarketplaceId': channel.get('marketplaceId')})
                items = []
                for item in row['orderItems']:
                    if not isinstance(item, dict):
                        raise SPAPIError('Malformed 2026 order item')
                    product = item.get('product') or {}
                    cancellation = item.get('cancellation') or {}
                    item_fulfillment = item.get('fulfillment') or {}
                    if not all(isinstance(value, dict) for value in (product, cancellation, item_fulfillment)):
                        raise SPAPIError('Malformed 2026 item datasets')
                    cancelled = cancellation.get('cancellationExecution')
                    items.append({'OrderItemId': item.get('orderItemId'), 'SellerSKU': product.get('sellerSku'),
                                  'ASIN': product.get('asin'),
                                  'QuantityOrdered': 0 if cancelled else item.get('quantityOrdered'),
                                  'QuantityShipped': item_fulfillment.get('quantityFulfilled')})
                items_by_order[order_id] = items
            pagination = data.get('pagination') or {}
            if not isinstance(pagination, dict):
                raise SPAPIError('Malformed 2026 pagination')
            token = pagination.get('nextToken')
            if not token:
                self._order_items = items_by_order
                return orders
            if not isinstance(token, str) or token in seen:
                raise SPAPIError('Invalid or repeated pagination token')
            seen.add(token)
            params['paginationToken'] = token
