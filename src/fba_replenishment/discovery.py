"""Persistent SKU discovery; inventory presence never approves a family mapping.

Marketplace API ID plus SellerSKU is the master identity. Only complete,
validated snapshots may change presence; failed/partial attempts retain history.
Candidates and effective stop inheritance are separate from approved families.
"""
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone

from .collection import sanitize_inventory, validate_inventory
from .core import inferred_family, now
from .spapi import SPAPIError


SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS discovery_site (
        api_marketplace TEXT PRIMARY KEY, marketplace TEXT NOT NULL UNIQUE)''',
    '''CREATE TABLE IF NOT EXISTS discovery_sku (
        api_marketplace TEXT NOT NULL, sku TEXT NOT NULL, marketplace TEXT NOT NULL,
        asin TEXT, candidate_family TEXT, candidate_families TEXT NOT NULL,
        family TEXT, effective_family TEXT, mapping_basis TEXT NOT NULL,
        mapping_status TEXT NOT NULL, match_status TEXT NOT NULL,
        replenishment_status TEXT NOT NULL, presence_status TEXT NOT NULL,
        first_discovered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        is_new INTEGER NOT NULL, last_run_id INTEGER NOT NULL,
        missing_count INTEGER NOT NULL DEFAULT 0, missing_since_at TEXT,
        approved_by TEXT, approved_at TEXT, approval_evidence TEXT,
        approved_asin TEXT, format_issue INTEGER NOT NULL DEFAULT 0,
        mapping_conflict INTEGER NOT NULL DEFAULT 0, inventory_identities TEXT NOT NULL,
        PRIMARY KEY(api_marketplace,sku))''',
    '''CREATE TABLE IF NOT EXISTS discovery_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT, api_marketplace TEXT NOT NULL,
        marketplace TEXT NOT NULL, observed_at TEXT NOT NULL, business_day TEXT NOT NULL,
        status TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
        existing_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0,
        sku_count INTEGER NOT NULL DEFAULT 0, daily_baseline_count INTEGER,
        daily_new_count INTEGER, error_code TEXT)''',
    '''CREATE TABLE IF NOT EXISTS discovery_day_baseline (
        api_marketplace TEXT NOT NULL, business_day TEXT NOT NULL,
        recorded_at TEXT NOT NULL, sku_count INTEGER NOT NULL,
        skus TEXT NOT NULL, PRIMARY KEY(api_marketplace,business_day))''',
    '''CREATE TABLE IF NOT EXISTS discovery_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
        api_marketplace TEXT NOT NULL, marketplace TEXT NOT NULL, sku TEXT NOT NULL,
        action TEXT NOT NULL, detail TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS discovery_naming_rule (
        api_marketplace TEXT NOT NULL, name TEXT NOT NULL, marketplace TEXT NOT NULL,
        pattern TEXT NOT NULL, family_group INTEGER NOT NULL, approved_by TEXT NOT NULL,
        approved_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(api_marketplace,name))''',
)
OWNED_BLOCKS = ('DISCOVERY_MAPPING_UNAPPROVED', 'DISCOVERY_FAMILY_CONFLICT',
                'DISCOVERY_ABSENT', 'DISCOVERY_FAMILY_STOPPED')


def ensure_schema(con):
    # Do not use executescript: it implicitly commits the caller's transaction.
    for statement in SCHEMA:
        con.execute(statement)


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(
            ord(character) < 32 for character in value):
        raise ValueError('Missing or invalid ' + field)
    return value.strip()


def _stamp(value=None):
    value = value or now()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid observation timestamp') from None
    if parsed.tzinfo is None:
        raise ValueError('Observation timestamp requires timezone')
    return parsed.astimezone(timezone.utc).isoformat()


def _day(value, timestamp):
    try:
        return date.fromisoformat(value).isoformat() if value else timestamp[:10]
    except (ValueError, TypeError):
        raise ValueError('Invalid discovery business day') from None


def _site(con, marketplace, api_marketplace):
    marketplace = _text(marketplace, 'marketplace namespace')
    api_marketplace = _text(api_marketplace, 'API marketplace ID')
    if not re.fullmatch(r'[A-Z0-9]{10,20}', api_marketplace):
        raise ValueError('Invalid API marketplace ID')
    binding = con.execute('SELECT api_marketplace FROM marketplace_binding WHERE marketplace=?',
                          (marketplace,)).fetchone()
    if binding and binding['api_marketplace'] != api_marketplace:
        raise ValueError('Marketplace namespace is bound to another API marketplace')
    sites = con.execute('SELECT * FROM discovery_site WHERE marketplace=? OR api_marketplace=?',
                        (marketplace, api_marketplace)).fetchall()
    if any(row['marketplace'] != marketplace or row['api_marketplace'] != api_marketplace for row in sites):
        raise ValueError('Discovery marketplace identity conflicts with an existing site')
    return marketplace, api_marketplace


def _history(con, timestamp, api_marketplace, marketplace, sku, action, detail):
    con.execute('INSERT INTO discovery_history(at,api_marketplace,marketplace,sku,action,detail) VALUES(?,?,?,?,?,?)',
                (timestamp, api_marketplace, marketplace, sku, action,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _families(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [_text(value, 'candidate family')] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        raise ValueError('Invalid candidate family collection')
    return sorted({_text(item, 'candidate family') for item in value if item is not None and item != ''})


def _rules(rules):
    if rules is None:
        return []
    if isinstance(rules, dict):
        rules = [rules] if 'pattern' in rules else rules.get('rules', [])
    if not isinstance(rules, (list, tuple)):
        raise ValueError('Invalid discovery rule configuration')
    result = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError('Invalid discovery rule configuration')
        # Unapproved templates never acquire operational authority.
        if rule.get('approved') is not True:
            continue
        _text(rule.get('approved_by'), 'rule approver')
        _stamp(rule.get('approved_at')) if rule.get('approved_at') else _text(None, 'rule approval timestamp')
        _text(rule.get('name'), 'rule name')
        try:
            pattern = re.compile(_text(rule.get('pattern'), 'rule pattern'))
            group = rule.get('family_group', 1)
            if type(group) is not int or group < 1 or group > pattern.groups:
                raise ValueError()
        except (re.error, ValueError):
            raise ValueError('Invalid approved discovery rule') from None
        result.append((pattern, group))
    return result


def _candidate(sku, asin, sources, catalog, rules, allow_fallback=True):
    source = sources.get(sku) or {}
    if isinstance(source, str):
        source = {'candidate_family': source}
    if not isinstance(source, dict):
        raise ValueError('Invalid SKU source candidate')
    reference = _families(source.get('candidate_family'))
    reference += _families(source.get('conflicting_candidates'))
    reference += _families(source.get('conflicting_families'))
    reference += _families(source.get('candidates'))
    reference = sorted(set(reference))
    names = []
    for pattern, group in rules:
        match = pattern.fullmatch(sku)
        if match:
            names.extend(_families(match.group(group)))
    parent = catalog.get(asin) or []
    if isinstance(parent, dict):
        parent = parent.get('parent_asins') or []
    parents = _families(parent)
    fallback = inferred_family(sku) if allow_fallback and not rules and sku not in sources and not reference and not parents else None
    if fallback:
        names.append(fallback)
    names = sorted(set(names))
    candidates = sorted(set(reference + names))
    # Parent ASIN and a product-model identifier are different namespaces; one
    # parent is supporting evidence, not an automatic conflict with an exact SKU.
    if not candidates and len(parents) == 1:
        candidates = ['PARENT:' + parents[0]]
    conflict = len(candidates) > 1 or len(parents) > 1
    candidate = names[0] if len(names) == 1 else (reference[0] if len(reference) == 1 else (candidates[0] if len(candidates) == 1 else None))
    if names and rules:
        basis = 'APPROVED_NAMING_RULE_CANDIDATE'
    elif reference:
        basis = 'USER_WORKBOOK_EXACT_SKU' if source.get('basis') == 'USER_WORKBOOK_EXACT_SKU' else 'REFERENCE_CANDIDATE'
    elif names:
        basis = 'APPROVED_NAMING_RULE_CANDIDATE' if rules else 'CONSERVATIVE_SKU_NAME_CANDIDATE'
    elif candidates:
        basis = 'AMAZON_CATALOG_PARENT_CANDIDATE'
    else:
        basis = 'NO_CONFIRMED_FAMILY_EVIDENCE'
    return {'candidate_family': candidate, 'candidate_families': candidates,
            'mapping_basis': basis, 'mapping_conflict': conflict,
            'format_issue': bool(source.get('format_issue')),
            'match_status': 'AUTO_MATCHED_PENDING_REVIEW' if candidates else 'UNMATCHED'}


def _refresh_safety(con, marketplace):
    rows = con.execute('SELECT * FROM discovery_sku WHERE marketplace=?', (marketplace,)).fetchall()
    family_stops = {r['family'] for r in con.execute(
        'SELECT family FROM family_stop WHERE marketplace=? AND active=1', (marketplace,))}
    direct = {r['sku'] for r in con.execute(
        'SELECT sku FROM stop_flag WHERE marketplace=? AND active=1', (marketplace,))}
    disabled = {r['sku'] for r in con.execute('SELECT sku FROM sku WHERE marketplace=? AND enabled=0', (marketplace,))}
    # Existing core stops on pending children also protect their candidate family.
    for row in rows:
        if row['sku'] in direct | disabled:
            families = set(json.loads(row['candidate_families'])) | {row['family'], row['effective_family']}
            for family in families - {None, ''}:
                family_stops.add(family)
                con.execute('''INSERT INTO family_stop VALUES(?,?,?,1)
                    ON CONFLICT(marketplace,family,trigger_sku) DO UPDATE SET active=1''',
                            (marketplace, family, row['sku']))
    for row in rows:
        sku = row['sku']
        candidates = set(json.loads(row['candidate_families'])) | {row['family'], row['effective_family']}
        stopped = sku in direct | disabled or bool((candidates - {None, ''}) & family_stops)
        if stopped:
            replenishment = 'STOPPED'
        elif row['mapping_conflict']:
            replenishment = 'BLOCKED_CONFLICT'
        elif row['presence_status'] != 'ACTIVE':
            replenishment = row['presence_status']
        elif row['mapping_status'] != 'APPROVED' or not row['family']:
            replenishment = 'BLOCKED_MAPPING'
        else:
            replenishment = 'MAPPING_APPROVED'
        con.execute('UPDATE discovery_sku SET replenishment_status=? WHERE api_marketplace=? AND sku=?',
                    (replenishment, row['api_marketplace'], sku))
        con.execute('DELETE FROM data_quality_block WHERE marketplace=? AND sku=? AND code IN (?,?,?,?)',
                    (marketplace, sku, *OWNED_BLOCKS))
        blocks = []
        if row['mapping_status'] != 'APPROVED' or not row['family']:
            blocks.append(('DISCOVERY_MAPPING_UNAPPROVED', 'Discovered SKU family requires human approval'))
        if row['mapping_conflict']:
            blocks.append(('DISCOVERY_FAMILY_CONFLICT', 'Conflicting family evidence requires human review'))
        if row['presence_status'] != 'ACTIVE':
            blocks.append(('DISCOVERY_ABSENT', 'SKU absent from complete inventory; status is not confirmed inactive'))
        if stopped:
            blocks.append(('DISCOVERY_FAMILY_STOPPED', 'Manual stop applies to same-marketplace effective family'))
        for code, reason in blocks:
            con.execute('INSERT OR REPLACE INTO data_quality_block VALUES(?,?,?,?)',
                        (marketplace, sku, code, reason))


def discovery_export_rows(con, marketplace='US'):
    ensure_schema(con)
    result = []
    for stored in con.execute('SELECT * FROM discovery_sku WHERE marketplace=? ORDER BY sku', (marketplace,)):
        row = dict(stored)
        row['is_new'] = bool(row['is_new'])
        row['mapping_conflict'] = bool(row['mapping_conflict'])
        row['format_issue'] = bool(row['format_issue'])
        row['candidate_families'] = json.loads(row['candidate_families'])
        row['inventory_identities'] = json.loads(row['inventory_identities'])
        row.pop('approval_evidence', None)
        row.pop('approved_asin', None)
        row['status'] = ('STOPPED' if row['replenishment_status'] == 'STOPPED' else
                         'INACTIVE' if row['presence_status'] == 'INACTIVE' else row['mapping_status'])
        row['review_status'] = row['mapping_status']
        row['last_synced_at'] = row['last_seen_at']
        row['recommended_qty'] = 0
        result.append(row)
    return result


def should_run_discovery(con, api_marketplace, day):
    ensure_schema(con)
    day = date.fromisoformat(day).isoformat()
    return con.execute('SELECT 1 FROM discovery_run WHERE api_marketplace=? AND business_day=? AND status=\'SUCCESS\' LIMIT 1',
                       (api_marketplace, day)).fetchone() is None


def approved_rules(con, api_marketplace):
    ensure_schema(con)
    return [dict(row, approved=True) for row in con.execute(
        'SELECT name,pattern,family_group,approved_by,approved_at FROM discovery_naming_rule WHERE api_marketplace=? AND active=1 ORDER BY name',
        (api_marketplace,))]


def register_approved_rule(con, marketplace, api_marketplace, name, pattern, family_group,
                           approved_by, observed_at=None):
    """Persist a manually approved matching rule; matches remain pending SKUs."""
    ensure_schema(con)
    marketplace, api_marketplace = _site(con, marketplace, api_marketplace)
    timestamp = _stamp(observed_at)
    rule = {'name': name, 'pattern': pattern, 'family_group': family_group,
            'approved_by': approved_by, 'approved_at': timestamp, 'approved': True}
    _rules([rule])
    with con:
        con.execute('''INSERT INTO discovery_naming_rule VALUES(?,?,?,?,?,?,?,1)
            ON CONFLICT(api_marketplace,name) DO UPDATE SET pattern=excluded.pattern,
            family_group=excluded.family_group,approved_by=excluded.approved_by,
            approved_at=excluded.approved_at,active=1''',
                    (api_marketplace, name, marketplace, pattern, family_group, approved_by, timestamp))
        _history(con, timestamp, api_marketplace, marketplace, '', 'NAMING_RULE_APPROVED',
                 {'name': name, 'approved_by': approved_by, 'pattern': pattern, 'family_group': family_group})
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'NAMING_RULE_APPROVED', marketplace, '', json.dumps(rule)))
    return rule


def discover(con, inventory, namespace, api_marketplace, source_candidates=None,
             rules=None, observed_at=None, complete=True, catalog_candidates=None,
             business_day=None, failure_code=None, manage_transaction=True):
    """Upsert a complete inventory discovery snapshot; no Amazon request is made.

    Partial/failed attempts do not alter master rows, absence, approvals or stops.
    Approved naming rules produce pending candidates; only approve_mapping writes
    a newly approved core family. Absence is suspected inactivity, never deletion.
    """
    ensure_schema(con)
    namespace, api_marketplace = _site(con, namespace, api_marketplace)
    timestamp = _stamp(observed_at)
    day = _day(business_day, timestamp)
    if complete is not True:
        code = failure_code if isinstance(failure_code, str) and re.fullmatch(r'[A-Z0-9_]{1,80}', failure_code) else 'INCOMPLETE_INVENTORY_SNAPSHOT'
        with con if manage_transaction else nullcontext():
            cursor = con.execute('INSERT INTO discovery_run(api_marketplace,marketplace,observed_at,business_day,status,error_code) VALUES(?,?,?,?,?,?)',
                                 (api_marketplace, namespace, timestamp, day, 'FAILED', code))
            _history(con, timestamp, api_marketplace, namespace, '', 'DISCOVERY_FAILED', {'code': code})
        return {'status': 'FAILED', 'run_id': cursor.lastrowid, 'error_code': code,
                'new_count': 0, 'existing_count': 0, 'missing_count': 0,
                'records': discovery_export_rows(con, namespace)}
    try:
        clean = sanitize_inventory(inventory)
        validate_inventory(clean)
        if not clean:
            raise SPAPIError('Empty inventory snapshot cannot certify SKU disappearance', 'EMPTY_INVENTORY')
        configured_rules = approved_rules(con, api_marketplace) if rules is None else rules
        compiled = _rules(configured_rules)
        sources, catalog = source_candidates or {}, catalog_candidates or {}
        if not isinstance(sources, dict) or not isinstance(catalog, dict):
            raise ValueError('Invalid discovery source candidate collection')
        grouped = {}
        for item in clean:
            sku = item['sellerSku']
            asin = _text(item.get('asin'), 'inventory ASIN')
            group = grouped.setdefault(sku, {'asins': set(), 'identities': []})
            group['asins'].add(asin)
            group['identities'].append({'fnsku': item['fnSku'], 'condition': item['condition'], 'asin': asin})
        incoming = {}
        for sku, values in grouped.items():
            asin = next(iter(values['asins'])) if len(values['asins']) == 1 else None
            candidate = _candidate(sku, asin, sources, catalog, compiled,
                                   allow_fallback=not configured_rules)
            candidate['mapping_conflict'] |= len(values['asins']) != 1
            incoming[sku] = {'asin': asin, 'inventory_identities': values['identities'],
                             'identity_conflict': len(values['asins']) != 1, **candidate}
    except (SPAPIError, ValueError, TypeError, KeyError):
        # Persist safe failure metadata, never raw rows or exception strings.
        if manage_transaction:
            discover(con, None, namespace, api_marketplace, observed_at=timestamp,
                     complete=False, business_day=day, failure_code='DISCOVERY_VALIDATION_FAILED')
        raise
    old = {r['sku']: dict(r) for r in con.execute(
        'SELECT * FROM discovery_sku WHERE api_marketplace=?', (api_marketplace,))}
    if any(timestamp < r['last_seen_at'] for r in old.values()):
        raise ValueError('Discovery observation is older than the last complete snapshot')
    masters = {r['sku']: dict(r) for r in con.execute('SELECT * FROM sku WHERE marketplace=?', (namespace,))}
    previously_known = set(old)
    new_skus = sorted(set(incoming) - previously_known)
    existing_skus = sorted(set(incoming) & previously_known)
    missing_skus = sorted(set(old) - set(incoming))
    with con if manage_transaction else nullcontext():
        con.execute('INSERT OR IGNORE INTO discovery_site VALUES(?,?)', (api_marketplace, namespace))
        con.execute('INSERT INTO marketplace_binding VALUES(?,?,NULL) ON CONFLICT(marketplace) DO NOTHING',
                    (namespace, api_marketplace))
        con.execute('INSERT OR IGNORE INTO discovery_day_baseline VALUES(?,?,?,?,?)',
                    (api_marketplace, day, timestamp, len(previously_known), json.dumps(sorted(previously_known))))
        baseline = con.execute('SELECT * FROM discovery_day_baseline WHERE api_marketplace=? AND business_day=?',
                               (api_marketplace, day)).fetchone()
        baseline_skus = set(json.loads(baseline['skus']))
        known_after = previously_known | set(incoming)
        daily_new = len(known_after - baseline_skus)
        run = con.execute('''INSERT INTO discovery_run(api_marketplace,marketplace,observed_at,business_day,status,
            new_count,existing_count,missing_count,sku_count,daily_baseline_count,daily_new_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (api_marketplace, namespace, timestamp, day, 'SUCCESS',
            len(new_skus), len(existing_skus), len(missing_skus), len(incoming), baseline['sku_count'], daily_new))
        run_id = run.lastrowid
        for sku, item in incoming.items():
            previous = old.get(sku) or {}
            master = masters.get(sku) or {}
            family = previous.get('family') or master.get('family') or None
            approved = bool(previous.get('approved_by') and previous.get('approved_at'))
            candidates = item['candidate_families']
            conflict = item['mapping_conflict']
            if approved:
                accepted = json.loads(previous.get('approval_evidence') or '[]')
                conflict = bool(item['identity_conflict'] or (conflict and candidates != accepted))
                contradiction = candidates and any(value != family for value in candidates)
                conflict |= bool(contradiction and candidates != accepted)
                conflict |= bool(previous.get('approved_asin') and item['asin'] != previous['approved_asin'])
                status = 'FAMILY_CONFLICT' if conflict else 'APPROVED'
            elif conflict:
                status = 'FAMILY_CONFLICT'
            elif item['format_issue'] or family:
                status = 'REVIEW_REQUIRED'
            elif candidates:
                status = 'AUTO_MATCHED_PENDING_REVIEW'
            else:
                status = 'REVIEW_REQUIRED'
            effective = family or item['candidate_family']
            first_seen = previous.get('first_discovered_at') or timestamp
            row = {
                'api_marketplace': api_marketplace, 'sku': sku, 'marketplace': namespace,
                'asin': item['asin'], 'candidate_family': item['candidate_family'],
                'candidate_families': json.dumps(candidates), 'family': family,
                'effective_family': effective, 'mapping_basis': item['mapping_basis'],
                'mapping_status': status, 'match_status': item['match_status'],
                'replenishment_status': 'BLOCKED_MAPPING',
                'presence_status': 'INACTIVE' if previous.get('presence_status') == 'INACTIVE' else 'ACTIVE',
                'first_discovered_at': first_seen, 'last_seen_at': timestamp,
                'is_new': int(sku not in baseline_skus),
                'last_run_id': run_id, 'missing_count': 0,
                'missing_since_at': None, 'approved_by': previous.get('approved_by'),
                'approved_at': previous.get('approved_at'), 'approval_evidence': previous.get('approval_evidence'),
                'approved_asin': previous.get('approved_asin'), 'format_issue': int(item['format_issue']),
                'mapping_conflict': int(conflict), 'inventory_identities': json.dumps(item['inventory_identities']),
            }
            columns = ','.join(row)
            placeholders = ','.join('?' for _ in row)
            updates = ','.join(key + '=excluded.' + key for key in row if key not in ('api_marketplace', 'sku'))
            con.execute('INSERT INTO discovery_sku(' + columns + ') VALUES(' + placeholders + ') ON CONFLICT(api_marketplace,sku) DO UPDATE SET ' + updates,
                        tuple(row.values()))
            # No candidate may overwrite the formal master mapping.
            con.execute('INSERT INTO sku(marketplace,sku,family,asin) VALUES(?,?,NULL,?) ON CONFLICT(marketplace,sku) DO UPDATE SET asin=excluded.asin',
                        (namespace, sku, item['asin']))
            action = 'DISCOVERED' if not previous else 'SEEN'
            if previous and previous.get('mapping_status') != status:
                action = 'MAPPING_STATUS_CHANGED'
            _history(con, timestamp, api_marketplace, namespace, sku, action,
                     {'mapping_status': status, 'candidate_family': item['candidate_family'], 'family': family})
        for sku in missing_skus:
            previous = old[sku]
            con.execute('''UPDATE discovery_sku SET presence_status=CASE WHEN presence_status='INACTIVE'
                THEN 'INACTIVE' ELSE 'SUSPECTED_INACTIVE' END,
                missing_count=missing_count+1,missing_since_at=COALESCE(missing_since_at,?),
                is_new=?,last_run_id=? WHERE api_marketplace=? AND sku=?''',
                        (timestamp, int(sku not in baseline_skus), run_id, api_marketplace, sku))
            _history(con, timestamp, api_marketplace, namespace, sku, 'ABSENT_FROM_COMPLETE_SNAPSHOT',
                     {'missing_count': previous['missing_count'] + 1, 'presence_status': 'SUSPECTED_INACTIVE'})
        _refresh_safety(con, namespace)
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'SKU_DISCOVERY', namespace, '',
                     json.dumps({'new': len(new_skus), 'existing': len(existing_skus), 'missing': len(missing_skus), 'run_id': run_id})))
    records = discovery_export_rows(con, namespace)
    return {'status': 'SUCCESS', 'run_id': run_id, 'new_count': len(new_skus),
            'existing_count': len(existing_skus), 'missing_count': len(missing_skus),
            'sku_count': len(incoming), 'master_sku_count': len(records),
            'daily_baseline_count': baseline['sku_count'], 'daily_new_count': daily_new,
            'new_skus': new_skus, 'existing_skus': existing_skus, 'missing_skus': missing_skus,
            'records': records}


