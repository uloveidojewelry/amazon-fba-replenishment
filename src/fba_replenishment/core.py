"""SQLite-backed conservative replenishment planning; quantities are integer units."""
import csv
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sku (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, family TEXT, asin TEXT,
 enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(marketplace,sku));
CREATE TABLE IF NOT EXISTS stop_flag (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(marketplace,sku));
CREATE TABLE IF NOT EXISTS family_stop (
 marketplace TEXT NOT NULL, family TEXT NOT NULL, trigger_sku TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(marketplace,family,trigger_sku));
CREATE TABLE IF NOT EXISTS sales (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, day TEXT NOT NULL,
 units INTEGER NOT NULL CHECK(units >= 0), PRIMARY KEY(marketplace,sku,day));
CREATE TABLE IF NOT EXISTS stock (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL,
 fulfillable INTEGER NOT NULL CHECK(fulfillable >= 0), reserved INTEGER NOT NULL DEFAULT 0,
 unfulfillable INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL,
 PRIMARY KEY(marketplace,sku));
CREATE TABLE IF NOT EXISTS inbound (
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, shipment_id TEXT NOT NULL,
 units INTEGER NOT NULL CHECK(units >= 0), eta TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'SHIPPED', PRIMARY KEY(marketplace,sku,shipment_id));
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
 marketplace TEXT NOT NULL, sku TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, marketplace TEXT NOT NULL,
 kind TEXT NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, marketplace TEXT NOT NULL,
 kind TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS marketplace_binding (
 marketplace TEXT PRIMARY KEY, api_marketplace TEXT NOT NULL, sales_timezone TEXT);
CREATE TABLE IF NOT EXISTS order_line (
 marketplace TEXT NOT NULL, order_id TEXT NOT NULL, item_id TEXT NOT NULL,
 sku TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL, units INTEGER NOT NULL,
 PRIMARY KEY(marketplace,order_id,item_id));
