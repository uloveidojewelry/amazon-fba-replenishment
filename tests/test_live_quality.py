"""Acceptance guards for privacy-stripped real inventory and inbound shapes."""
from copy import deepcopy

import pytest

from fba_replenishment.spapi import SPAPIError
from fba_replenishment.sync import collect_inbound, collect_stock, collect_stock_details


def stock_row(fnsku='FNSKU-A'):
    return {
        'sellerSku': 'SKU-A', 'fnSku': fnsku, 'condition': 'NewItem',
        'inventoryDetails': {
            'fulfillableQuantity': 10,
            'reservedQuantity': {'totalReservedQuantity': 3},
            'unfulfillableQuantity': {'totalUnfulfillableQuantity': 2},
            'inboundWorkingQuantity': 4,
            'inboundShippedQuantity': 5,
            'inboundReceivingQuantity': 6,
        },
    }


class InventoryAPI:
    def __init__(self, rows):
        self.rows = rows

    def inventory_summaries(self):
        return self.rows


def shipment_item(fnsku='FNSKU-A', shipped=10, received=3):
    return {'ShipmentId': 'SHIPMENT-A', 'SellerSKU': 'SKU-A',
            'FulfillmentNetworkSKU': fnsku,
            'QuantityShipped': shipped, 'QuantityReceived': received}


class InboundAPI:
    def __init__(self, items, status='SHIPPED'):
        self.items, self.status = items, status

    def inbound_shipments(self):
        return [{'ShipmentId': 'SHIPMENT-A', 'ShipmentStatus': self.status}]

    def shipment_items(self, shipment_id):
        assert shipment_id == 'SHIPMENT-A'
        return self.items


def test_stock_details_aggregate_distinct_fnskus_without_double_deduction():
    api = InventoryAPI([stock_row(), stock_row('FNSKU-B')])
    assert collect_stock_details(api) == {'SKU-A': {
        'fulfillable': 20, 'reserved': 6, 'unfulfillable': 4,
        'inbound_working': 8, 'inbound_shipped': 10, 'inbound_receiving': 12,
    }}
    # Existing planner gets stock quantities only, not summed inbound deductions.
    assert collect_stock(api) == {'SKU-A': [20, 6, 4]}


@pytest.mark.parametrize('field', ['inboundWorkingQuantity', 'inboundShippedQuantity',
                                   'inboundReceivingQuantity'])
def test_strict_inventory_rejects_unknown_inbound_quantity(field):
    row = stock_row()
    del row['inventoryDetails'][field]
    with pytest.raises(ValueError, match='Missing'):
        collect_stock_details(InventoryAPI([row]))
    assert collect_stock(InventoryAPI([row])) == {'SKU-A': [10, 3, 2]}


def test_non_strict_inventory_propagates_unknown_across_fnskus():
    second = stock_row('FNSKU-B')
    del second['inventoryDetails']['inboundShippedQuantity']
    details = collect_stock_details(InventoryAPI([stock_row(), second]), strict=False)
    assert details['SKU-A']['inbound_shipped'] is None
    assert details['SKU-A']['inbound_working'] == 8


def test_duplicate_inventory_identity_is_not_summed():
    with pytest.raises(SPAPIError, match='Duplicate inventory'):
        collect_stock_details(InventoryAPI([stock_row(), stock_row()]))


def test_strict_inbound_deduplicates_same_item_and_preserves_explicit_eta():
    row = shipment_item()
    result, _ = collect_inbound(InboundAPI([row, deepcopy(row)]),
                                {('SKU-A', 'SHIPMENT-A'): '2026-10-20'}, strict=True)
    assert result == [('SKU-A', 'SHIPMENT-A', 7, '2026-10-20', 'SHIPPED')]


def test_strict_inbound_aggregates_distinct_fnskus_and_keeps_unknown_eta():
    result, _ = collect_inbound(InboundAPI([shipment_item(), shipment_item('FNSKU-B')]),
                                {}, strict=True)
    assert result == [('SKU-A', 'SHIPMENT-A', 14, '', 'SHIPPED')]


def test_strict_inbound_conflicting_duplicate_fails_closed():
    with pytest.raises(SPAPIError) as error:
        collect_inbound(InboundAPI([shipment_item(), shipment_item(shipped=11)]), {}, strict=True)
    assert error.value.code == 'INBOUND_DUPLICATE_CONFLICT'


def test_strict_inbound_rejects_over_received_instead_of_silently_using_zero():
    with pytest.raises(SPAPIError) as error:
        collect_inbound(InboundAPI([shipment_item(received=11)]), {}, strict=True)
    assert error.value.code == 'INBOUND_QUANTITY_CONFLICT'
    assert collect_inbound(InboundAPI([shipment_item(received=11)]), {})[0][0][2] == 0


def test_strict_active_inbound_requires_usable_items():
    with pytest.raises(SPAPIError) as error:
        collect_inbound(InboundAPI([]), {}, strict=True)
    assert error.value.code == 'INBOUND_ITEMS_INCOMPLETE'
    assert collect_inbound(InboundAPI([], status='CLOSED'), {}, strict=True)[0] == []


def test_strict_inbound_missing_received_quantity_is_unknown_not_zero():
    row = shipment_item()
    del row['QuantityReceived']
    with pytest.raises(ValueError, match='Missing'):
        collect_inbound(InboundAPI([row]), {}, strict=True)


def test_strict_inbound_does_not_require_fnsku_but_still_deduplicates():
    row = shipment_item()
    del row['FulfillmentNetworkSKU']
    result, _ = collect_inbound(InboundAPI([row, deepcopy(row)]), {}, strict=True)
    assert result[0][2] == 7


def test_strict_inbound_rejects_cross_shipment_items():
    row = shipment_item()
    row['ShipmentId'] = 'OTHER-SHIPMENT'
    with pytest.raises(SPAPIError, match='different shipment'):
        collect_inbound(InboundAPI([row]), {}, strict=True)