def _record(con, marketplace, sku):
    ensure_schema(con)
    row = con.execute('SELECT * FROM discovery_sku WHERE marketplace=? AND sku=?',
                      (marketplace, sku)).fetchone()
    if row is None:
        raise ValueError('SKU must be discovered before manual review')
    return dict(row)


def approve_mapping(con, marketplace, sku, family, approved_by, observed_at=None):
    """Explicit manual mapping approval; candidates never invoke this operation."""
    row = _record(con, marketplace, sku)
    family, approver = _text(family, 'approved family'), _text(approved_by, 'mapping approver')
    timestamp = _stamp(observed_at)
    with con:
        con.execute('''UPDATE discovery_sku SET family=?,effective_family=?,mapping_status='APPROVED',
            mapping_conflict=0,approved_by=?,approved_at=?,approval_evidence=?,approved_asin=?
            WHERE api_marketplace=? AND sku=?''',
                    (family, family, approver, timestamp, row['candidate_families'], row['asin'], row['api_marketplace'], sku))
        con.execute('UPDATE sku SET family=? WHERE marketplace=? AND sku=?', (family, marketplace, sku))
        # A remapped stopped trigger continues protecting its former series too.
        if con.execute('SELECT 1 FROM stop_flag WHERE marketplace=? AND sku=? AND active=1', (marketplace, sku)).fetchone():
            con.execute('INSERT OR REPLACE INTO family_stop VALUES(?,?,?,1)', (marketplace, family, sku))
        con.execute("DELETE FROM data_quality_block WHERE marketplace=? AND sku=? AND code='MAPPING_UNAPPROVED'",
                    (marketplace, sku))
        _refresh_safety(con, marketplace)
        detail = {'family': family, 'approved_by': approver, 'previous_family': row['family']}
        _history(con, timestamp, row['api_marketplace'], marketplace, sku, 'MAPPING_APPROVED', detail)
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'MAPPING_APPROVED', marketplace, sku, json.dumps(detail)))
    return next(r for r in discovery_export_rows(con, marketplace) if r['sku'] == sku)


