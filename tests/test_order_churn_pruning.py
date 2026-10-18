"""Tests for prune_unchanged_order_commands.

A cancel paired with an identical repost is dropped so the unchanged quote
stays resting in the book.
"""

import pytest

from spacesim2.core.commands import (
    CancelOrderCommand,
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
    prune_unchanged_order_commands,
)
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market

from .helpers import get_actor


@pytest.fixture
def food():
    return CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )


@pytest.fixture
def market(food):
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food
    return market


def test_identical_repost_pair_is_dropped(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    order_id = market.place_buy_order(buyer, food, 5, 10)

    commands = [
        CancelOrderCommand(order_id),
        PlaceBuyOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)

    assert pruned == []
    # The standing order and its reservation are untouched.
    assert order_id in market.orders_by_id
    assert buyer.reserved_money == 50


def test_kept_order_timestamp_is_refreshed(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    order_id = market.place_buy_order(buyer, food, 5, 10)
    assert market.orders_by_id[order_id].timestamp == 0

    # On a later turn the kept order is stamped as if reposted now, so it
    # does not gain price-time priority over fresh orders.
    market.current_turn = 7
    commands = [
        CancelOrderCommand(order_id),
        PlaceBuyOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)

    assert pruned == []
    assert market.orders_by_id[order_id].timestamp == 7


def test_changed_price_or_quantity_is_not_dropped(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=200)
    price_changed = market.place_buy_order(buyer, food, 5, 10)
    qty_changed = market.place_buy_order(buyer, food, 3, 7)

    commands = [
        CancelOrderCommand(price_changed),
        CancelOrderCommand(qty_changed),
        PlaceBuyOrderCommand(food, 5, 11),  # new price
        PlaceBuyOrderCommand(food, 4, 7),  # new quantity
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)
    assert pruned == commands


def test_side_is_part_of_the_key(market, food, mock_sim) -> None:
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food, 10)
    sell_id = market.place_sell_order(seller, food, 5, 10)

    # A buy repost at the same commodity, price, and quantity must not pair
    # with a cancelled sell order.
    commands = [
        CancelOrderCommand(sell_id),
        PlaceBuyOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, seller, commands)
    assert pruned == commands


def test_identical_sell_repost_pair_is_dropped(market, food, mock_sim) -> None:
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food, 10)
    sell_id = market.place_sell_order(seller, food, 5, 10)

    commands = [
        CancelOrderCommand(sell_id),
        PlaceSellOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, seller, commands)

    assert pruned == []
    assert sell_id in market.orders_by_id
    assert seller.inventory.get_reserved_quantity(food) == 5


def test_mixed_list_keeps_unmatched_commands_in_order(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=200)
    kept_id = market.place_buy_order(buyer, food, 5, 10)
    repriced_id = market.place_buy_order(buyer, food, 2, 8)

    cancel_repriced = CancelOrderCommand(repriced_id)
    repost_repriced = PlaceBuyOrderCommand(food, 2, 9)
    commands = [
        CancelOrderCommand(kept_id),
        cancel_repriced,
        PlaceBuyOrderCommand(food, 5, 10),
        repost_repriced,
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)
    assert pruned == [cancel_repriced, repost_repriced]


def test_other_actors_orders_are_ignored(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    other = get_actor("Other", mock_sim, initial_money=100)
    other_id = market.place_buy_order(other, food, 5, 10)

    # A brain should never cancel another actor's order, but if such a command
    # appears it must not pair with this actor's reposts.
    commands = [
        CancelOrderCommand(other_id),
        PlaceBuyOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)
    assert pruned == commands


def test_duplicate_keys_pair_one_to_one(market, food, mock_sim) -> None:
    buyer = get_actor("Buyer", mock_sim, initial_money=300)
    id_a = market.place_buy_order(buyer, food, 5, 10)
    id_b = market.place_buy_order(buyer, food, 5, 10)

    # Two identical cancels and one identical repost: one pair is dropped
    # and the other cancel passes through.
    commands = [
        CancelOrderCommand(id_a),
        CancelOrderCommand(id_b),
        PlaceBuyOrderCommand(food, 5, 10),
    ]
    pruned = prune_unchanged_order_commands(market, buyer, commands)
    assert len(pruned) == 1
    assert isinstance(pruned[0], CancelOrderCommand)
