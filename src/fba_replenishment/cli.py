import argparse
import os
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from .core import calculate, connect, import_csv, resume, stop
from .export import export
from .spapi import SPAPIClient, SPAPIError
from .sync import sync
from .diagnostics import diagnose, diagnostic_exit_code
from .mock import MockSPAPIClient, prepare_mock_database, is_mock_database


def main():
    parser = argparse.ArgumentParser(description='Read-only Amazon FBA replenishment planning')
    parser.add_argument('--db', help='SQLite path; mock defaults to output/mock-fba.db, otherwise data/fba.db')
    parser.add_argument('--marketplace', default='US', help='Local marketplace namespace; keep consistent with API marketplace')
    commands = parser.add_subparsers(dest='command', required=True)
    imp = commands.add_parser('import')
    imp.add_argument('kind', choices=['sku', 'sales', 'stock', 'inbound'])
    imp.add_argument('csv')
    st = commands.add_parser('stop')
    st.add_argument('sku')
    st.add_argument('--reason', required=True)
    rs = commands.add_parser('resume')
    rs.add_argument('sku')
    rs.add_argument('--approved-by', required=True)
    collect = commands.add_parser('sync', help='Read-only GET collection, followed by local SQLite reconciliation')
    collect.add_argument('--kind', choices=['all', 'sales', 'stock', 'inbound'], default='all')
    collect.add_argument('--asof', help='Last complete sales day YYYY-MM-DD')
    collect.add_argument('--timezone', default=os.environ.get('SP_API_TIMEZONE', 'UTC'), help='IANA timezone used for sales daily buckets')
    collect.add_argument('--mock', action='store_true', help='Synthetic store in an isolated mock database; no HTTP/credentials')
    collect.add_argument('--confirm-read-only', action='store_true', help='Explicitly permit LWA authentication and read-only store requests')
    doctor = commands.add_parser('diagnose-spapi', help='Offline configuration report by default; never prints credentials')
    modes = doctor.add_mutually_exclusive_group()
    modes.add_argument('--mock', action='store_true', help='Exercise synthetic collection and family stop without HTTP')
    modes.add_argument('--live', action='store_true', help='Prepare a real first-page connection smoke test')
    doctor.add_argument('--confirm-read-only', action='store_true', help='Required alongside --live to perform real requests')
    review = commands.add_parser('review-skus', help='Full current FBA inventory and at most one Catalog relationship batch; no sales history sync')
    review.add_argument('--confirm-read-only', action='store_true')
    review.add_argument('--catalog-sample-size', type=int, default=20)
    review.add_argument('--output-dir', default='output/sku-review')
    discovery = commands.add_parser('discover-skus', help='Persistent current FBA SKU discovery; candidates require manual approval')
    discovery.add_argument('--confirm-read-only', action='store_true')
    discovery.add_argument('--source-xlsx', help='User SKU reference workbook; never implies approval')
    discovery.add_argument('--daily', action='store_true', help='Skip when this marketplace already completed discovery on its business day')
    discovery.add_argument('--catalog-sample-size', type=int, default=20)
    discovery.add_argument('--output-dir', default='output/sku-review')
    approval = commands.add_parser('approve-sku', help='Explicit local family mapping approval; does not resume stops')
    approval.add_argument('sku')
    approval.add_argument('--family', required=True)
    approval.add_argument('--approved-by', required=True)
    rule = commands.add_parser('approve-naming-rule', help='Register an explicitly approved full-match rule; matched SKUs still await mapping review')
    rule.add_argument('--name', required=True)
    rule.add_argument('--pattern', required=True)
    rule.add_argument('--family-group', type=int, default=1)
    rule.add_argument('--approved-by', required=True)
    plan = commands.add_parser('plan')
    for name, default in [('lead-days', 35), ('target-days', 75), ('safety-days', 0), ('moq', 1), ('case-pack', 1), ('max-stock-age-days', 2)]:
        plan.add_argument('--' + name, type=int, default=default)
    plan.add_argument('--asof', help='Last complete sales day; default previous day in synced sales timezone (UTC for CSV)')
    plan.add_argument('--output', default='output/replenishment.xlsx')
    args = parser.parse_args()
    if args.command == 'diagnose-spapi':
        report = diagnose(mock=args.mock, live=args.live, confirm_read_only=args.confirm_read_only)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if diagnostic_exit_code(report):
            parser.exit(1)
        return
    if args.command in ('review-skus', 'discover-skus') and not args.confirm_read_only:
        parser.error('Real SKU review requires --confirm-read-only')
    if args.command == 'sync' and not args.mock and not args.confirm_read_only:
        parser.error('Live sync requires --confirm-read-only; use --mock for offline data')
    db = args.db or ('output/sku-review/review.db' if args.command in ('review-skus', 'discover-skus') else
                    'output/mock-fba.db' if args.command == 'sync' and args.mock else 'data/fba.db')
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    try:
        if args.command == 'import':
            print(f'Imported {import_csv(con, args.kind, args.csv, args.marketplace)} rows')
        elif args.command == 'stop':
            from .discovery import ensure_schema, mark_stop
            ensure_schema(con)
            if con.execute('SELECT 1 FROM discovery_sku WHERE marketplace=? AND sku=?', (args.marketplace,args.sku)).fetchone():
                mark_stop(con, args.marketplace, args.sku, args.reason)
            else:
                stop(con, args.marketplace, args.sku, args.reason)
            print('Family stop active')
        elif args.command == 'resume':
            from .discovery import ensure_schema, manual_resume
            ensure_schema(con)
            if con.execute('SELECT 1 FROM discovery_sku WHERE marketplace=? AND sku=?', (args.marketplace,args.sku)).fetchone():
                manual_resume(con, args.marketplace, args.sku, args.approved_by)
            else:
                resume(con, args.marketplace, args.sku, args.approved_by)
            print('Approved stop released; remaining family flags still apply')
        elif args.command == 'sync':
            if args.mock:
                day = date.fromisoformat(args.asof) if args.asof else datetime.now(ZoneInfo(args.timezone)).date() - timedelta(days=1)
                prepare_mock_database(con, args.marketplace, day)
                client = MockSPAPIClient(args.timezone)
                print('SIMULATED DATA ONLY:', db)
            else:
                if is_mock_database(con):
                    raise ValueError('Live sync cannot write into a mock database')
                client = SPAPIClient(allow_live=True)
            counts = sync(con, client, args.marketplace, args.kind, args.asof, args.timezone)
            print('Read-only collection complete:', counts)
        elif args.command == 'plan':
            settings = {key: getattr(args, key) for key in ['lead_days', 'target_days', 'safety_days', 'moq', 'case_pack', 'asof', 'max_stock_age_days']}
            rows = calculate(con, marketplace=args.marketplace, **settings)
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            export(rows, args.output, con, args.marketplace, settings)
            for row in rows:
                print(row['sku'], row['status'], row['recommended_qty'])
            print('Wrote', args.output)
        elif args.command == 'review-skus':
            from .review import run_review
            result = run_review(con, SPAPIClient(allow_live=True), args.output_dir,
                                args.marketplace, args.catalog_sample_size)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == 'discover-skus':
            from .discovery_runner import run_discovery
            result = run_discovery(con, args.output_dir, args.marketplace, args.source_xlsx,
                                   args.daily, args.catalog_sample_size)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == 'approve-sku':
            from .discovery import approve_mapping
            approve_mapping(con, args.marketplace, args.sku, args.family, args.approved_by)
            print('Mapping approved locally; active stops and data-quality blocks remain enforced')
        elif args.command == 'approve-naming-rule':
            from .config import SPAPIConfig
            from .discovery import register_approved_rule
            register_approved_rule(con, args.marketplace, SPAPIConfig.from_env().marketplace,
                                   args.name, args.pattern, args.family_group, args.approved_by)
            print('Naming rule approved locally; no SKU mapping was automatically approved')
    except SPAPIError as error:
        parser.exit(1, f'{error.code}: {error}. {error.hint}\n')
    except (ValueError, KeyError, TypeError):
        # Avoid printing potentially sensitive exception bodies from external data.
        parser.exit(1, 'Operation failed: check required bindings, inputs, API permissions and local Sync Log.\n')
    finally:
        con.close()


if __name__ == '__main__':
    main()
