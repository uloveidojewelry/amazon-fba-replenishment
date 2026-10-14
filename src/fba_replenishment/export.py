"""Auditable Excel workbook containing actual planning inputs, never formulas/PII."""
from collections import defaultdict
from datetime import date, timedelta
import math

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from .mock import is_mock_database

COLUMNS = ['marketplace', 'sku', 'family', 'asin', 'sales_30d', 'observed_sales_days',
           'daily_sales', 'fulfillable', 'reserved', 'unfulfillable', 'stock_observed_at',
           'stock_age_days', 'eligible_inbound', 'unknown_eta_inbound', 'coverage_days',
           'status', 'stop_reason', 'recommended_qty', 'asof', 'window_start',
           'lead_days', 'target_days', 'safety_days', 'moq', 'case_pack', 'sales_timezone']

WINDOWS = (7, 30, 60, 90)
ACCEPTANCE_COLUMNS = ['sales_7d', 'sales_60d', 'sales_90d'] + [
    f'{field}_{days}d' for days in WINDOWS
    for field in ('observed_sales', 'observed_sales_days', 'sales_complete', 'sales_completeness')
] + ['inventory_complete', 'inbound_working', 'inbound_shipped', 'inbound_receiving',
     'inventory_inbound', 'sales_source', 'inventory_source', 'inbound_source']
ANOMALY_COLUMNS = ['marketplace', 'sku', 'family', 'code', 'severity', 'message', 'source']
COLLECTION_COLUMNS = ['at', 'stage', 'kind', 'status', 'row_count', 'pages', 'window_start',
                      'window_end', 'source', 'detail']
DISCOVERY_COLUMNS = ['SKU', 'ASIN', 'Marketplace', '候选系列', '正式系列', '匹配依据',
                     '审核状态', '补货状态', '首次发现时间', '最近同步时间',
                     '安全停补系列', '建议补货数量']
# Export only explicit, operational metrics. Never serialize arbitrary API responses,
# credentials, exception objects, or customer fields passed by a caller.
SUMMARY_KEYS = {
    'collection_status', 'data_source', 'asof', 'window_start', 'window_end',
    'orders_collected', 'orders_canceled', 'order_lines_collected', 'sku_count',
    'inventory_sku_count', 'inventory_complete_sku_count', 'inventory_coverage',
    'sales_complete_sku_count', 'sales_completeness', 'sales_window_days',
    'stopped_family_count', 'calculable_sku_count', 'blocked_sku_count',
    'inbound_shipments_collected', 'inbound_v2024_status', 'inbound_v0_status',
    'inbound_units_reconciled', 'sample_orders', 'sample_inventory_skus',
    'sample_order_lines', 'quality_gate', 'next_action',
    'mapping_candidate_family_count', 'mapping_unknown_sku_count',
    'mapping_pending_sku_count', 'catalog_success_count', 'catalog_failed_count',
    'new_sku_count', 'latest_run_new_sku_count', 'pending_review_count',
    'stop_inherited_count', 'suspected_inactive_count', 'discovery_run_id',
    'stop_list_review_status', 'inventory_coverage_denominator',
    'sales_sample_skus_in_full_inventory', 'sales_sample_skus_absent_from_full_inventory',
    'order_item_count', 'catalog_status', 'observed_at',
    'discovered_sku_count', 'new_sku_count', 'pending_review_sku_count',
    'approved_sku_count', 'conflict_sku_count', 'stopped_sku_count',
    'inactive_sku_count', 'suspected_inactive_sku_count', 'auto_matched_sku_count',
    'rule_count', 'approved_rule_count', 'unapproved_rule_count',
}


def _scalar(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return None


def _quantity(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 and int(value) == value)


def sheet(wb, name, columns, rows):
    ws = wb.create_sheet(name)
    ws.append(columns)
    for row_index, row in enumerate(rows, 2):
        ws.append([_scalar(row.get(key)) for key in columns])
        # Treat all external strings as literal text, preventing formula injection.
        # max_row and row indexing scan the growing cell dictionary. Explicit
        # coordinates keep a full real SKU export linear in its cell count.
        for column_index in range(1, len(columns) + 1):
            cell = ws.cell(row=row_index, column=column_index)
            if isinstance(cell.value, str):
                cell.data_type = 's'
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F4E78')
    for index, key in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(index)].width = max(16, min(42, len(key) + 6))
    return ws