def mark_stop(con, marketplace, sku, reason, observed_at=None):
    """Stop the trigger and every same-site candidate/approved family child."""
    row = _record(con, marketplace, sku)
    reason, timestamp = _text(reason, 'stop reason'), _stamp(observed_at)
    with con:
        con.execute('INSERT INTO stop_flag VALUES(?,?,?,?,1) ON CONFLICT(marketplace,sku) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at,active=1',
                    (marketplace, sku, reason, timestamp))
        families = set(json.loads(row['candidate_families'])) | {row['family'], row['effective_family']}
        for family in families - {None, ''}:
            con.execute('INSERT OR REPLACE INTO family_stop VALUES(?,?,?,1)', (marketplace, family, sku))
        _refresh_safety(con, marketplace)
        _history(con, timestamp, row['api_marketplace'], marketplace, sku, 'STOP', {'reason': reason})
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'STOP', marketplace, sku, reason))
    return next(r for r in discovery_export_rows(con, marketplace) if r['sku'] == sku)


def manual_resume(con, marketplace, sku, approved_by, observed_at=None):
    """Resume one stopped trigger with an explicit approver; other stops survive."""
    row = _record(con, marketplace, sku)
    approver, timestamp = _text(approved_by, 'resume approver'), _stamp(observed_at)
    if not con.execute('''SELECT 1 FROM sku s LEFT JOIN stop_flag f ON f.marketplace=s.marketplace AND f.sku=s.sku
        WHERE s.marketplace=? AND s.sku=? AND (s.enabled=0 OR f.active=1)''', (marketplace, sku)).fetchone():
        raise ValueError('No direct active stop for this SKU')
    with con:
        con.execute('UPDATE sku SET enabled=1 WHERE marketplace=? AND sku=?', (marketplace, sku))
        con.execute('UPDATE stop_flag SET active=0 WHERE marketplace=? AND sku=?', (marketplace, sku))
        con.execute('UPDATE family_stop SET active=0 WHERE marketplace=? AND trigger_sku=?', (marketplace, sku))
        _refresh_safety(con, marketplace)
        _history(con, timestamp, row['api_marketplace'], marketplace, sku, 'RESUME', {'approved_by': approver})
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'RESUME', marketplace, sku, approver))
    return next(r for r in discovery_export_rows(con, marketplace) if r['sku'] == sku)


def mark_inactive(con, marketplace, sku, approved_by, reason, observed_at=None):
    """Only an explicit manual review may confirm inactivity; nothing is deleted."""
    row = _record(con, marketplace, sku)
    approver = _text(approved_by, 'inactive approver')
    reason, timestamp = _text(reason, 'inactive reason'), _stamp(observed_at)
    with con:
        con.execute("UPDATE discovery_sku SET presence_status='INACTIVE' WHERE api_marketplace=? AND sku=?",
                    (row['api_marketplace'], sku))
        _refresh_safety(con, marketplace)
        detail = {'approved_by': approver, 'reason': reason}
        _history(con, timestamp, row['api_marketplace'], marketplace, sku, 'INACTIVE_APPROVED', detail)
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp, 'INACTIVE_APPROVED', marketplace, sku, json.dumps(detail)))
    return next(r for r in discovery_export_rows(con, marketplace) if r['sku'] == sku)
