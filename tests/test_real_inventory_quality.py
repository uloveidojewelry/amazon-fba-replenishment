"""Optional acceptance checks on the locally retained sanitized live snapshot.

No network requests or credentials are used. CI without a real snapshot skips
these checks; the development acceptance run verifies actual collected data.
"""
import json
from pathlib import Path
import re
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'output/live-acceptance-2026-10-13/full-inventory.json'
DATABASE = ROOT / 'output/sku-review/review.db'


@pytest.mark.skipif(not DATASET.exists(), reason='No local sanitized real inventory acceptance snapshot')
def test_real_inventory_identities_and_all_quantity_categories_are_available():
    rows = json.loads(DATASET.read_text(encoding='utf-8'))
    identities = set()
    for row in rows:
        assert row['sellerSku'] and re.fullmatch(r'[A-Z0-9]{10}', row['asin'])
        identity = row['sellerSku'], row['fnSku'], row['condition']
        assert identity not in identities
        identities.add(identity)
        details = row['inventoryDetails']
        quantities = [details[key] for key in ('fulfillableQuantity','inboundWorkingQuantity',
                                              'inboundShippedQuantity','inboundReceivingQuantity')]
        quantities += [details['reservedQuantity']['totalReservedQuantity'],
                       details['unfulfillableQuantity']['totalUnfulfillableQuantity']]
        assert all(type(value) is int and value >= 0 for value in quantities)
    assert rows


@pytest.mark.skipif(not DATASET.exists() or not DATABASE.exists(), reason='No locally retained real SKU review database')
def test_real_discovery_covers_snapshot_and_unapproved_mapping_is_blocked():
    from fba_replenishment.core import calculate
    rows = json.loads(DATASET.read_text(encoding='utf-8'))
    expected = {r['sellerSku'] for r in rows}
    con = sqlite3.connect(f'file:{DATABASE}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        masters = {r['sku']:r for r in con.execute('SELECT * FROM discovery_sku WHERE api_marketplace=?',('ATVPDKIKX0DER',))}
        assert expected.issubset(masters)
        for planned in calculate(con):
            if masters[planned['sku']]['mapping_status'] != 'APPROVED' or masters[planned['sku']]['replenishment_status'] == 'STOPPED':
                assert planned['recommended_qty'] == 0
        assert con.execute('SELECT COUNT(*) FROM stock WHERE marketplace=?',('US',)).fetchone()[0] == len(expected)
    finally:
        con.close()