def _sales_windows(con, marketplace, sku, asof):
    """Observed totals are distinct from certified, complete calendar windows."""
    result = {}
    if con is None or not asof:
        return result
    end = date.fromisoformat(asof)
    start = end - timedelta(days=89)
    daily = {r['day']: r['units'] for r in con.execute(
        'SELECT day,units FROM sales WHERE marketplace=? AND sku=? AND day>=? AND day<=?',
        (marketplace, sku, start.isoformat(), end.isoformat()))}
    for days in WINDOWS:
        calendar = [(end - timedelta(days=i)).isoformat() for i in range(days)]
        known = [daily[day] for day in calendar if day in daily and _quantity(daily[day])]
        result[f'observed_sales_days_{days}d'] = len(known)
        result[f'observed_sales_{days}d'] = sum(known) if known else None
        result[f'sales_complete_{days}d'] = len(known) == days
        result[f'sales_{days}d'] = sum(known) if len(known) == days else None
    return result


def _acceptance_rows(rows, acceptance, con):
    summary = acceptance.get('summary') or {}
    per_sku = acceptance.get('per_sku') or {}
    partial = summary.get('collection_status') in {
        'SAMPLE_ONLY', 'FAILED_QUALITY_GATE', 'PARTIAL', 'FAILED', 'INCOMPLETE',
    }
    result, anomalies = [], []
    failed_sales = set()
    if con is not None:
        for market in {r['marketplace'] for r in rows}:
            latest = con.execute("SELECT status FROM sync_log WHERE marketplace=? AND kind='sales' ORDER BY id DESC LIMIT 1", (market,)).fetchone()
            if latest is not None and latest['status'] != 'SUCCESS':
                failed_sales.add(market)
    for original in rows:
        row = dict(original)
        additions = per_sku.get(row['sku']) or {}
        # A sample must never be promoted to a complete sales window using older
        # database rows. Explicitly certified per-SKU values may still be used.
        windows = {} if partial else _sales_windows(con, row['marketplace'], row['sku'], row.get('asof'))
        for days in WINDOWS:
            sales_key, count_key = f'sales_{days}d', f'observed_sales_days_{days}d'
            complete_key, observed_key = f'sales_complete_{days}d', f'observed_sales_{days}d'
            count = additions.get(count_key, row.get(count_key, windows.get(count_key)))
            if days == 30 and count is None and not partial:
                count = row.get('observed_sales_days')
            observed = additions.get(observed_key, row.get(observed_key, windows.get(observed_key)))
            total = additions.get(sales_key, row.get(sales_key, windows.get(sales_key)))
            if observed is None and _quantity(total):
                observed = total
            certified = additions.get(complete_key, row.get(complete_key, windows.get(complete_key)))
            if certified is None and not partial:
                certified = count == days
            complete = (certified is True and count == days and _quantity(total)
                        and row['marketplace'] not in failed_sales)
            row[sales_key] = total if complete else None
            row[observed_key] = observed
            row[count_key] = count
            row[complete_key] = complete
            row[f'sales_completeness_{days}d'] = min(1, count / days) if _quantity(count) else None
        row['observed_sales_days'] = row['observed_sales_days_30d']
        if not row['sales_complete_30d']:
            row['daily_sales'] = None
            row['coverage_days'] = None
        inventory_complete = additions.get('inventory_complete', row.get('inventory_complete'))
        if inventory_complete is None:
            inventory_complete = all(_quantity(row.get(key)) for key in ('fulfillable', 'reserved', 'unfulfillable'))
        row['inventory_complete'] = inventory_complete is True
        for key in ACCEPTANCE_COLUMNS:
            if key in additions and key != 'inventory_complete' and not key.startswith(('sales_', 'observed_sales')):
                row[key] = _scalar(additions[key])
        for key in ('sales_source', 'inventory_source', 'inbound_source'):
            row.setdefault(key, summary.get('data_source'))
        result.append(row)
    stopped = {(r['marketplace'], r['family']) for r in result if r.get('family') and r.get('status') == 'STOPPED'}
    for row in result:
        if (row['marketplace'], row.get('family')) in stopped:
            if row.get('status') != 'STOPPED' or row.get('recommended_qty') != 0:
                anomalies.append(dict(row, code='STOP_RULE_INCONSISTENCY', severity='BLOCKING',
                                      message='Export blocked all children of a stopped family', source='Export validation'))
            row['status'], row['recommended_qty'] = 'STOPPED', 0
            row['stop_reason'] = row.get('stop_reason') or 'Same-marketplace family stop'
        elif row.get('status') == 'ACTIVE' and not row.get('family'):
            row['status'], row['recommended_qty'] = 'FAMILY_REVIEW_REQUIRED', 0
            row['stop_reason'] = 'No confirmed family mapping'
        elif row.get('status') == 'ACTIVE' and (not row['sales_complete_30d'] or not row['inventory_complete']):
            anomalies.append(dict(row, code='UNCERTIFIED_PLANNING_INPUT', severity='BLOCKING',
                                  message='Sales or inventory is incomplete; recommendation blocked', source='Export validation'))
            row['status'], row['recommended_qty'] = 'DATA_REVIEW_REQUIRED', 0
            row['stop_reason'] = 'Sales or inventory completeness is not certified'
        if row.get('status') not in ('ACTIVE', 'STOPPED'):
            anomalies.append(dict(row, code=row.get('status'), severity='BLOCKING',
                                  message=row.get('stop_reason'), source='Planning validation'))
    return result, anomalies


