"""Non-secret configuration validation shared by diagnostics and live clients."""
import os
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

US_MARKETPLACE = 'ATVPDKIKX0DER'
NA_ENDPOINT = 'https://sellingpartnerapi-na.amazon.com'
ENDPOINTS = {NA_ENDPOINT, 'https://sellingpartnerapi-eu.amazon.com',
             'https://sellingpartnerapi-fe.amazon.com'}
CREDENTIAL_NAMES = ('SP_API_LWA_CLIENT_ID', 'SP_API_LWA_CLIENT_SECRET', 'SP_API_REFRESH_TOKEN')
ORDERS_VERSIONS = ('2026-01-01', 'v0')


@dataclass(frozen=True)
class SPAPIConfig:
    endpoint: str
    marketplace: str
    timezone: str
    orders_version: str

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        endpoint = env.get('SP_API_ENDPOINT', NA_ENDPOINT).rstrip('/')
        marketplace = env.get('SP_API_MARKETPLACE_ID', US_MARKETPLACE)
        timezone_name = env.get('SP_API_TIMEZONE', 'UTC')
        version = env.get('SP_API_ORDERS_VERSION', '2026-01-01')
        if endpoint not in ENDPOINTS:
            raise ValueError('SP_API_ENDPOINT must be an official regional HTTPS endpoint')
        if marketplace == US_MARKETPLACE and endpoint != NA_ENDPOINT:
            raise ValueError('US marketplace requires the North America endpoint')
        if not re.fullmatch(r'[A-Z0-9]{10,20}', marketplace):
            raise ValueError('SP_API_MARKETPLACE_ID has invalid format')
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError('SP_API_TIMEZONE must be an installed IANA timezone') from None
        if version not in ORDERS_VERSIONS:
            raise ValueError('SP_API_ORDERS_VERSION must be 2026-01-01 or v0')
        return cls(endpoint, marketplace, timezone_name, version)


def credential_presence(environ=None):
    env = os.environ if environ is None else environ
    # A proxy placeholder counts as present; validity requires an authorized live test.
    return {name: bool(env.get(name, '').strip()) for name in CREDENTIAL_NAMES}
