"""Opt-in US SKU scenarios using the existing token-free snapshot collectors."""
from collections import Counter
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from .collection import CollectionQualityError, collect_sales_shards, collect_inventory_snapshot
from .config import US_MARKETPLACE
from .core import connect, log_sync
from .mock import is_mock_database
from .spapi import SPAPIError


class SafeCountSession(requests.Session):
    """Serial per-operation pacing; audit only counts and fixed operation names.

Server rates update ordinary pacing only on successful responses. A 429 adds
one cooldown rather than permanently multiplying the normal request interval.
The existing SPAPIClient retains its bounded retry/authentication behavior.
"""
    DEFAULT_INTERVALS = {
        'ORDERS_SEARCH': 2.0, 'ORDERS_ITEMS': 2.0, 'ORDERS_GET': 2.0,
        'FBA_INVENTORY': 0.5, 'INBOUND_SHIPMENTS': 0.5,
        'INBOUND_ITEMS': 0.5, 'CATALOG_ITEMS': 0.5, 'LWA': 0.0, 'OTHER': 0.0,
    }

    def __init__(self, progress=None, clock=time.monotonic, sleep=time.sleep):
        super().__init__()
        self.http_status_counts = Counter()
        self.progress = progress
        self.clock, self.sleep = clock, sleep
        self.operation_intervals = dict(self.DEFAULT_INTERVALS)
        self.throttle_audit = {}
        self._next_allowed = {}
        self._request_lock = threading.Lock()

    @staticmethod
    def _operation(method, url):
        try:
            path = urlsplit(url).path
        except (TypeError, ValueError, AttributeError):
            return 'OTHER'
        if str(method).upper() == 'POST' and path == '/auth/o2/token':
            return 'LWA'
        if str(method).upper() != 'GET':
            return 'OTHER'
        if path in ('/orders/2026-01-01/orders', '/orders/v0/orders'):
            return 'ORDERS_SEARCH'
        if path.startswith('/orders/v0/orders/') and path.endswith('/orderItems'):
            return 'ORDERS_ITEMS'
        if path.startswith('/orders/2026-01-01/orders/'):
            return 'ORDERS_GET'
        if path == '/fba/inventory/v1/summaries':
            return 'FBA_INVENTORY'
        if path == '/fba/inbound/v0/shipments':
            return 'INBOUND_SHIPMENTS'
        if (path == '/fba/inbound/v0/shipmentItems'
                or path.startswith('/fba/inbound/v0/shipments/') and path.endswith('/items')):
            return 'INBOUND_ITEMS'
        if path == '/catalog/2022-04-01/items':
            return 'CATALOG_ITEMS'
        return 'OTHER'

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            result = float(value)
        except (ValueError, TypeError, OverflowError):
            return None
        return result if math.isfinite(result) and result > 0 else None

    def _wait(self, operation):
        deadline = self._next_allowed.get(operation, 0.0)
        elapsed = 0.0
        stats = self.throttle_audit[operation]
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            chunk = min(30.0, remaining)
            self.sleep(chunk)
            elapsed += chunk
            if self.progress and elapsed >= 30:
                self.progress({'stage': 'READ_ONLY_THROTTLE_WAIT', 'operation': operation,
                               'wait_seconds': round(elapsed, 3),
                               'interval_seconds': self.operation_intervals[operation],
                               'http_status_counts': dict(self.http_status_counts)})
        if elapsed:
            stats['wait_events'] += 1
            stats['wait_seconds'] += elapsed

    def request(self, *args, **kwargs):
        method = args[0] if args else kwargs.get('method')
        url = args[1] if len(args) > 1 else kwargs.get('url')
        operation = self._operation(method, url)
        # The read-only workflow intentionally has at most one in-flight request.
        with self._request_lock:
            stats = self.throttle_audit.setdefault(operation, {
                'requests': 0, 'rate_updates': 0, 'rate_limits': 0,
                'wait_events': 0, 'wait_seconds': 0.0,
            })
            self._wait(operation)
            started = self.clock()
            self._next_allowed[operation] = started + self.operation_intervals[operation]
            stats['requests'] += 1
            response = super().request(*args, **kwargs)
            self.http_status_counts[str(response.status_code)] += 1
            if response.status_code == 200:
                rate = self._number(response.headers.get('x-amzn-RateLimit-Limit'))
                if rate is not None:
                    interval = 1.0 / rate
                    if math.isfinite(interval) and interval > 0:
                        self.operation_intervals[operation] = interval
                        self._next_allowed[operation] = started + interval
                        stats['rate_updates'] += 1
            elif response.status_code == 429:
                delay = self._number(response.headers.get('Retry-After'))
                cooldown = min(120.0, delay) if delay is not None else 60.0
                self._next_allowed[operation] = max(self._next_allowed[operation],
                                                    self.clock() + cooldown)
                stats['rate_limits'] += 1
            if self.progress and sum(self.http_status_counts.values()) % 50 == 0:
                self.progress({'stage': 'READ_ONLY_HTTP', 'http_status_counts': dict(self.http_status_counts)})
            return response