"""
ACTIVE_INBOUND = ('SHIPPED', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN', 'RECEIVING')


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Additive migration for V0.1 databases.
    if 'status' not in {row['name'] for row in con.execute('PRAGMA table_info(inbound)')}:
        con.execute("ALTER TABLE inbound ADD COLUMN status TEXT NOT NULL DEFAULT 'SHIPPED'")
    con.execute('''INSERT OR IGNORE INTO family_stop(marketplace,family,trigger_sku,active)
        SELECT s.marketplace,s.family,s.sku,1 FROM sku s JOIN stop_flag f
        ON f.marketplace=s.marketplace AND f.sku=s.sku WHERE f.active=1 AND s.family IS NOT NULL''')
    con.commit()
    return con


def inferred_family(sku):
    match = re.fullmatch(r'(J\d+)-size\s+\d+(?:\.\d+)?', sku, re.I)
    return match.group(1).upper() if match else None


def units(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Missing or invalid quantity')
    if isinstance(value, float) and not value.is_integer():
        raise ValueError('Quantity must be an integer')
    result = int(value)
    if result < 0:
        raise ValueError('Negative quantity')
    return result


def record_snapshot(con, marketplace, kind, source):
    rows = [dict(row) for row in con.execute(f'SELECT * FROM {kind} WHERE marketplace=?', (marketplace,))]
    con.execute('INSERT INTO snapshots(at,marketplace,kind,source,data) VALUES(?,?,?,?,?)',
                (now(), marketplace, kind, source, json.dumps(rows)))


def log_sync(con, marketplace, kind, status, count, detail):
    con.execute('INSERT INTO sync_log(at,marketplace,kind,status,row_count,detail) VALUES(?,?,?,?,?,?)',
                (now(), marketplace, kind, status, count, detail))


def import_csv(con, kind, path, marketplace):
    schemas = {'sku': ['sku'], 'sales': ['sku', 'day', 'units'],
               'stock': ['sku', 'fulfillable'], 'inbound': ['sku', 'shipment_id', 'units', 'eta']}
    if kind not in schemas:
        raise ValueError('Unknown import type')
    with open(path, encoding='utf-8-sig', newline='') as file, con:
        reader = csv.DictReader(file)
        if not set(schemas[kind]).issubset(reader.fieldnames or []):
            raise ValueError(f'Missing columns: {schemas[kind]}')
        count = 0
        for row in reader:
            sku = row['sku'].strip()
            if not sku:
                raise ValueError('Empty SKU')
            if kind == 'sku':
                family = (row.get('family') or '').strip() or inferred_family(sku)
                con.execute('''INSERT INTO sku(marketplace,sku,family,asin) VALUES(?,?,?,?)
                    ON CONFLICT(marketplace,sku) DO UPDATE SET family=excluded.family,asin=excluded.asin''',
                            (marketplace, sku, family, row.get('asin', '')))
                if family and con.execute('SELECT 1 FROM stop_flag WHERE marketplace=? AND sku=? AND active=1',
                                          (marketplace, sku)).fetchone():
                    con.execute('INSERT OR REPLACE INTO family_stop VALUES(?,?,?,1)', (marketplace, family, sku))
            elif kind == 'sales':
                day = date.fromisoformat(row['day']).isoformat()
                con.execute('''INSERT INTO sales VALUES(?,?,?,?) ON CONFLICT(marketplace,sku,day)
                    DO UPDATE SET units=excluded.units''', (marketplace, sku, day, units(row['units'])))
            elif kind == 'stock':
                # Missing fulfillable must never turn into zero.
                quantities = [units(row['fulfillable'])] + [units(row.get(key) or 0) for key in ('reserved', 'unfulfillable')]
                timestamp = row.get('observed_at') or now()
                parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('observed_at requires timezone')
                con.execute('''INSERT INTO stock VALUES(?,?,?,?,?,?) ON CONFLICT(marketplace,sku)
                    DO UPDATE SET fulfillable=excluded.fulfillable,reserved=excluded.reserved,
                    unfulfillable=excluded.unfulfillable,observed_at=excluded.observed_at''',
                            (marketplace, sku, *quantities, timestamp))
            else:
                eta = date.fromisoformat(row['eta']).isoformat() if row['eta'] else ''
                status = (row.get('status') or 'SHIPPED').upper()
                shipment = row['shipment_id'].strip()
                if not shipment:
                    raise ValueError('Shipment ID required')
                con.execute('''INSERT INTO inbound(marketplace,sku,shipment_id,units,eta,status)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(marketplace,sku,shipment_id)
                    DO UPDATE SET units=excluded.units,eta=excluded.eta,status=excluded.status''',
                            (marketplace, sku, shipment, units(row['units']), eta, status))
            count += 1
        record_snapshot(con, marketplace, kind, 'CSV')
        log_sync(con, marketplace, kind, 'SUCCESS', count, 'CSV upsert; absent rows are not deleted')
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (now(), 'IMPORT', marketplace, '', f'{kind}: {count} rows'))
    return count


def stop(con, marketplace, sku, reason):
    if not reason.strip():
        raise ValueError('Reason required')
    mapped = con.execute('SELECT family FROM sku WHERE marketplace=? AND sku=?', (marketplace, sku)).fetchone()
    if mapped is None:
        raise ValueError('SKU must be mapped first')
    with con:
        con.execute('''INSERT INTO stop_flag VALUES(?,?,?,?,1) ON CONFLICT(marketplace,sku)
            DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at,active=1''',
                    (marketplace, sku, reason, now()))
        if mapped['family']:
            con.execute('INSERT OR REPLACE INTO family_stop VALUES(?,?,?,1)', (marketplace, mapped['family'], sku))
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (now(), 'STOP', marketplace, sku, reason))


def resume(con, marketplace, sku, approved_by):
    if not approved_by.strip():
        raise ValueError('Explicit approver required')
    if not con.execute('''SELECT 1 FROM sku s LEFT JOIN stop_flag f ON f.marketplace=s.marketplace AND f.sku=s.sku
        WHERE s.marketplace=? AND s.sku=? AND (s.enabled=0 OR f.active=1)''', (marketplace, sku)).fetchone():
        raise ValueError('No active stop for this SKU')
    with con:
        con.execute('UPDATE sku SET enabled=1 WHERE marketplace=? AND sku=?', (marketplace, sku))
        con.execute('UPDATE stop_flag SET active=0 WHERE marketplace=? AND sku=?', (marketplace, sku))
        con.execute('UPDATE family_stop SET active=0 WHERE marketplace=? AND trigger_sku=?', (marketplace, sku))
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (now(), 'RESUME', marketplace, sku, approved_by))


def calculate(con, marketplace='US', lead_days=35, target_days=75, safety_days=0,
              moq=1, case_pack=1, asof=None, max_stock_age_days=2):
    """30 calendar days ending on asof (inclusive); default last completed UTC day."""
    binding = con.execute('SELECT sales_timezone FROM marketplace_binding WHERE marketplace=?', (marketplace,)).fetchone()
    sales_timezone = (binding['sales_timezone'] if binding else None) or 'UTC'
    asof = date.fromisoformat(asof) if asof else datetime.now(ZoneInfo(sales_timezone)).date() - timedelta(days=1)
    if min(lead_days, target_days, safety_days, max_stock_age_days) < 0 or min(moq, case_pack) < 1:
        raise ValueError('Invalid settings')
    stop_families = {}
    for row in con.execute('SELECT family,trigger_sku FROM family_stop WHERE marketplace=? AND active=1 ORDER BY trigger_sku', (marketplace,)):
        stop_families.setdefault(row['family'], []).append(row['trigger_sku'])
    for disabled in con.execute('SELECT sku,family FROM sku WHERE marketplace=? AND enabled=0', (marketplace,)):
        if disabled['family']:
            stop_families.setdefault(disabled['family'], []).append(disabled['sku'])
    direct_stops = {r['sku'] for r in con.execute('SELECT sku FROM stop_flag WHERE marketplace=? AND active=1', (marketplace,))}
    start, end = (asof - timedelta(days=29)).isoformat(), asof.isoformat()
    due = (asof + timedelta(days=lead_days)).isoformat()
    failed_sources = []
    for kind in ('stock', 'sales', 'inbound'):
        latest = con.execute('SELECT status FROM sync_log WHERE marketplace=? AND kind=? ORDER BY id DESC LIMIT 1', (marketplace, kind)).fetchone()
        if latest and latest['status'] == 'FAILED':
            failed_sources.append(kind)
    rows = []
    for mapped in con.execute('SELECT * FROM sku WHERE marketplace=? ORDER BY family,sku', (marketplace,)):
        sku, family = mapped['sku'], mapped['family']
        stock = con.execute('SELECT * FROM stock WHERE marketplace=? AND sku=?', (marketplace, sku)).fetchone()
        sales = con.execute('SELECT SUM(units) n,COUNT(*) days FROM sales WHERE marketplace=? AND sku=? AND day>=? AND day<=?', (marketplace, sku, start, end)).fetchone()
        daily = (sales['n'] or 0) / 30
        incoming = list(con.execute('SELECT * FROM inbound WHERE marketplace=? AND sku=?', (marketplace, sku)))
        eligible = sum(r['units'] for r in incoming if r['status'] in ACTIVE_INBOUND and r['eta'] and r['eta'] <= due)
        unknown_eta = sum(r['units'] for r in incoming if r['status'] in ACTIVE_INBOUND and not r['eta'])
        age = None
        if stock:
            observed = datetime.fromisoformat(stock['observed_at'].replace('Z', '+00:00')).date()
            # Historical asof does not make an old current snapshot fresh.
            age = max(0, (max(asof, datetime.now(timezone.utc).date()) - observed).days)
        status, reason, qty = 'ACTIVE', '', 0
        if sku in direct_stops or family in stop_families or not mapped['enabled']:
            status, reason = 'STOPPED', 'Manual/family stop: ' + ', '.join(stop_families.get(family, [sku]))
        elif not family:
            status, reason = 'FAMILY_REVIEW_REQUIRED', 'No confirmed family mapping'
        elif failed_sources:
            status, reason = 'SYNC_FAILED', 'Latest collection failed: ' + ', '.join(failed_sources)
        elif stock is None:
            status, reason = 'MISSING_STOCK', 'No inventory snapshot'
        elif age > max_stock_age_days:
            status, reason = 'STALE_STOCK', 'Inventory snapshot exceeds freshness limit'
        elif sales['days'] != 30:
            status, reason = 'INCOMPLETE_SALES', f"Only {sales['days']}/30 daily totals; missing dates are not zero"
        elif unknown_eta:
            status, reason = 'INBOUND_REVIEW_REQUIRED', 'Active inbound has unknown ETA'
        else:
            raw = max(0, math.ceil(daily * (lead_days + target_days + safety_days) - stock['fulfillable'] - eligible))
            qty = math.ceil(max(raw, moq) / case_pack) * case_pack if raw else 0
        rows.append({
            'marketplace': marketplace, 'sku': sku, 'family': family or '', 'asin': mapped['asin'] or '',
            'sales_30d': sales['n'] if sales['days'] else None, 'observed_sales_days': sales['days'],
            'daily_sales': round(daily, 3) if sales['days'] else None,
            'fulfillable': stock['fulfillable'] if stock else None,
            'reserved': stock['reserved'] if stock else None,
            'unfulfillable': stock['unfulfillable'] if stock else None,
            'stock_observed_at': stock['observed_at'] if stock else None, 'stock_age_days': age,
            'eligible_inbound': eligible, 'unknown_eta_inbound': unknown_eta,
            'coverage_days': round(stock['fulfillable'] / daily, 1) if stock and daily > 0 else None,
            'status': status, 'stop_reason': reason, 'recommended_qty': qty, 'asof': end,
            'window_start': start, 'lead_days': lead_days, 'target_days': target_days,
            'safety_days': safety_days, 'moq': moq, 'case_pack': case_pack, 'sales_timezone': sales_timezone,
        })
    return rows
