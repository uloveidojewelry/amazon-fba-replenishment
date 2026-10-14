"""Read-only validation of the first, exact-reference human review collection.

This module issues SELECT statements only. In particular it does not create
schemas, refresh stops, approve mappings, or simulate by writing and rolling
back. A successful simulation is never authorization to import decisions.
"""
from collections import Counter, defaultdict
from datetime import date, datetime
import json

from .batch_decisions import _expected_state, _member_payload, _sha, normalize_stop_row
from .discovery import _confirmed_stop_family

DECISIONS = ('APPROVE', 'REJECT', 'NEEDS_REVIEW')
REQUIRED_TABLES = {
    'discovery_sku', 'sku', 'stop_flag', 'family_stop', 'marketplace_binding',
    'batch_review_manifest', 'batch_review_member', 'batch_review_decision',
    'review_queue_snapshot', 'review_queue_group',
}


def _literal(value):
    return isinstance(value, str) and bool(value.strip()) and not value.startswith('=')


def _selected_decisions(groups, decisions):
    """Accept a decision-sheet list or an explicit group-ID dictionary."""
    selected, issues = {}, []
    known = {group.get('group_id') for group in groups}
    if decisions is None:
        return selected, issues
    if isinstance(decisions, dict):
        rows = [dict(value, group_id=key) if isinstance(value, dict)
                else {'group_id': key, 'decision': value} for key, value in decisions.items()]
    else:
        rows = list(decisions)
    for row in rows:
        group_id = row.get('group_id')
        if group_id not in known:
            issues.append('UNKNOWN_DECISION_GROUP')
        elif group_id in selected:
            issues.append('DUPLICATE_DECISION_GROUP')
        else:
            selected[group_id] = row
    return selected, sorted(set(issues))


def _stop_preview(stops, rows, planned, scope):
    """Project reviewed stops against current and proposed formal identities."""
    previews = []
    for original in stops or []:
        problems, warnings = [], []
        try:
            row = normalize_stop_row(original)
        except ValueError:
            previews.append({'status': 'BLOCKED', 'blockers': ['INVALID_STOP_VALUES']})
            continue
        marketplace, sku, family = row['marketplace'], row['sku'], row['family']
        preview = {'marketplace': marketplace, 'sku': sku, 'family': family,
                   'status': 'NO_CHANGE', 'blockers': problems, 'warnings': warnings,
                   'affected_sku_count': None, 'outside_review_sku_count': None}
        previews.append(preview)
        if row['stop'] != 'TRUE' or row['review_status'] not in ('APPROVE', 'APPROVED'):
            warnings.append('UNAPPROVED_OR_NO_CHANGE_STOP; NEVER_RESUMES_EXISTING_STOP')
            continue
        if not _literal(row['reason']) or not _literal(row['reviewer']):
            problems.append('STOP_REVIEWER_AND_REASON_REQUIRED')
        value = row['effective_date']
        try:
            day = value.date() if isinstance(value, datetime) else value
            effective = day.isoformat() if isinstance(day, date) else date.fromisoformat(day).isoformat()
        except (ValueError, TypeError, AttributeError):
            problems.append('STOP_EFFECTIVE_DATE_REQUIRED')
            effective = None
        preview['effective_date'] = effective
        site = [item for item in rows if item['marketplace'] == marketplace]
        if marketplace not in {identity[0] for identity in scope}:
            problems.append('STOP_OUTSIDE_REVIEW_MARKETPLACE')
        if not site:
            problems.append('STOP_UNKNOWN_MARKETPLACE')
        elif row['api_marketplace'] not in (None, '', site[0]['api_marketplace']):
            problems.append('STOP_API_MARKETPLACE_MISMATCH')
        targets = {(item['marketplace'], item['sku']): item for item in site}
        if sku:
            current = targets.get((marketplace, sku))
            if current is None:
                problems.append('STOP_UNKNOWN_SKU')
            else:
                formal = planned.get((marketplace, sku)) or (
                    current['family'] if current['mapping_status'] == 'APPROVED' else None)
                if not formal:
                    problems.append('STOP_REQUIRES_APPROVED_MAPPING')
                elif family and family != formal:
                    problems.append('STOP_FORMAL_FAMILY_CONFLICT')
                else:
                    family = formal
                    preview['family'] = formal
        elif not family:
            problems.append('STOP_SKU_OR_FAMILY_REQUIRED')
        if family:
            affected = {(item['marketplace'], item['sku']) for item in site
                        if (planned.get((marketplace, item['sku'])) or
                            _confirmed_stop_family(item)) == family}
            if not affected:
                problems.append('STOP_REQUIRES_APPROVED_MAPPING')
            preview['affected_sku_count'] = len(affected)
            preview['outside_review_sku_count'] = len(affected - scope)
            if affected - scope:
                warnings.append('STOP_PROPAGATES_OUTSIDE_FIRST_BATCH')
        preview['status'] = 'BLOCKED' if problems else 'SIMULATED_STOP_REQUIRES_CONFIRMATION'
        if not problems:
            warnings.append('EFFECTIVE_DATE_APPLIED_BY_CONFIRMED_IMPORT_ONLY')
    return previews