def _known_sum(rows, key):
    values = [r.get(key) for r in rows]
    return sum(values) if values and all(_quantity(v) for v in values) else None


def _acceptance_sheets(wb, rows, acceptance, anomalies):
    families = defaultdict(list)
    for row in rows:
        if row.get('family'):
            families[(row['marketplace'], row['family'])].append(row)
    family_rows = []
    for (market, family), children in sorted(families.items()):
        stopped = any(r['status'] == 'STOPPED' for r in children)
        item = {'marketplace': market, 'family': family, 'sku_count': len(children),
                'status': 'STOPPED' if stopped else ('ACTIVE' if all(r['status'] == 'ACTIVE' for r in children) else 'BLOCKED'),
                'calculable_sku_count': sum(r['status'] == 'ACTIVE' for r in children),
                'blocked_sku_count': sum(r['status'] != 'ACTIVE' for r in children),
                'recommended_qty': 0 if stopped else _known_sum(children, 'recommended_qty'),
                'stop_reason': '; '.join(sorted({r['stop_reason'] for r in children if r.get('stop_reason')}))}
        for key in ['sales_7d', 'sales_30d', 'sales_60d', 'sales_90d', 'fulfillable', 'reserved', 'unfulfillable', 'eligible_inbound', 'unknown_eta_inbound']:
            item[key] = _known_sum(children, key)
        family_rows.append(item)
    family_columns = ['marketplace', 'family', 'sku_count', 'status', 'calculable_sku_count', 'blocked_sku_count',
                      'sales_7d', 'sales_30d', 'sales_60d', 'sales_90d', 'fulfillable', 'reserved',
                      'unfulfillable', 'eligible_inbound', 'unknown_eta_inbound', 'recommended_qty', 'stop_reason']
    sheet(wb, 'Family Summary', family_columns, family_rows)
    sheet(wb, 'Stopped Families', family_columns, [r for r in family_rows if r['status'] == 'STOPPED'])
    risks = []
    for row in rows:
        coverage = row.get('coverage_days')
        if row.get('status') != 'ACTIVE':
            risks.append(dict(row, risk='UNKNOWN_DATA' if row['status'] != 'STOPPED' else 'MANUAL_STOP',
                              risk_reason=row.get('stop_reason')))
        elif isinstance(coverage, (int, float)) and isinstance(row.get('lead_days'), (int, float)) and coverage < row['lead_days']:
            risks.append(dict(row, risk='STOCKOUT_BEFORE_REPLENISHMENT', risk_reason='Coverage shorter than lead time'))
    sheet(wb, 'Stockout Risk', ['marketplace', 'sku', 'family', 'risk', 'risk_reason', 'fulfillable',
                              'daily_sales', 'coverage_days', 'lead_days', 'status', 'recommended_qty'], risks)
    sheet(wb, 'Data Anomalies', ANOMALY_COLUMNS, anomalies + list(acceptance.get('anomalies') or []))
    sheet(wb, 'Collection Audit', COLLECTION_COLUMNS, acceptance.get('audit') or [])
    count = len(rows)
    summary = {k: _scalar(v) for k, v in (acceptance.get('summary') or {}).items() if k in SUMMARY_KEYS}
    # These metrics describe exactly the rows and quantities in this workbook.
    summary.update({'sku_count': count, 'stopped_family_count': sum(r['status'] == 'STOPPED' for r in family_rows),
                    'calculable_sku_count': sum(r['status'] == 'ACTIVE' for r in rows),
                    'blocked_sku_count': sum(r['status'] != 'ACTIVE' for r in rows),
                    'inventory_complete_sku_count': sum(r['inventory_complete'] is True for r in rows),
                    'inventory_coverage': sum(r['inventory_complete'] is True for r in rows) / count if count else None,
                    'sales_complete_sku_count': sum(r['sales_complete_90d'] for r in rows),
                    'sales_completeness': sum(r['sales_complete_90d'] for r in rows) / count if count else None,
                    'sales_window_days': 90})
    supplied = acceptance.get('summary') or {}
    if supplied.get('stopped_family_count', 0) is None and summary['stopped_family_count'] == 0:
        summary['stopped_family_count'] = None
    if supplied.get('sales_completeness', 0) is None and str(supplied.get('collection_status', '')).upper() in ('SAMPLE_ONLY', 'FAILED_QUALITY_GATE', 'PARTIAL'):
        summary['sales_completeness'] = None
    sheet(wb, 'Acceptance', ['metric', 'value'], [{'metric': k, 'value': v} for k, v in summary.items()])


