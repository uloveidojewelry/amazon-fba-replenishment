"""Only trusted series identities may create persistent family stop policy."""
import pytest

from fba_replenishment.core import connect
from fba_replenishment.discovery import (
    approve_mapping, discover, discovery_export_rows, mark_stop,
)


US = 'ATVPDKIKX0DER'
CA = 'A2EUQ1WTGCTBG2'
STAMP = '2026-10-14T12:00:00+00:00'


def inventory(sku):
    return {
        'sellerSku': sku, 'asin': 'B012345678', 'fnSku': 'FN-' + sku,
        'condition': 'NewItem',
        'inventoryDetails': {
            'fulfillableQuantity': 8,
            'reservedQuantity': {'totalReservedQuantity': 0},
            'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
            'inboundWorkingQuantity': 0, 'inboundShippedQuantity': 0,
            'inboundReceivingQuantity': 0,
        },
    }


@pytest.fixture
def con(tmp_path):
    connection = connect(tmp_path / 'stop-safety.db')
    yield connection
    connection.close()


def rows(con, marketplace='US'):
    return {row['sku']: row for row in discovery_export_rows(con, marketplace)}


def active_families(con, marketplace='US'):
    return {row['family'] for row in con.execute(
        'SELECT family FROM family_stop WHERE marketplace=? AND active=1',
        (marketplace,),
    )}


def test_approved_series_stop_does_not_propagate_stale_candidate(con):
    discover(con, [inventory(sku) for sku in ('trigger', 'formal-a', 'formal-b')],
             'US', US, observed_at=STAMP,
             source_candidates={'trigger': {'candidate_family': 'B'}})
    approve_mapping(con, 'US', 'trigger', 'A', 'reviewer', STAMP)
    approve_mapping(con, 'US', 'formal-a', 'A', 'reviewer', STAMP)
    approve_mapping(con, 'US', 'formal-b', 'B', 'reviewer', STAMP)

    mark_stop(con, 'US', 'trigger', 'Human stop on confirmed A family', STAMP)

    found = rows(con)
    assert found['trigger']['replenishment_status'] == 'STOPPED'
    assert found['formal-a']['replenishment_status'] == 'STOPPED'
    assert found['formal-b']['replenishment_status'] == 'MAPPING_APPROVED'
    assert active_families(con) == {'A'}


def test_candidate_only_direct_stop_cannot_create_family_policy(con):
    discover(con, [inventory('unconfirmed'), inventory('formal-b')],
             'US', US, observed_at=STAMP,
             source_candidates={'unconfirmed': {'candidate_family': 'B'}})
    approve_mapping(con, 'US', 'formal-b', 'B', 'reviewer', STAMP)

    mark_stop(con, 'US', 'unconfirmed', 'Stop this SKU pending identity review', STAMP)

    found = rows(con)
    assert found['unconfirmed']['replenishment_status'] == 'STOPPED'
    assert found['unconfirmed']['family'] is None
    assert found['unconfirmed']['recommended_qty'] == 0
    assert found['formal-b']['replenishment_status'] == 'MAPPING_APPROVED'
    assert active_families(con) == set()


def test_stopped_candidate_resolved_by_human_creates_only_confirmed_stop(con):
    discover(con, [inventory('unconfirmed'), inventory('formal-a'), inventory('formal-b')],
             'US', US, observed_at=STAMP,
             source_candidates={'unconfirmed': {'candidate_family': 'B'}})
    approve_mapping(con, 'US', 'formal-a', 'A', 'reviewer', STAMP)
    approve_mapping(con, 'US', 'formal-b', 'B', 'reviewer', STAMP)
    mark_stop(con, 'US', 'unconfirmed', 'Pending manual series decision', STAMP)

    approve_mapping(con, 'US', 'unconfirmed', 'A', 'reviewer', STAMP)

    found = rows(con)
    assert found['formal-a']['replenishment_status'] == 'STOPPED'
    assert found['formal-b']['replenishment_status'] == 'MAPPING_APPROVED'
    assert active_families(con) == {'A'}


def test_untrusted_conflicting_candidates_do_not_stop_either_formal_family(con):
    discover(con, [inventory('conflict'), inventory('formal-a'), inventory('formal-b')],
             'US', US, observed_at=STAMP,
             source_candidates={'conflict': {
                 'candidate_family': 'A', 'conflicting_families': ['B'],
             }})
    approve_mapping(con, 'US', 'formal-a', 'A', 'reviewer', STAMP)
    approve_mapping(con, 'US', 'formal-b', 'B', 'reviewer', STAMP)

    mark_stop(con, 'US', 'conflict', 'Do not replenish this ambiguous SKU', STAMP)

    found = rows(con)
    assert found['conflict']['replenishment_status'] == 'STOPPED'
    assert found['conflict']['recommended_qty'] == 0
    assert found['formal-a']['replenishment_status'] == 'MAPPING_APPROVED'
    assert found['formal-b']['replenishment_status'] == 'MAPPING_APPROVED'
    assert active_families(con) == set()


def test_formal_stop_is_isolated_by_marketplace(con):
    discover(con, [inventory('same-sku')], 'US', US, observed_at=STAMP,
             source_candidates={'same-sku': {'candidate_family': 'B'}})
    discover(con, [inventory('same-sku')], 'CA', CA, observed_at=STAMP)
    approve_mapping(con, 'US', 'same-sku', 'A', 'reviewer', STAMP)
    approve_mapping(con, 'CA', 'same-sku', 'A', 'reviewer', STAMP)

    mark_stop(con, 'US', 'same-sku', 'US series stop only', STAMP)

    assert rows(con)['same-sku']['replenishment_status'] == 'STOPPED'
    assert rows(con, 'CA')['same-sku']['replenishment_status'] == 'MAPPING_APPROVED'
    assert active_families(con) == {'A'}
    assert active_families(con, 'CA') == set()