def validate_first_batch(con, groups, items, decisions=None, stops=None):
    """Validate frozen workbook scopes and predict stop risks without mutations.

    ``groups`` and ``items`` use batch-export field names. Every edited decision
    must retain the frozen identifiers. Default decisions are NEEDS_REVIEW;
    even valid APPROVE/REJECT rows require a subsequent explicit user approval.
    No unknown business quantity is converted into zero in this report.
    """
    groups, items = list(groups), list(items)
    selected, global_issues = _selected_decisions(groups, decisions)
    report = {'mode': 'READ_ONLY_DRY_RUN', 'status': 'PENDING_REVIEW',
              'structure_status': 'READY', 'decision_status': 'PENDING',
              'requires_explicit_user_confirmation': True, 'import_permitted': False,
              'global_blockers': global_issues, 'groups': [], 'stops': []}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES <= tables:
        report.update(status='BLOCKED', structure_status='BLOCKED')
        report['global_blockers'].append('REQUIRED_REVIEW_SCHEMA_MISSING')
        report['summary'] = {'group_count': len(groups), 'sku_count': len(items),
                             'blocked_group_count': None, 'stop_risk_sku_count': None}
        return report
    counts = Counter(group.get('group_id') for group in groups)
    if any(n != 1 for n in counts.values()):
        global_issues.append('DUPLICATE_REVIEW_GROUP')
    indexed = defaultdict(list)
    for item in items:
        indexed[item.get('group_id')].append(item)
        if item.get('group_id') not in counts:
            global_issues.append('VISIBLE_MEMBER_WITHOUT_GROUP')
    current_rows = [dict(row) for row in con.execute('SELECT * FROM discovery_sku')]
    current = {(row['marketplace'], row['sku']): row for row in current_rows}
    scope = {(item.get('marketplace'), item.get('sku')) for item in items}
    direct = {(row['marketplace'], row['sku']) for row in con.execute(
        'SELECT marketplace,sku FROM stop_flag WHERE active=1')}
    disabled = {(row['marketplace'], row['sku']) for row in con.execute(
        'SELECT marketplace,sku FROM sku WHERE enabled=0')}
    family_stops = {(row['marketplace'], row['family']) for row in con.execute(
        'SELECT marketplace,family FROM family_stop WHERE active=1')}
    for row in current_rows:
        formal = _confirmed_stop_family(row)
        if (row['marketplace'], row['sku']) in direct | disabled and formal:
            family_stops.add((row['marketplace'], formal))
    planned, risk_ids = {}, set()
    for group in groups:
        group_id, marketplace = group.get('group_id'), group.get('marketplace')
        api_marketplace, family = group.get('api_marketplace'), group.get('candidate_family')
        structure, approval, warnings = [], [], []
        decision_row = selected.get(group_id, {})
        action = decision_row.get('decision', 'NEEDS_REVIEW')
        result = {'group_id': group_id, 'candidate_family': family, 'decision': action,
                  'structure_status': 'READY', 'approval_status': 'READY',
                  'structure_blockers': structure, 'approval_blockers': approval,
                  'blockers': [], 'warnings': warnings, 'member_count': len(indexed[group_id]),
                  'stop_risk': {'known_stopped_family': (marketplace, family) in family_stops,
                                'projection_assumption': 'IF_THIS_FROZEN_GROUP_IS_APPROVED',
                                'direct_stopped_member_count': None,
                                'projected_stopped_member_count': None,
                                'outside_review_formal_child_count': None,
                                'future_skus_auto_approved': 0}}
        report['groups'].append(result)
        if action not in DECISIONS:
            structure.append('INVALID_REVIEW_DECISION')
        if action in ('APPROVE', 'REJECT'):
            if not _literal(decision_row.get('reviewer')) or not _literal(decision_row.get('reason')):
                structure.append('REVIEWER_AND_REASON_REQUIRED')
            for key in ('snapshot_id', 'member_digest', 'marketplace', 'api_marketplace', 'candidate_family', 'sku_count'):
                if decision_row.get(key) != group.get(key):
                    structure.append('EDITED_DECISION_SCOPE')
                    break
            if decision_row.get('manifest_id') not in (None, group_id):
                structure.append('EDITED_DECISION_SCOPE')
            if action == 'APPROVE' and decision_row.get('approved_family') != family:
                approval.append('APPROVED_FAMILY_MISMATCH')
        if any(not _literal(group.get(k)) for k in ('group_id', 'snapshot_id', 'member_digest', 'marketplace', 'api_marketplace')):
            structure.append('INVALID_FROZEN_SCOPE_IDENTIFIER')
        manifest = con.execute('SELECT * FROM batch_review_manifest WHERE manifest_id=?', (group_id,)).fetchone()
        saved = con.execute('SELECT * FROM review_queue_group WHERE snapshot_id=? AND group_id=?',
                            (group.get('snapshot_id'), group_id)).fetchone()
        snapshot = con.execute('SELECT * FROM review_queue_snapshot WHERE snapshot_id=?',
                               (group.get('snapshot_id'),)).fetchone()
        binding = con.execute('SELECT api_marketplace FROM marketplace_binding WHERE marketplace=?',
                              (marketplace,)).fetchone()
        if not snapshot or (snapshot['marketplace'], snapshot['api_marketplace']) != (marketplace, api_marketplace):
            structure.append('UNKNOWN_OR_CROSS_MARKETPLACE_SNAPSHOT')
        if not binding or binding['api_marketplace'] != api_marketplace:
            structure.append('MARKETPLACE_BINDING_MISMATCH')
        if not manifest or not saved:
            structure.append('UNKNOWN_FROZEN_GROUP')
            result.update(structure_status='BLOCKED', approval_status='BLOCKED', blockers=structure.copy())
            continue
        if ((manifest['marketplace'], manifest['api_marketplace'], manifest['family'], manifest['member_sha'])
                != (marketplace, api_marketplace, family, group.get('member_digest'))
                or (saved['family'], saved['member_digest']) != (family, group.get('member_digest'))
                or bool(saved['batch_eligible']) != bool(manifest['batch_eligible'])
                or group.get('batch_eligible') != bool(saved['batch_eligible'])):
            structure.append('FROZEN_GROUP_METADATA_CHANGED')
        if con.execute('SELECT 1 FROM batch_review_decision WHERE manifest_id=?', (group_id,)).fetchone():
            structure.append('GROUP_ALREADY_DECIDED')
        members = []
        try:
            members = [{'sku': row['sku'], 'expected_state': json.loads(row['expected_state'])}
                       for row in con.execute('SELECT sku,expected_state FROM batch_review_member WHERE manifest_id=? ORDER BY sku', (group_id,))]
            digest = _sha(_member_payload(manifest['marketplace'], manifest['api_marketplace'], manifest['family'],
                                         members, manifest['batch_eligible']))
            if digest != manifest['member_sha']:
                structure.append('FROZEN_MEMBER_HASH_CHANGED')
        except (ValueError, TypeError):
            structure.append('INVALID_FROZEN_MEMBER_STATE')
        expected = {member['sku']: member['expected_state'].get('asin') for member in members}
        visible = indexed[group_id]
        if (type(group.get('sku_count')) is not int or group['sku_count'] != len(members)
                or manifest['member_count'] != len(members) or len(visible) != len(expected)
                or len({item.get('sku') for item in visible}) != len(visible)
                or {item.get('sku'): item.get('asin') for item in visible} != expected):
            structure.append('VISIBLE_MEMBERSHIP_CHANGED')
        if any(item.get(key) != group.get(key) for item in visible
               for key in ('snapshot_id', 'member_digest', 'marketplace', 'api_marketplace', 'candidate_family')):
            structure.append('VISIBLE_MEMBER_SCOPE_CHANGED')
        if not manifest['batch_eligible'] or not family:
            approval.append('GROUP_REQUIRES_INDIVIDUAL_REVIEW')
        for member in members:
            row = current.get((marketplace, member['sku']))
            if row is None:
                structure.append('REVIEW_MEMBER_MISSING_FROM_DATABASE')
                continue
            if _expected_state(con, row) != member['expected_state']:
                structure.append('STALE_REVIEW_STATE')
            if row['api_marketplace'] != api_marketplace:
                structure.append('MEMBER_API_MARKETPLACE_MISMATCH')
            if row['mapping_basis'] != 'USER_WORKBOOK_EXACT_SKU' and row['mapping_status'] != 'APPROVED':
                approval.append('EXACT_REFERENCE_REQUIRED')
            if row['format_issue']:
                approval.append('REFERENCE_FORMAT_WARNING_REQUIRES_MANUAL_RESOLUTION')
            if row['mapping_conflict'] or row['mapping_status'] == 'FAMILY_CONFLICT':
                approval.append('MAPPING_CONFLICT')
            identities = json.loads(row['inventory_identities'])
            if not row['asin'] or not identities or {identity.get('asin') for identity in identities} != {row['asin']}:
                approval.append('AMBIGUOUS_SKU_ASIN_IDENTITY')
            master = con.execute('SELECT family,asin FROM sku WHERE marketplace=? AND sku=?',
                                 (marketplace, row['sku'])).fetchone()
            if not master or master['asin'] != row['asin']:
                approval.append('CORE_SKU_ASIN_CONFLICT')
            elif any(existing and existing != family for existing in (row['family'], master['family'])):
                approval.append('PROTECTED_FORMAL_MAPPING')
            if row['mapping_status'] == 'APPROVED' and row['family'] == family:
                warnings.append('EXISTING_MANUAL_APPROVAL_PRESERVED')
        member_ids = {(marketplace, member['sku']) for member in members}
        triggers = member_ids & (direct | disabled)
        known = (marketplace, family) in family_stops
        stopped = member_ids if known or (triggers and family) else triggers
        outside = {(row['marketplace'], row['sku']) for row in current_rows
                   if row['marketplace'] == marketplace and _confirmed_stop_family(row) == family} - scope
        result['stop_risk'].update(direct_stopped_member_count=len(triggers),
                                   projected_stopped_member_count=len(stopped),
                                   outside_review_formal_child_count=len(outside) if known or triggers else 0)
        if known:
            warnings.append('APPROVED_MEMBERS_INHERIT_EXISTING_FAMILY_STOP')
        if triggers and family:
            warnings.append('APPROVAL_ESTABLISHES_FAMILY_STOP_FROM_STOPPED_MEMBER')
        if outside and (known or triggers):
            warnings.append('FAMILY_STOP_AFFECTS_OUTSIDE_FIRST_BATCH')
        if stopped:
            risk_ids.update(stopped)
        structure[:], approval[:], warnings[:] = sorted(set(structure)), sorted(set(approval)), sorted(set(warnings))
        result['blockers'] = sorted(set(structure + approval))
        result['structure_status'] = 'BLOCKED' if structure else 'READY'
        result['approval_status'] = 'BLOCKED' if structure or approval else 'READY'
        if action == 'APPROVE' and not result['blockers']:
            planned.update({identity: family for identity in member_ids})
    # A selected stopped child can introduce a family stop that also affects
    # another selected frozen group. It never approves either group's future
    # children or changes the identities outside these exact collections.
    projected_stops = family_stops | {(marketplace, family) for (marketplace, sku), family
                                     in planned.items() if (marketplace, sku) in direct | disabled}
    for result in report['groups']:
        group_id = result['group_id']
        group = next(group for group in groups if group['group_id'] == group_id)
        family, marketplace = group['candidate_family'], group['marketplace']
        if (result['decision'] == 'APPROVE' and not result['blockers'] and
                (marketplace, family) in projected_stops):
            member_ids = {(marketplace, item['sku']) for item in indexed[group_id]}
            risk_ids.update(member_ids)
            result['stop_risk']['projected_stopped_member_count'] = len(member_ids)
            if (marketplace, family) not in family_stops:
                result['warnings'] = sorted(set(result['warnings'] + ['SELECTED_GROUPS_INTRODUCE_FAMILY_STOP']))
    report['stops'] = _stop_preview(stops, current_rows, planned, scope)
    selected_groups = [row for row in report['groups'] if row['decision'] in ('APPROVE', 'REJECT')]
    blocked_selected = [row for row in selected_groups if row['structure_blockers'] or
                        (row['decision'] == 'APPROVE' and row['approval_blockers'])]
    report['global_blockers'] = sorted(set(global_issues))
    report['structure_status'] = 'BLOCKED' if global_issues or any(
        row['structure_status'] == 'BLOCKED' for row in report['groups']) else 'READY'
    has_stops = any(row['status'] != 'NO_CHANGE' for row in report['stops'])
    report['decision_status'] = 'SIMULATED' if selected_groups or has_stops else 'PENDING'
    if report['structure_status'] == 'BLOCKED' or blocked_selected or any(row['status'] == 'BLOCKED' for row in report['stops']):
        report['status'] = 'BLOCKED'
    elif selected_groups or has_stops:
        report['status'] = 'SIMULATED_REQUIRES_EXPLICIT_CONFIRMATION'
    report['summary'] = {
        'group_count': len(groups), 'sku_count': len(items),
        'structurally_ready_group_count': sum(row['structure_status'] == 'READY' for row in report['groups']),
        'approval_ready_group_count': sum(row['approval_status'] == 'READY' for row in report['groups']),
        'blocked_group_count': sum(row['approval_status'] == 'BLOCKED' for row in report['groups']),
        'pending_group_count': sum(row['decision'] == 'NEEDS_REVIEW' for row in report['groups']),
        'approve_simulated_group_count': sum(row['decision'] == 'APPROVE' and not row['blockers'] for row in report['groups']),
        'reject_simulated_group_count': sum(row['decision'] == 'REJECT' and not row['structure_blockers'] for row in report['groups']),
        'blocked_selected_group_count': len(blocked_selected),
        'stop_risk_sku_count': len(risk_ids), 'future_skus_auto_approved': 0,
        'inventory_unknown_sku_count': sum(any(type(item.get(key)) is not int or item[key] < 0 for key in (
            'fulfillable', 'reserved', 'unfulfillable', 'inbound_working', 'inbound_shipped', 'inbound_receiving')) for item in items),
        'sales_unknown_sku_count': sum(item.get('recent_sales_status') in (None, 'UNKNOWN') for item in items),
    }
    return report
