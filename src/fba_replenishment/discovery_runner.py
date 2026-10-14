"""Daily-safe read-only discovery runner; no scheduler daemon or Amazon writes."""
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .collection import collect_inventory_snapshot
from .config import SPAPIConfig, US_MARKETPLACE
from .discovery import discover, ensure_schema, should_run_discovery
from .review import run_review
from .sku_reference import load_sku_reference
from .spapi import SPAPIClient, SPAPIError


def settings_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS discovery_settings (
        api_marketplace TEXT PRIMARY KEY, reference_path TEXT, reference_sha256 TEXT)''')


def run_discovery(con, output_dir, marketplace='US', source_xlsx=None, daily=False,
                  catalog_sample_size=20, client=None, inventory=None, inventory_audit=None,
                  sample_summary=None, catalog_probe=None):
    config = SPAPIConfig.from_env()
    if config.marketplace != US_MARKETPLACE or marketplace != 'US':
        raise ValueError('Discovery runner requires the US marketplace')
    day = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    ensure_schema(con)
    settings_schema(con)
    if daily and not should_run_discovery(con, config.marketplace, day):
        return {'status': 'SKIPPED_ALREADY_COMPLETED', 'business_day': day,
                'message': 'Last successful discovery retained; no Amazon request attempted'}
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    prior = con.execute('SELECT reference_path FROM discovery_settings WHERE api_marketplace=?',
                        (config.marketplace,)).fetchone()
    source = source_xlsx or (prior['reference_path'] if prior else None)
    candidates, metadata = None, None
    if source:
        candidates, metadata = load_sku_reference(source)
        retained = dest / 'sku_reference_source.xlsx'
        if Path(source).resolve() != retained.resolve():
            shutil.copyfile(source, retained)
        with con:
            con.execute('INSERT OR REPLACE INTO discovery_settings VALUES(?,?,?)',
                        (config.marketplace, str(retained.resolve()), metadata['source_sha256']))
    real_client = client or SPAPIClient(allow_live=True)
    try:
        if inventory is None:
            # Daily discovery always fetches fresh inventory; never reuse another
            # seller's or an older snapshot merely because the marketplace matches.
            inventory, inventory_audit = collect_inventory_snapshot(real_client, dest / 'checkpoints', force=True)
        sample = dict(sample_summary or {})
        sample.setdefault('timezone', config.timezone)
        result = run_review(con, real_client, dest, marketplace, catalog_sample_size,
                            sample_summary=sample, source_candidates=candidates,
                            inventory=inventory, inventory_audit=inventory_audit,
                            catalog_probe=catalog_probe)
    except (SPAPIError, ValueError, TypeError, KeyError):
        discover(con, None, marketplace, config.marketplace, complete=False,
                 business_day=day, failure_code='DISCOVERY_COLLECTION_FAILED')
        raise
    result['status'] = 'SUCCESS'
    result['business_day'] = day
    if metadata:
        result['reference'] = metadata
    return result
