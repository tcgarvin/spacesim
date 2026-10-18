from typing import Tuple

import pytest

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market

from .helpers import get_actor


@pytest.fixture
def commodity_registry():
    """Registry loaded from data/commodities.yaml."""
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


@pytest.fixture
def nova_fuel(commodity_registry):
    """The nova_fuel commodity."""
    return commodity_registry.get_commodity("nova_fuel")


@pytest.fixture
def market_with_actors(
    commodity_registry, nova_fuel, mock_sim
) -> Tuple[Market, Actor, Actor]:
    """Market plus a buyer and a seller holding 20 nova_fuel."""
    market = Market()
    market.commodity_registry = commodity_registry

    buyer = get_actor("Buyer", mock_sim, initial_money=1000)
    seller = get_actor("Seller", mock_sim, initial_money=500)

    seller.inventory.add_commodity(nova_fuel, 20)

    return market, buyer, seller


def count_total_commodities(
    market: Market, commodity: CommodityDefinition, *actors: Actor
) -> int:
    """Total of a commodity across the given actors."""
    total = 0
    for actor in actors:
        total += actor.inventory.get_quantity(commodity)
    return total


def count_total_money(market: Market, *actors: Actor) -> int:
    """Total available plus reserved money across the given actors."""
    total = 0
    for actor in actors:
        total += actor.money + actor.reserved_money
    return total


def test_conservation_in_normal_trade(market_with_actors, nova_fuel):
    """Commodity and money totals are unchanged by placing and matching orders."""
    market, buyer, seller = market_with_actors

    initial_commodity_count = count_total_commodities(market, nova_fuel, buyer, seller)
    initial_money = count_total_money(market, buyer, seller)

    market.place_buy_order(buyer, nova_fuel, 5, 10)
    market.place_sell_order(seller, nova_fuel, 5, 10)

    assert (
        count_total_commodities(market, nova_fuel, buyer, seller)
        == initial_commodity_count
    )
    assert count_total_money(market, buyer, seller) == initial_money

    market.match_orders()

    assert (
        count_total_commodities(market, nova_fuel, buyer, seller)
        == initial_commodity_count
    )
    assert count_total_money(market, buyer, seller) == initial_money


def test_conservation_with_partial_matches(market_with_actors, nova_fuel):
    """Commodity and money totals are unchanged by a partial match."""
    market, buyer, seller = market_with_actors

    initial_commodity_count = count_total_commodities(market, nova_fuel, buyer, seller)
    initial_money = count_total_money(market, buyer, seller)

    market.place_buy_order(buyer, nova_fuel, 8, 10)
    market.place_sell_order(seller, nova_fuel, 5, 10)

    market.match_orders()

    assert (
        count_total_commodities(market, nova_fuel, buyer, seller)
        == initial_commodity_count
    )
    assert count_total_money(market, buyer, seller) == initial_money


def test_conservation_after_order_cancellation(market_with_actors, nova_fuel):
    """Commodity and money totals are unchanged by cancelling orders."""
    market, buyer, seller = market_with_actors

    initial_commodity_count = count_total_commodities(market, nova_fuel, buyer, seller)
    initial_money = count_total_money(market, buyer, seller)

    buy_order_id = market.place_buy_order(buyer, nova_fuel, 5, 10)
    sell_order_id = market.place_sell_order(seller, nova_fuel, 5, 10)

    market.cancel_order(buy_order_id)
    market.cancel_order(sell_order_id)

    assert (
        count_total_commodities(market, nova_fuel, buyer, seller)
        == initial_commodity_count
    )
    assert count_total_money(market, buyer, seller) == initial_money


def test_conservation_when_buy_order_quantity_is_clamped(market_with_actors, nova_fuel):
    """An unaffordable buy order reserves only the clamped cost.

    Reserving the original cost drove money negative and orphaned the
    difference in reserved_money.
    """
    market, buyer, seller = market_with_actors

    buyer.money = 100
    order_id = market.place_buy_order(buyer, nova_fuel, 1000, 10)
    assert order_id

    order = market.orders_by_id[order_id]
    assert order.quantity == 10  # clamped to what 100 money affords
    assert buyer.money == 0
    assert buyer.reserved_money == 100

    market.cancel_order(order_id)
    assert buyer.money == 100
    assert buyer.reserved_money == 0