def _discovery_sheets(wb, discovery):
    """Keep discovered candidates distinct from manually approved families."""
    rows = []
    stopped_families = set()
    for original in discovery:
        mapping_status = original.get('review_status', original.get('mapping_status'))
        replenishment_status = original.get('replenishment_status', original.get('status'))
        status = str(original.get('status') or replenishment_status or '').upper()
        mapping = str(mapping_status or '').upper()
        replenishment = str(replenishment_status or status).upper()
        conflict = (original.get('mapping_conflict') is True or 'CONFLICT' in mapping
                    or 'CONFLICT' in replenishment or 'CONFLICT' in status)
        stopped = status in ('STOPPED', 'FAMILY_STOPPED') or replenishment in ('STOPPED', 'FAMILY_STOPPED')
        recommended_qty = original.get('recommended_qty')
        # Discovery approval alone cannot certify inventory, ETA or sales data.
        # Only an explicit active planning result may carry a positive quantity.
        calculable = (mapping == 'APPROVED' and replenishment == 'ACTIVE'
                      and not stopped and not conflict and _quantity(recommended_qty))
        effective_family = original.get('effective_family') or original.get('family')
        if stopped and effective_family:
            stopped_families.add((original.get('marketplace'), effective_family))
        row = {
            'SKU': _scalar(original.get('sku')), 'ASIN': _scalar(original.get('asin')),
            'Marketplace': _scalar(original.get('marketplace')),
            '候选系列': _scalar(original.get('candidate_family')),
            '正式系列': _scalar(original.get('family')),
            '匹配依据': _scalar(original.get('mapping_basis')),
            '审核状态': _scalar(mapping_status),
            '补货状态': _scalar(replenishment_status or original.get('status')),
            '首次发现时间': _scalar(original.get('first_discovered_at')),
            '最近同步时间': _scalar(original.get('last_synced_at', original.get('last_seen_at'))),
            '安全停补系列': _scalar(effective_family) if stopped else None,
            '建议补货数量': recommended_qty if calculable else 0,
            '_is_new': original.get('is_new') in (True, 1),
            '_pending': mapping != 'APPROVED', '_conflict': conflict, '_stopped': stopped,
            '_effective_family': effective_family,
        }
        rows.append(row)
    for row in rows:
        if (row['Marketplace'], row['_effective_family']) in stopped_families:
            row['_stopped'] = True
            row['安全停补系列'] = _scalar(row['_effective_family'])
            row['补货状态'], row['建议补货数量'] = 'STOPPED', 0
    groups = [
        ('新发现 SKU', [r for r in rows if r['_is_new']]),
        ('待审核 SKU', [r for r in rows if r['_pending']]),
        ('SKU 系列映射', rows),
        ('已停补系列', [r for r in rows if r['_stopped']]),
        ('系列映射冲突', [r for r in rows if r['_conflict']]),
    ]
    for name, data in groups:
        sheet(wb, name, DISCOVERY_COLUMNS, data)


