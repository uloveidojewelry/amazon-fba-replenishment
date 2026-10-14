import argparse
import os
from pathlib import Path

from .core import calculate, connect, import_csv, resume, stop
from .export import export
from .spapi import SPAPIClient, SPAPIError
from .sync import sync


def main():
    parser = argparse.ArgumentParser(description='Read-only Amazon FBA replenishment planning')
    parser.add_argument('--db', default='data/fba.db')
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
    plan = commands.add_parser('plan')
    for name, default in [('lead-days', 35), ('target-days', 75), ('safety-days', 0), ('moq', 1), ('case-pack', 1), ('max-stock-age-days', 2)]:
        plan.add_argument('--' + name, type=int, default=default)
    plan.add_argument('--asof', help='Last complete sales day; default previous day in synced sales timezone (UTC for CSV)')
    plan.add_argument('--output', default='output/replenishment.xlsx')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = connect(args.db)
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
            counts = sync(con, SPAPIClient(), args.marketplace, args.kind, args.asof, args.timezone)
            print('Read-only collection complete:', counts)
        elif args.command == 'plan':
            settings = {key: getattr(args, key) for key in ['lead_days', 'target_days', 'safety_days', 'moq', 'case_pack', 'asof', 'max_stock_age_days']}
            rows = calculate(con, marketplace=args.marketplace, **settings)
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            export(rows, args.output, con, args.marketplace, settings)
            for row in rows:
                print(row['sku'], row['status'], row['recommended_qty'])
            print('Wrote', args.output)
    except (SPAPIError, ValueError, KeyError, TypeError):
        # Avoid printing potentially sensitive exception bodies from external data.
        parser.exit(1, 'Operation failed: check required bindings, inputs, API permissions and local Sync Log.\n')
    finally:
        con.close()


if __name__ == '__main__':
    main()
