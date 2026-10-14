"""Configuration-only by default. Diagnostics never print credential values/PII."""
import os
from datetime import datetime, timedelta, timezone

from .config import SPAPIConfig, US_MARKETPLACE, credential_presence
from .core import calculate, connect
from .mock import MockSPAPIClient, prepare_mock_database
from .spapi import SPAPIClient, SPAPIError
from .sync import sync


def diagnose(mock=False, live=False, confirm_read_only=False, environ=None, client_factory=None):
    if mock and live:
        raise ValueError('Choose mock or live diagnostics')
    env = os.environ if environ is None else environ
    presence = credential_presence(env)
    report = {'mode': 'mock' if mock else 'live' if live else 'offline',
              'credentials': {key: 'present_unverified' if value else 'missing' for key, value in presence.items()},
              'configuration_valid': False, 'authentication_verified': False,
              'network_requests_attempted': False, 'checks': [], 'warnings': [
                  'Roles and seller consent cannot be verified offline',
                  'Inbound v0 reads are retained; 2024-03-20 adapters are not implemented',
              ]}
    try:
        config = SPAPIConfig.from_env(env)
    except ValueError as error:
        # Only predefined field-specific validation messages are returned.
        report['checks'].append({'name': 'configuration', 'status': 'failed', 'message': str(error)})
        return report
    report['configuration_valid'] = True
    report['configuration'] = {'endpoint': config.endpoint,
                               'marketplace': 'US' if config.marketplace == US_MARKETPLACE else 'non-US (ID suppressed)',
                               'timezone': config.timezone, 'orders_version': config.orders_version}
    if config.marketplace != US_MARKETPLACE:
        report['warnings'].append('Configured marketplace is not the requested US store')
    if config.timezone == 'UTC':
        report['warnings'].append('UTC daily buckets are valid but differ from America/Los_Angeles business days; choose before first sync')
    if config.orders_version == 'v0':
        report['warnings'].append('Orders v0 GETs are marked deprecated in the current official model; prefer 2026-01-01')
    report['configuration_complete'] = all(presence.values())
    if mock:
        con = connect(':memory:')
        try:
            day = datetime.now(timezone.utc).date() - timedelta(days=2)
            prepare_mock_database(con, 'US', day)
            counts = sync(con, MockSPAPIClient(config.timezone), 'US', asof=day.isoformat(), timezone_name=config.timezone)
            rows = calculate(con, asof=day.isoformat())
            valid = (all(r['recommended_qty'] == 0 and r['status'] == 'STOPPED' for r in rows if r['family'] == 'J001')
                     and any(r['recommended_qty'] > 0 for r in rows if r['family'] == 'J002'))
            report['checks'].append({'name': 'mock_collection_and_family_stop', 'status': 'passed' if valid else 'failed', 'row_counts': counts})
        finally:
            con.close()
        report['warnings'].append('All mock quantities are synthetic; this does not validate Amazon connectivity')
        return report
    if not live:
        report['checks'].append({'name': 'configuration', 'status': 'passed'})
        return report
    if not confirm_read_only:
        report['checks'].append({'name': 'live_consent', 'status': 'blocked', 'code': 'CONFIRMATION_REQUIRED'})
        return report
    if not all(presence.values()):
        report['checks'].append({'name': 'credentials', 'status': 'blocked', 'code': 'MISSING_CREDENTIALS'})
        return report
    factory = client_factory or SPAPIClient
    try:
        client = factory(allow_live=True)
        # LWA POST then at most one page per business API; never persist results.
        report['network_requests_attempted'] = True
        client.access_token()
        report['authentication_verified'] = True
        report['checks'].append({'name': 'LWA', 'status': 'passed'})
        end = datetime.now(timezone.utc) - timedelta(days=1)
        for name, path, params, collection, wrapped in [
            ('inventory', '/fba/inventory/v1/summaries', {'granularityType': 'Marketplace',
             'granularityId': config.marketplace, 'marketplaceIds': config.marketplace, 'details': 'true'}, 'inventorySummaries', True),
            ('orders', '/orders/2026-01-01/orders' if config.orders_version == '2026-01-01' else '/orders/v0/orders',
             {'marketplaceIds': config.marketplace, 'createdAfter': (end-timedelta(days=1)).isoformat(),
              'createdBefore': end.isoformat(), 'fulfilledBy': 'AMAZON', 'includedData': 'FULFILLMENT', 'maxResultsPerPage': 1}
             if config.orders_version == '2026-01-01' else {'MarketplaceIds': config.marketplace,
              'CreatedAfter': (end-timedelta(days=1)).isoformat(), 'CreatedBefore': end.isoformat(),
              'FulfillmentChannels': 'AFN', 'MaxResultsPerPage': 1},
             'orders' if config.orders_version == '2026-01-01' else 'Orders', config.orders_version == 'v0'),
            ('inbound', '/fba/inbound/v0/shipments', {'QueryType': 'SHIPMENT', 'ShipmentStatusList': 'SHIPPED',
             'MarketplaceId': config.marketplace}, 'ShipmentData', True),
        ]:
            try:
                data = client.get(path, params)
                payload = data.get('payload') if wrapped else data
                if not isinstance(payload, dict) or not isinstance(payload.get(collection), list):
                    raise SPAPIError('Missing collection in diagnostic response', 'INVALID_RESPONSE')
                report['checks'].append({'name': name, 'status': 'passed', 'first_page_count': len(payload[collection])})
            except SPAPIError as error:
                report['checks'].append({'name': name, 'status': 'failed', 'code': error.code, 'message': str(error), 'hint': error.hint})
    except SPAPIError as error:
        report['checks'].append({'name': 'LWA', 'status': 'failed', 'code': error.code, 'message': str(error), 'hint': error.hint})
    report['warnings'].append('First-page success is a permission/connectivity smoke check, not a complete snapshot')
    return report


def diagnostic_exit_code(report):
    if not report['configuration_valid'] or any(row['status'] in ('failed', 'blocked') for row in report['checks']):
        return 1
    return 0