def _database_discovery(con, marketplace):
    if con is None or not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_sku'"
    ).fetchone():
        return None
    # Discovery has no dependency on export. Load it only for databases with the
    # additive schema, preserving old database and export behavior.
    from .discovery import discovery_export_rows
    rows = discovery_export_rows(con, marketplace)
    return rows or None


def export(rows, path, con=None, marketplace=None, settings=None, acceptance=None, discovery=None):
    rows = [dict(row) for row in rows]
    marketplace = marketplace or (rows[0]['marketplace'] if rows else 'US')
    anomalies = []
    if acceptance is not None:
        rows, anomalies = _acceptance_rows(rows, acceptance, con)
    wb = Workbook()
    wb.remove(wb.active)
    sheet(wb, 'Replenishment', COLUMNS + (ACCEPTANCE_COLUMNS if acceptance is not None else []), rows)
    alerts = [dict(row) for row in rows if row['status'] != 'ACTIVE']
    sheet(wb, 'Alerts', ['sku', 'family', 'status', 'stop_reason', 'recommended_qty'], alerts)
    tables = {
        'Sales': ('sales', ['marketplace', 'sku', 'day', 'units']),
        'Inventory': ('stock', ['marketplace', 'sku', 'fulfillable', 'reserved', 'unfulfillable', 'observed_at']),
        'Inbound': ('inbound', ['marketplace', 'sku', 'shipment_id', 'units', 'eta', 'status']),
        'Orders': ('order_line', ['marketplace', 'order_id', 'item_id', 'sku', 'day', 'status', 'units']),
        'Sync Log': ('sync_log', ['id', 'at', 'marketplace', 'kind', 'status', 'row_count', 'detail']),
        'Audit': ('audit', ['id', 'at', 'action', 'marketplace', 'sku', 'detail']),
    }
    for name, (table, columns) in tables.items():
        data = [dict(r) for r in con.execute(f'SELECT {",".join(columns)} FROM {table} WHERE marketplace=?', (marketplace,))] if con is not None else []
        sheet(wb, name, columns, data)
    parameters = dict(settings or {})
    if rows:
        for key in ['asof', 'window_start', 'lead_days', 'target_days', 'safety_days', 'moq', 'case_pack', 'sales_timezone']:
            if parameters.get(key) is None:
                parameters[key] = rows[0].get(key)
    parameters.update({'marketplace': marketplace, 'mode': 'SIMULATED_READ_ONLY' if con is not None and is_mock_database(con) else 'READ_ONLY',
                       'sales_basis': 'Daily gross ordered AFN units; canceled orders excluded; not net returns',
                       'formula': 'ceil to case pack(max(MOQ, daily_sales * horizon - fulfillable - eligible_inbound)) if positive',
                       'inbound_policy': 'Unknown ETA blocks planning; terminal/working shipments excluded',
                       'snapshot_history': 'SQLite snapshots table; no customer PII stored'})
    sheet(wb, 'Parameters', ['parameter', 'value'], [{'parameter': k, 'value': v} for k, v in parameters.items()])
    if acceptance is not None:
        _acceptance_sheets(wb, rows, acceptance, anomalies)
    if discovery is None:
        discovery = _database_discovery(con, marketplace)
    if discovery is not None:
        _discovery_sheets(wb, discovery)
    wb.save(path)