def open_sku_database(path, source_path=None):
    """Copy existing human configuration once; never migrate the source store."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if source_path and Path(source_path).resolve() == path.resolve():
        raise ValueError('Independent SKU runs require a separate database')
    if not path.exists() and source_path:
        source = sqlite3.connect(Path(source_path).resolve().as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    con = connect(path)
    if is_mock_database(con):
        con.close()
        raise ValueError('Independent live SKU planning cannot use a mock database')
    return con


def apply_latest_source_stops(rows, source_path, marketplace='US'):
    """Observe newly effective original SKU stops without importing or resuming."""
    if not source_path:
        return rows
    from .first_batch import readonly_database
    with readonly_database(source_path) as source:
        tables={row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        stops={row['sku']:row['reason'] for row in source.execute(
            'SELECT sku,reason FROM stop_flag WHERE marketplace=? AND active=1',(marketplace,))}
        for row in source.execute('SELECT sku FROM sku WHERE marketplace=? AND enabled=0',(marketplace,)):
            stops.setdefault(row['sku'],'Explicitly disabled in the current original configuration')
        if 'batch_scheduled_stop' in tables:
            instant=datetime.now(ZoneInfo('UTC'))
            for row in source.execute("SELECT sku,reason,effective_date,effective_timezone FROM batch_scheduled_stop WHERE marketplace=? AND status IN ('PENDING','CONFLICT_PENDING')",(marketplace,)):
                if row['effective_date']<=instant.astimezone(ZoneInfo(row['effective_timezone'])).date().isoformat():
                    stops.setdefault(row['sku'],row['reason'])
    for row in rows:
        if row['marketplace']==marketplace and row['sku'] in stops:
            row.update(status='STOPPED',replenishment_status='STOPPED',recommended_qty=0,
                       stop_reason=stops[row['sku']],procurement_eligible=False)
    return rows


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def run_sku_collection(client, database_path, output_dir, *, source_path=None,
                       asof=None, timezone_name='America/Los_Angeles', shard_days=7,
                       force=False, precheck_only=False, settings=None, progress=None):
    from .sku_collection import precheck_sku, reconcile_sku_data, collect_sku_inbound_snapshot
    from .sku_plan import calculate_skus
    from .sku_export import export_sku_report

    if client.marketplace != US_MARKETPLACE:
        raise ValueError('This independent acceptance run is restricted to the US marketplace')
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    asof = asof or (datetime.now(ZoneInfo(timezone_name)).date() - timedelta(days=1)).isoformat()
    config = dict(settings or {})
    config.update(asof=asof, marketplace='US')
    # Validate parameters before any Amazon request or new database file.
    from .sku_plan import _settings
    _settings(*(config.get(key, default) for key, default in
                [('lead_days',35),('target_days',75),('safety_days',0),('moq',1),
                 ('case_pack',1),('max_stock_age_days',2),('inbound_rule','confirmed_eta'),
                 ('parameters_confirmed',False)]))
    def emit(stage, **counts):
        if progress:
            progress(dict(stage=stage, **counts))
    audits = []
    con = None
    stage = 'PRECHECK'
    try:
        emit(stage)
        report, inventory, inventory_audit = precheck_sku(client, dest/'checkpoints', asof, timezone_name)
        _save(dest/'precheck.json', report)
        if report.get('expansion_allowed') is not True:
            raise CollectionQualityError('PRECHECK_NOT_READY')
        audits.append(dict(stage='PRECHECK', **report))
        emit('PRECHECK_PASSED', inventory_skus=inventory_audit.get('skus'),
             sample_orders=report.get('orders'), sample_items=report.get('order_items'))
        if precheck_only:
            return {'status': 'PRECHECK_PASSED', 'precheck_path': str(dest/'precheck.json'),
                    'full_collection_executed': False}
        con = open_sku_database(database_path, source_path)
        stage = 'SALES_90D'
        emit(stage)
        sales = collect_sales_shards(client, dest/'checkpoints', asof, timezone_name,
                                     days=90, shard_days=shard_days, force=force)
        sales_audit = {'stage': stage, 'complete': sales.complete, 'start': sales.start,
                       'asof': sales.asof, 'timezone': sales.timezone_name,
                       'orders': sales.order_count, 'order_items': sales.item_count,
                       'issues': sales.issues, 'shards': sales.audit,
                       'sales_basis': 'FBA/AFN ordered units; canceled orders excluded; not net returns'}
        _save(dest/'sales_audit.json', sales_audit)
        audits.append(sales_audit)
        audits.extend(dict(stage='SALES_DATE_SHARD', **row) for row in sales.audit)
        emit('SALES_COLLECTED', orders=sales.order_count, order_items=sales.item_count,
             completed_shards=len(sales.audit), issues=sales.issues)
        # A slow Orders usage plan can take hours. Reuse only a fresh inventory
        # checkpoint at calculation time, rather than the precheck's old stock.
        stage = 'INVENTORY_REFRESH'
        emit(stage)
        inventory,inventory_audit=collect_inventory_snapshot(client,dest/'checkpoints',force=force)
        _save(dest/'inventory_final_audit.json',inventory_audit)
        stage = 'INBOUND'
        emit(stage)
        shipments, shipment_items, inbound_audit = collect_sku_inbound_snapshot(
            client, dest/'checkpoints', force=force)
        _save(dest/'inbound_audit.json', inbound_audit)
        audits.extend([dict(stage='INVENTORY', **inventory_audit), dict(stage=stage, **inbound_audit)])
        stage = 'RECONCILE'
        emit(stage)
        reconciliation = reconcile_sku_data(con, inventory, sales, shipments, shipment_items,
                                            marketplace='US', inventory_audit=inventory_audit)
        _save(dest/'reconciliation.json', reconciliation)
        session_counts = dict(getattr(client.session, 'http_status_counts', {}))
        audits.append({'stage': 'READ_ONLY_HTTP', 'http_status_counts': session_counts,
                       'pagination': list(client.pagination_audit)})
        audits.extend({'stage':'READ_ONLY_HTTP','code':'HTTP_'+code,'count':count}
                      for code,count in session_counts.items())
        audits.extend(dict(stage='API_PAGINATION',source=SafeCountSession._operation('GET',row.get('path','')),
                           **{key:row[key] for key in ('pages','rows','complete','code') if key in row})
                      for row in client.pagination_audit)
        earlier_inventory=dest/'inventory_audit.json'
        if earlier_inventory.exists():
            prior=json.loads(earlier_inventory.read_text(encoding='utf-8'))
            audits.extend({'stage':'INVENTORY_BASELINE_HTTP','code':'HTTP_'+code,'count':count}
                          for code,count in prior.get('http_status_counts',{}).items()
                          if code.isdigit() and type(count) is int)
            audits.extend(dict(stage='INVENTORY_BASELINE_PAGINATION',source='FBA Inventory v1',
                              **{key:row[key] for key in ('pages','rows','complete') if key in row})
                          for row in prior.get('pagination',[]))
        rows = apply_latest_source_stops(calculate_skus(con, **config),source_path)
        path = dest/'sku_replenishment_90d.xlsx'
        summary = export_sku_report(rows, path, con, 'US', config, collection_audit=audits)
        summary.update(status='COLLECTED', orders_collected=sales.order_count,
                       order_items_collected=sales.item_count, start=sales.start,
                       asof=sales.asof, timezone=timezone_name, mode='INDEPENDENT_SKU_READ_ONLY',
                       parameters_confirmed=config.get('parameters_confirmed',False),
                       http_status_counts=session_counts, database_path=str(database_path),
                       throttle_audit=getattr(client.session,'throttle_audit',{}))
        _save(dest/'summary.json', summary)
        emit('EXPORTED', excel_path=str(path))
        return summary
    except (SPAPIError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, SPAPIError) else 'LOCAL_VALIDATION_OR_STORAGE_ERROR'
        failure = {'status': 'FAILED', 'stage': stage, 'code': code,
                   'previous_valid_data_retained': True, 'checkpoint_dir': str(dest/'checkpoints'),
                   'full_sales_window_certified': False,
                   'http_status_counts': dict(getattr(client.session,'http_status_counts',{}))}
        if con is not None:
            with con:
                for kind in ('stock','sales','inbound'):
                    log_sync(con,'US',kind,'FAILED',0,'Independent SKU collection failed: '+code)
        _save(dest/'failure.json', failure)
        emit('FAILED', code=code, failed_stage=stage)
        raise
    finally:
        if con is not None:
            con.close()
