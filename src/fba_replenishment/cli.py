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
    if args.command == 'sync' and not args.mock and not args.confirm_read_only:
        parser.error('Live sync requires --confirm-read-only; use --mock for offline data')
    db = args.db or ('output/mock-fba.db' if args.command == 'sync' and args.mock else 'data/fba.db')
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    try:
        if args.command == 'import':
            print(f'Imported {import_csv(con, args.kind, args.csv, args.marketplace)} rows')
        elif args.command == 'stop':
            stop(con, args.marketplace, args.sku, args.reason)
            print('Family stop active')
        elif args.command == 'resume':
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
    except SPAPIError as error:
        parser.exit(1, f'{error.code}: {error}. {error.hint}\n')
    except (ValueError, KeyError, TypeError):
        # Avoid printing potentially sensitive exception bodies from external data.
        parser.exit(1, 'Operation failed: check required bindings, inputs, API permissions and local Sync Log.\n')
    finally:
        con.close()


if __name__ == '__main__':
    main()