def export_mapping_review(records, mapping_path, stop_path, summary=None):
    """Create unapproved review workbooks; no flags are inferred or imported.

    Parent relationships and SKU spelling can suggest candidates but cannot approve
    a business family or declare that a product is allowed to replenish.
    """
    records = list(records)
    summary = summary or {}
    metrics = summary.get('summary', summary)
    metric_rows = [{'metric': key, 'value': _scalar(value)} for key, value in metrics.items()
                   if key in SUMMARY_KEYS]
    audit = summary.get('audit') or []
    mapping_rows, stop_rows = [], []
    for original in records:
        parent_asins = original.get('parent_asins')
        if isinstance(parent_asins, (list, tuple)):
            parent_asins = '; '.join(sorted({v for v in parent_asins if isinstance(v, str)}))
        row = {key: _scalar(original.get(key)) for key in (
            'marketplace', 'sku', 'asin', 'candidate_family', 'mapping_basis',
            'catalog_status', 'fulfillable', 'reserved', 'unfulfillable',
            'inbound_working', 'inbound_shipped', 'inbound_receiving',
        )}
        row.update({'parent_asins': _scalar(parent_asins), 'review_status': 'UNAPPROVED',
                    'recommended_qty': 0, 'reviewer': None, 'reviewed_at': None,
                    'approved_family': None})
        mapping_rows.append(row)
        stop_rows.append({key: row.get(key) for key in ('marketplace', 'sku', 'asin', 'candidate_family')})
        stop_rows[-1].update({'family': None, 'stop_replenishment': None, 'reason': None,
                             'effective_date': None, 'review_status': 'UNAPPROVED',
                             'recommended_qty': 0, 'reviewer': None, 'reviewed_at': None})
    mapping_columns = ['marketplace', 'sku', 'asin', 'candidate_family', 'mapping_basis',
                       'review_status', 'parent_asins', 'catalog_status', 'fulfillable',
                       'reserved', 'unfulfillable', 'inbound_working', 'inbound_shipped',
                       'inbound_receiving', 'recommended_qty', 'approved_family', 'reviewer', 'reviewed_at']
    stop_columns = ['marketplace', 'sku', 'asin', 'family', 'candidate_family', 'stop_replenishment',
                    'reason', 'effective_date', 'review_status', 'recommended_qty', 'reviewer', 'reviewed_at']
    instructions = [
        {'topic': '用途', 'instruction': '仅供人工审核的美国站 SKU 系列映射和停补名单；本文件不批准、不导入、不执行补货。'},
        {'topic': '数据范围', 'instruction': '使用已授权的小批量只读采集及库存/目录标识；未扩大到完整 90 天采集。具体采集范围见 Collection Audit。'},
        {'topic': '未审核', 'instruction': '全部行保持 UNAPPROVED，建议补货数量为 0。候选系列、目录父体和 SKU 命名均不能自动视作业务系列批准。'},
        {'topic': '映射审核', 'instruction': '逐个核对 SKU 与 ASIN，查验候选系列及 mapping_basis、parent_asins；将确认后的业务系列填写到 approved_family，并填写 reviewer、reviewed_at。'},
        {'topic': '系列含义', 'instruction': '目录父 ASIN 只表示目录变体关系；同一业务系列可能需要人工归并多个父体，或拆分错误候选。未知候选保持空白并待确认。'},
        {'topic': '停补审核', 'instruction': '先填写批准后的 family，再明确填写 stop_replenishment；空白表示未知，不能理解为 FALSE。停补时填写 reason、effective_date、reviewer 和 reviewed_at。'},
        {'topic': '系列停补', 'instruction': '任意一个子体被人工标记停止补货，同站点同批准系列的全部子体建议补货数量必须为 0；恢复需要单独人工批准。'},
        {'topic': '审批后处理', 'instruction': '审核文件需由负责人员明确批准后另行导入并校验；仅在工作簿填写 APPROVED 不会自动更新系统。'},
        {'topic': '安全拦截', 'instruction': '映射、停补状态、销量、库存或 ETA 未确认时继续拦截；不得将未知数据填为 0 或推断为允许补货。'},
        {'topic': '只读边界', 'instruction': '审核及建议不创建 FBA 货件，不修改 Listing、库存或价格，不保存 Amazon 凭据或客户个人信息。'},
    ]
    for path, name, columns, data in (
        (mapping_path, 'SKU Mapping Review', mapping_columns, mapping_rows),
        (stop_path, 'Stop List Review', stop_columns, stop_rows),
    ):
        wb = Workbook()
        wb.remove(wb.active)
        sheet(wb, name, columns, data)
        readme = sheet(wb, 'Read Me', ['topic', 'instruction'], instructions)
        readme.column_dimensions['B'].width = 110
        for cell in readme['B']:
            cell.alignment = Alignment(wrap_text=True, vertical='top')
        for index in range(2, readme.max_row + 1):
            readme.row_dimensions[index].height = 36
        sheet(wb, 'Collection Audit', COLLECTION_COLUMNS, audit)
        sheet(wb, 'Review Summary', ['metric', 'value'], metric_rows)
        wb.save(path)
