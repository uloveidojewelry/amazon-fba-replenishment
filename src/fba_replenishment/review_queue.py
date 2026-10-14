"""Evidence based, finite-set SKU review queues. No network or auto approval."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

from .batch_decisions import create_review_manifest
from .discovery import discovery_export_rows

GRAMMAR_VERSION = 'SKU_REVIEW_CANDIDATES_V1'
MODEL = r'[A-Za-z]{1,4}\d{2,5}[A-Za-z]?'
SIZE = r'\d{1,2}(?:\.\d)?'
COLOR = (r'Black|White|Blue|Red|Pink|Purple|Green|Yellow|Orange|Gold|Golden|Silver|'
         r'Rose[ -]?Gold|Multicolor|Multi[ -]?Color|WhiteBlack|WhiteBlue|WhiteGreen')
GRAMMARS = (
    ('MODEL_EXPLICIT_SIZE', re.compile(r'(?P<model>'+MODEL+r')-size\s+'+SIZE, re.I)),
    ('MODEL_COLOR_SIZE', re.compile(r'(?P<model>'+MODEL+r')-(?:'+COLOR+r')-'+SIZE, re.I)),
    ('MODEL_ENUM_COLOR', re.compile(r'(?P<model>'+MODEL+r')-(?:'+COLOR+r')', re.I)),
    ('MODEL_PAIRED_SIZES', re.compile(r'(?P<model>'+MODEL+r')-?(?:Men|Male)\s?'+SIZE+r'\*(?:Women|Female)\s?'+SIZE, re.I)),
    ('DATED_MODEL_VARIANT', re.compile(r'(?P<logistics>LCS-)?(?P<date>20\d{6}|\d{6})-(?P<model>(?:YA|YC|Y|SA|J)\d{3,5})-(?P<variant>[A-Za-z][A-Za-z0-9\s+*./-]*)', re.I)),
)


def naming_candidate(sku):
    """Capture an explicit model in a whole recognized format, never split '-'."""
    for label, pattern in GRAMMARS:
        match = pattern.fullmatch(sku)
        if match:
            if label == 'DATED_MODEL_VARIANT':
                try:
                    datetime.strptime(match.group('date'), '%Y%m%d' if len(match.group('date')) == 8 else '%y%m%d')
                except ValueError:
                    return {'candidate_family': None, 'naming_format': 'INVALID_DATED_FORMAT',
                            'mapping_basis': 'INVALID_DATE_IN_NAMING_FORMAT', 'confidence': 'UNKNOWN'}
                label = 'LCS_DATED_MODEL_VARIANT' if match.group('logistics') else label
            return {'candidate_family': match.group('model'), 'naming_format': label,
                    'mapping_basis': 'UNAPPROVED_FULLMATCH:'+label, 'confidence': 'CANDIDATE_ONLY'}
    if re.fullmatch(r'(?i)Amazon\.Found\..+', sku):
        label = 'AMAZON_FOUND_IDENTIFIER'
    elif re.fullmatch(r'(?i)[0-9A-Z]{2,5}(?:-[0-9A-Z]{2,5}){2,4}', sku):
        label = 'OPAQUE_GENERATED_IDENTIFIER'
    elif re.fullmatch(r'\d+', sku):
        label = 'NUMERIC_IDENTIFIER'
    elif re.fullmatch(r'(?i)(?:LCS-)?(?:20\d{6}|\d{6})-.+', sku):
        label = 'DATED_UNRECOGNIZED_VARIANT'
    elif re.fullmatch(MODEL+r'-.+', sku, re.I):
        label = 'MODEL_UNRECOGNIZED_SUFFIX'
    else:
        label = 'UNRECOGNIZED_FORMAT'
    return {'candidate_family': None, 'naming_format': label,
            'mapping_basis': 'FORMAT_ONLY_NO_CONFIRMED_MODEL', 'confidence': 'UNKNOWN'}


def _quantity(value):
    return type(value) is int and value >= 0


def _fresh(timestamp, observed_at):
    try:
        value = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return value.tzinfo is not None and timedelta(0) <= observed_at-value <= timedelta(days=2)
    except (ValueError, TypeError, AttributeError):
        return False


def _sales_evidence(con, marketplace, asof, sample=None):
    start = asof-timedelta(days=6)
    sales = defaultdict(dict)
    for row in con.execute('SELECT sku,day,units FROM sales WHERE marketplace=? AND day BETWEEN ? AND ?',
                           (marketplace,start.isoformat(),asof.isoformat())):
        sales[row['sku']][row['day']] = row['units']
    evidence = {}
    expected = {(start+timedelta(days=n)).isoformat() for n in range(7)}
    for sku, values in sales.items():
        if any(not _quantity(n) for n in values.values()):
            continue
        if set(values)==expected:
            units = sum(values.values())
            evidence[sku] = {'recent_sales_status':'POSITIVE_VERIFIED_WINDOW' if units else 'NO_SALES_VERIFIED_WINDOW',
                             'recent_units_observed':units,'recent_sales_source':'COMPLETE_7_DAY_DAILY_TOTALS'}
        elif sum(values.values()) > 0:
            evidence[sku] = {'recent_sales_status':'POSITIVE_PARTIAL_WINDOW',
                             'recent_units_observed':sum(values.values()),'recent_sales_source':'INCOMPLETE_DAILY_TOTALS'}
    observed, seen = Counter(), {}
    # The sample has sanitized business fields only. Its absence proves nothing.
    for order in (sample or {}).get('orders', []):
        if order.get('MarketplaceId') != 'ATVPDKIKX0DER' or order.get('FulfillmentChannel') not in ('AFN','AMAZON'):
            continue
        if order.get('OrderStatus') not in ('SHIPPED','Shipped','PARTIALLY_SHIPPED','PartiallyShipped'):
            continue
        try:
            stamp = datetime.fromisoformat(order['PurchaseDate'].replace('Z','+00:00'))
            # Explicit dates are local buckets in the same zone as the snapshot.
            from zoneinfo import ZoneInfo
            day = stamp.astimezone(ZoneInfo((sample or {}).get('timezone','America/Los_Angeles'))).date()
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if stamp.tzinfo is None or not start <= day <= asof:
            continue
        for line in (sample or {}).get('items',{}).get(order.get('AmazonOrderId'),[]):
            if line.get('CancellationExecuted') or line.get('CancellationAmbiguous'):
                continue
            quantity = line.get('QuantityShipped')
            if not _quantity(quantity) or not quantity:
                continue
            identity = order.get('AmazonOrderId'), line.get('OrderItemId')
            signature = line.get('SellerSKU'),quantity
            if identity in seen:
                if seen[identity]!=signature:
                    raise ValueError('Conflicting sales sample line cannot inform review priority')
                continue
            seen[identity]=signature
            if isinstance(line.get('SellerSKU'),str):
                observed[line['SellerSKU']]+=quantity
    for sku, units in observed.items():
        if sku not in evidence:
            evidence[sku] = {'recent_sales_status':'POSITIVE_SAMPLE_ONLY','recent_units_observed':units,
                             'recent_sales_source':'LIMITED_READ_ONLY_SAMPLE; NOT_COMPLETE_SALES'}
    return evidence


def _priority(item):
    reasons, score = [], 0
    positive_sales = item['recent_sales_status'].startswith('POSITIVE')
    quantities = [item[k] for k in ('fulfillable','reserved','unfulfillable','inbound_working','inbound_shipped','inbound_receiving')]
    if item.get('mapping_conflict'):
        reasons.append('FAMILY_CONFLICT');score+=1000
    if item.get('format_issue'):
        reasons.append('REFERENCE_FORMAT_WARNING');score+=600
    if positive_sales:
        reasons.append('OBSERVED_RECENT_SALES');score+=500
        if item['fulfillable']==0:
            reasons.append('OBSERVED_SALES_WITH_ZERO_FULFILLABLE');score+=800
        elif item['fulfillable'] is None:
            reasons.append('OBSERVED_SALES_WITH_UNKNOWN_INVENTORY');score+=800
    if item['exact_reference_match']:
        reasons.append('EXACT_USER_REFERENCE_PENDING');score+=300
    if item['fba_stock_status']=='HAS_FBA_STOCK':
        reasons.append('POSITIVE_CURRENT_FBA_INVENTORY');score+=200
    elif item['fba_stock_status']=='INBOUND_ONLY':
        reasons.append('POSITIVE_INBOUND_ONLY');score+=180
    if not all(_quantity(q) for q in quantities):
        reasons.append('INVENTORY_INCOMPLETE');score+=150
    if any(_quantity(item[k]) and item[k]>0 for k in ('reserved','unfulfillable')):
        reasons.append('RESERVED_OR_UNFULFILLABLE_REQUIRES_REVIEW');score+=50
    if not item['candidate_family']:
        reasons.append('UNKNOWN_FAMILY_REQUIRES_EVIDENCE')
    if not reasons:
        reasons.append('INSUFFICIENT_ACTIVITY_EVIDENCE')
    return ('P0' if score>=600 else 'P1' if score>=300 else 'P2' if score>=150 else 'P3', score, '; '.join(reasons))


def build_review_queue(con, marketplace='US', *, sample=None, asof=None, observed_at=None):
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    instant = datetime.fromisoformat(timestamp.replace('Z','+00:00'))
    if instant.tzinfo is None:
        raise ValueError('Review snapshot timestamp requires timezone')
    binding = con.execute('SELECT * FROM marketplace_binding WHERE marketplace=?',(marketplace,)).fetchone()
    if not binding:
        raise ValueError('Review requires an existing bound discovery baseline')
    from zoneinfo import ZoneInfo
    zone = binding['sales_timezone'] or 'UTC'
    day = date.fromisoformat(asof) if asof else instant.astimezone(ZoneInfo(zone)).date()-timedelta(days=1)
    if day >= instant.astimezone(ZoneInfo(zone)).date():
        raise ValueError('Review sales evidence asof must be a completed local day')
    records = discovery_export_rows(con,marketplace)
    stocks = {r['sku']:dict(r) for r in con.execute('SELECT * FROM stock WHERE marketplace=?',(marketplace,))}
    has_details = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_details'").fetchone()
    inbound = {r['sku']:dict(r) for r in con.execute('SELECT * FROM inventory_details WHERE marketplace=?',(marketplace,))} if has_details else {}
    sales = _sales_evidence(con,marketplace,day,sample if binding['api_marketplace']=='ATVPDKIKX0DER' else None)
    items, bucketed, anomalies = [], defaultdict(list), []
    for row in records:
        sku = row['sku'];stock=stocks.get(sku,{})
        record = {k:row.get(k) for k in ('sku','asin','marketplace','api_marketplace','family','mapping_status','replenishment_status','first_discovered_at','last_seen_at')}
        inferred = naming_candidate(sku)
        exact = row['mapping_basis']=='USER_WORKBOOK_EXACT_SKU'
        if row['mapping_status']=='APPROVED' and row['family']:
            candidate,basis,shape = row['family'],'MANUALLY_APPROVED_MAPPING','APPROVED'
        elif row['candidate_family']:
            candidate,basis,shape = row['candidate_family'],row['mapping_basis'],('EXACT_REFERENCE' if exact else inferred['naming_format'])
        else:
            candidate,basis,shape = inferred['candidate_family'],inferred['mapping_basis'],inferred['naming_format']
        record.update(candidate_family=candidate,mapping_basis=basis,naming_format=shape,
                      mapping_conflict=bool(row['mapping_conflict']),format_issue=bool(row['format_issue']),
                      exact_reference_match=exact,listing_status='UNKNOWN',
                      listing_evidence='No authorized Listing status snapshot available',
                      inventory_observed_at=stock.get('observed_at'),review_status='UNAPPROVED' if row['mapping_status']!='APPROVED' else 'APPROVED',
                      recommended_qty=0,history_status='UNKNOWN')
        for key in ('fulfillable','reserved','unfulfillable'):
            record[key]=stock.get(key) if _quantity(stock.get(key)) else None
        for key in ('inbound_working','inbound_shipped','inbound_receiving'):
            detail=inbound.get(sku,{})
            record[key]=detail.get(key) if _fresh(detail.get('observed_at'),instant) and _quantity(detail.get(key)) else None
        current = _fresh(stock.get('observed_at'),instant)
        if current and any(_quantity(record[k]) and record[k]>0 for k in ('fulfillable','reserved','unfulfillable')):
            fba='HAS_FBA_STOCK'
        elif current and any(_quantity(record[k]) and record[k]>0 for k in ('inbound_working','inbound_shipped','inbound_receiving')):
            fba='INBOUND_ONLY'
        elif current and all(record[k]==0 for k in ('fulfillable','reserved','unfulfillable','inbound_working','inbound_shipped','inbound_receiving')):
            fba='VERIFIED_ZERO_SNAPSHOT'
        else:
            fba='UNKNOWN'
        record['fba_stock_status']=fba
        record.update(sales.get(sku,{'recent_sales_status':'UNKNOWN','recent_units_observed':None,'recent_sales_source':'NO_COMPLETE_SALES_EVIDENCE'}))
        if row['presence_status']=='INACTIVE':
            record['history_status']='MANUALLY_CONFIRMED_INACTIVE'
        elif row['presence_status']=='SUSPECTED_INACTIVE':
            record['history_status']='UNKNOWN; ABSENT_FROM_ONE_SUCCESSFUL_SNAPSHOT'
        elif _fresh(row['last_seen_at'],instant):
            record['history_status']='CURRENT_API_RECORD; LISTING_STATUS_UNKNOWN'
        record['priority'],record['priority_score'],record['priority_reasons']=_priority(record)
        pending = row['mapping_status']!='APPROVED'
        eligible = bool(candidate and pending and not row['mapping_conflict'] and not row['format_issue'])
        if row['mapping_conflict'] or row['format_issue']:
            bucket=('EXCEPTION',sku)
        elif not pending:
            bucket=('APPROVED',candidate)
        elif exact:
            bucket=('EXACT',candidate)
        elif candidate:
            bucket=('NAMING_OR_AUXILIARY',candidate)
        else:
            bucket=('FORMAT_ONLY',shape)
        record['batch_eligible']=eligible
        bucketed[bucket].append(record);items.append(record)
        if row['mapping_conflict'] or row['format_issue'] or not candidate:
            anomalies.append({'priority':record['priority'],'severity':'BLOCKING','sku':sku,'marketplace':marketplace,
                              'api_marketplace':binding['api_marketplace'],'candidate_family':candidate,
                              'code':'FAMILY_CONFLICT' if row['mapping_conflict'] else 'REFERENCE_FORMAT_WARNING' if row['format_issue'] else 'UNKNOWN_FAMILY',
                              'message':'Human identity/series review required; no automatic approval', 'source':basis})
    input_digest=hashlib.sha256(json.dumps(items,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    snapshot_id='review-'+input_digest[:24]
    con.execute('''CREATE TABLE IF NOT EXISTS review_queue_snapshot (
        snapshot_id TEXT PRIMARY KEY, marketplace TEXT NOT NULL, api_marketplace TEXT NOT NULL,
        created_at TEXT NOT NULL, input_digest TEXT NOT NULL, summary TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS review_queue_group (
        snapshot_id TEXT NOT NULL, group_id TEXT NOT NULL, member_digest TEXT NOT NULL,
        family TEXT, batch_eligible INTEGER NOT NULL, PRIMARY KEY(snapshot_id,group_id))''')
    groups=[]
    with con:
        for bucket, children in sorted(bucketed.items(),key=lambda item:str(item[0])):
            family = children[0]['candidate_family']
            eligible = all(r['batch_eligible'] for r in children)
            manifest=create_review_manifest(con,marketplace,binding['api_marketplace'],family,[r['sku'] for r in children],
                                            observed_at=timestamp,batch_eligible=eligible)
            group_id=manifest['manifest_id'];member_digest=manifest['member_sha']
            best=max(children,key=lambda r:r['priority_score'])
            group={'snapshot_id':snapshot_id,'group_id':group_id,'member_digest':member_digest,'marketplace':marketplace,
                   'api_marketplace':binding['api_marketplace'],'candidate_family':family,
                   'mapping_basis':'; '.join(sorted({r['mapping_basis'] for r in children})),
                   'naming_format':'; '.join(sorted({r['naming_format'] for r in children})),
                   'sku_count':len(children),'exact_match_count':sum(r['exact_reference_match'] for r in children),
                   'stock_positive_count':sum(r['fba_stock_status'] in ('HAS_FBA_STOCK','INBOUND_ONLY') for r in children),
                   'recent_sales_positive_count':sum(r['recent_sales_status'].startswith('POSITIVE') for r in children),
                   'unknown_status_count':sum(r['listing_status']=='UNKNOWN' for r in children),
                   'priority':best['priority'],'priority_score':best['priority_score'],'priority_reasons':best['priority_reasons'],
                   'batch_eligible':eligible}
            groups.append(group)
            con.execute('INSERT OR REPLACE INTO review_queue_group VALUES(?,?,?,?,?)',
                        (snapshot_id,group_id,member_digest,family,int(eligible)))
            for member in children:
                member.update(snapshot_id=snapshot_id,group_id=group_id,member_digest=member_digest)
        pending=[r for r in items if r['mapping_status']!='APPROVED']
        eligible_skus=sum(g['sku_count'] for g in groups if g['batch_eligible'])
        eligible_groups=sum(g['batch_eligible'] for g in groups)
        exceptions=sum(r['format_issue'] or r['mapping_conflict'] for r in pending)
        exact_items=[r for r in pending if r['exact_reference_match']]
        exact_groups=sum(g['batch_eligible'] and g['exact_match_count']>0 for g in groups)
        exact_exceptions=sum(not r['batch_eligible'] for r in exact_items)
        actions=eligible_groups+len(pending)-eligible_skus
        summary={'snapshot_id':snapshot_id,'observed_at':timestamp,'asof':day.isoformat(),'source':'Retained real SP-API inventory + limited sales sample + unapproved source workbook',
                 'collection_status':'OFFLINE_REVIEW_ONLY','total_sku_count':len(items),'pending_sku_count':len(pending),
                 'approved_sku_count':len(items)-len(pending),'group_count':len(groups),'reviewable_group_count':eligible_groups,
                 'candidate_family_count':len({r['candidate_family'] for r in pending if r['candidate_family']}),
                 'exact_match_sku_count':len(exact_items),'exact_match_group_count':exact_groups,
                 'exact_exception_sku_count':exact_exceptions,'exact_review_action_count':exact_groups+exact_exceptions,
                 'exact_actions_saved':len(exact_items)-exact_groups-exact_exceptions,
                 'unknown_mapping_count':sum(not r['candidate_family'] for r in pending),
                 'conflict_sku_count':sum(r['mapping_conflict'] for r in pending),
                 'format_warning_sku_count':sum(r['format_issue'] for r in pending),
                 'exception_sku_count':exceptions,
                 'naming_candidate_sku_count':sum(r['mapping_basis'].startswith('UNAPPROVED_FULLMATCH:') for r in pending),
                 'risk_sku_count':sum(r['priority']=='P0' for r in pending),
                 'active_listing_count':0,'listing_unknown_count':len(items),
                 'recent_sales_count':sum(r['recent_sales_status'].startswith('POSITIVE') for r in items),
                 'fba_stock_count':sum(r['fba_stock_status'] in ('HAS_FBA_STOCK','INBOUND_ONLY') for r in items),
                 'manual_sku_review_count':len(pending),'batch_review_count':actions,'review_actions_saved':len(pending)-actions,
                 'review_reduction_pct':round((len(pending)-actions)/len(pending)*100,2) if pending else 0,
                 'full_history_sync':False,'daily_schedule_deployed':False}
        con.execute('INSERT OR REPLACE INTO review_queue_snapshot VALUES(?,?,?,?,?,?)',
                    (snapshot_id,marketplace,binding['api_marketplace'],timestamp,input_digest,json.dumps(summary,ensure_ascii=False)))
        con.execute('INSERT INTO audit(at,action,marketplace,sku,detail) VALUES(?,?,?,?,?)',
                    (timestamp,'BATCH_REVIEW_QUEUE_CREATED',marketplace,'',json.dumps(summary,ensure_ascii=False)))
    groups.sort(key=lambda g:(g['priority'],-g['priority_score'],-g['exact_match_count'],g['candidate_family'] or '',g['group_id']))
    group_order={g['group_id']:i for i,g in enumerate(groups)}
    items.sort(key=lambda r:(group_order[r['group_id']],r['priority'],-r['priority_score'],r['sku']))
    membership={r['sku']:r['group_id'] for r in items}
    for anomaly in anomalies:
        anomaly['group_id']=membership[anomaly['sku']]
    anomalies.sort(key=lambda r:(r['priority'],r['code'],r['sku']))
    return {'snapshot_id':snapshot_id,'summary':summary,'groups':groups,'items':items,'anomalies':anomalies,
            'audit':[{'at':timestamp,'action':'BATCH_REVIEW_QUEUE_CREATED','marketplace':marketplace,
                      'api_marketplace':binding['api_marketplace'],'snapshot_id':snapshot_id,
                      'reason':'Finite enumerated membership only; no mapping or stop approved'}]}
