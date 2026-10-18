"""Tests for the shared dealer algorithms in core/brains/dealer.py."""

import random

import pytest

from spacesim2.core.brains import dealer
from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.market import TRANSACTIONS_KEEP_PER_ACTOR, Market

from .helpers import get_actor


@pytest.fixture
def commodity_registry():
    """Registry loaded from data/commodities.yaml."""
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


@pytest.fixture
def food_commodity(commodity_registry):
    """The food commodity."""
    return commodity_registry.get_commodity("food")


# ---- ingest_fills ------------------------------------------------------------


def _traded_market(commodity_registry, food_commodity, mock_sim):
    """A market where a buyer and a seller have crossed twice."""
    market = Market()
    market.commodity_registry = commodity_registry
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)
    seller = get_actor("Seller", mock_sim, initial_money=0)
    seller.inventory.add_commodity(food_commodity, 20)

    market.place_sell_order(seller, food_commodity, 5, 4)
    market.place_buy_order(buyer, food_commodity, 5, 6)
    market.match_orders()

    market.place_sell_order(seller, food_commodity, 3, 7)
    market.place_buy_order(buyer, food_commodity, 3, 9)
    market.match_orders()

    return market, buyer, seller


def test_ingest_fills_groups_by_commodity_and_side(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """Fills come back grouped by commodity name and split buy versus sell."""
    market, buyer, seller = _traded_market(commodity_registry, food_commodity, mock_sim)

    cursor, fills = dealer.ingest_fills(buyer, market, 0)

    assert cursor == market.actor_transaction_history[buyer.name][-1].transaction_id
    bucket = fills[food_commodity.name]
    assert [lot.quantity for lot in bucket.buys] == [5, 3]
    assert bucket.sell_prices == []
    assert bucket.buy_prices == [
        market.actor_transaction_history[buyer.name][0].price,
        market.actor_transaction_history[buyer.name][1].price,
    ]

    _, seller_fills = dealer.ingest_fills(seller, market, 0)
    assert seller_fills[food_commodity.name].buys == []
    assert len(seller_fills[food_commodity.name].sells) == 2


def test_ingest_fills_advances_cursor(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """A second call from the returned cursor sees nothing new."""
    market, buyer, _ = _traded_market(commodity_registry, food_commodity, mock_sim)

    cursor, _ = dealer.ingest_fills(buyer, market, 0)
    next_cursor, fills = dealer.ingest_fills(buyer, market, cursor)

    assert next_cursor == cursor
    assert fills == {}


def test_ingest_fills_survives_history_trimming(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """Fills pushed out of the retained window are neither skipped nor replayed.

    The market keeps only the last TRANSACTIONS_KEEP_PER_ACTOR fills per actor,
    so an index-based cursor would go stale. Trade well past that bound between
    two ingests and check that the second call sees exactly the fills that are
    still retained and no earlier one a second time.
    """
    market = Market()
    market.commodity_registry = commodity_registry
    buyer = get_actor("Buyer", mock_sim, initial_money=1_000_000)
    seller = get_actor("Seller", mock_sim, initial_money=0)
    seller.inventory.add_commodity(food_commodity, 10_000)

    def _trade(price):
        market.place_sell_order(seller, food_commodity, 1, price)
        market.place_buy_order(buyer, food_commodity, 1, price)
        market.match_orders()

    _trade(5)
    cursor, first = dealer.ingest_fills(buyer, market, 0)
    assert len(first[food_commodity.name].buys) == 1

    extra = TRANSACTIONS_KEEP_PER_ACTOR + 20
    for _ in range(extra):
        _trade(7)

    history = market.actor_transaction_history[buyer.name]
    # Trimming really happened: the price-5 fill is gone from the window.
    assert len(history) <= TRANSACTIONS_KEEP_PER_ACTOR + 1 < extra
    assert all(txn.price == 7 for txn in history)

    next_cursor, second = dealer.ingest_fills(buyer, market, cursor)

    buys = second[food_commodity.name].buys
    # Every retained fill after the cursor, and not one from before it.
    assert len(buys) == len(history)
    assert all(lot.price == 7 for lot in buys)
    assert next_cursor == history[-1].transaction_id

    # A third call sees nothing: no replay of the retained window.
    third_cursor, third = dealer.ingest_fills(buyer, market, next_cursor)
    assert third == {}
    assert third_cursor == next_cursor


def test_cost_basis_stays_correct_across_a_trim(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """A dealer's running basis is not corrupted by a replayed window.

    Ingesting the same fills twice would double the pooled units and leave the
    operator quoting off a basis it never paid.
    """
    market = Market()
    market.commodity_registry = commodity_registry
    buyer = get_actor("Buyer", mock_sim, initial_money=1_000_000)
    seller = get_actor("Seller", mock_sim, initial_money=0)
    seller.inventory.add_commodity(food_commodity, 10_000)

    units, total_cost, cursor = 0, 0.0, 0
    bought = TRANSACTIONS_KEEP_PER_ACTOR + 50
    for i in range(bought):
        market.place_sell_order(seller, food_commodity, 1, 8)
        market.place_buy_order(buyer, food_commodity, 1, 8)
        market.match_orders()
        if i % 10 == 0:
            cursor, fills = dealer.ingest_fills(buyer, market, cursor)
            bucket = fills.get(food_commodity.name, dealer.CommodityFills())
            units, total_cost, _ = dealer.cost_basis(
                units, total_cost, bucket.buys, bucket.sells
            )

    cursor, fills = dealer.ingest_fills(buyer, market, cursor)
    bucket = fills.get(food_commodity.name, dealer.CommodityFills())
    units, total_cost, per_unit = dealer.cost_basis(
        units, total_cost, bucket.buys, bucket.sells
    )

    assert units == bought
    assert per_unit == 8.0


def test_commodity_fills_truthiness() -> None:
    """An empty window is falsy; any fill makes it truthy."""
    assert not dealer.CommodityFills()
    assert dealer.CommodityFills(buys=[dealer.FillLot(1, 1)])
    assert dealer.CommodityFills(sells=[dealer.FillLot(1, 1)])


# ---- skew_midpoint -----------------------------------------------------------


def test_skew_midpoint_on_target_is_neutral() -> None:
    """Holding exactly the target leaves the midpoint alone."""
    assert dealer.skew_midpoint(100, 25, 25, 0.5) == 100


def test_skew_midpoint_directions() -> None:
    """Over-inventoried quotes lower, under-inventoried quotes higher."""
    assert dealer.skew_midpoint(100, 40, 25, 0.5) < 100
    assert dealer.skew_midpoint(100, 10, 25, 0.5) > 100


def test_skew_midpoint_clamps_to_cap() -> None:
    """Extreme inventory ratios are clamped to ±cap."""
    assert dealer.skew_midpoint(100, 10_000, 25, 0.5) == 50
    assert dealer.skew_midpoint(100, 0, 1, 0.5) == 150
    assert dealer.skew_midpoint(100, 10_000, 25, 0.1) == 90


def test_skew_midpoint_respects_min_price() -> None:
    """The floor wins over a deep downward skew."""
    assert dealer.skew_midpoint(2, 10_000, 25, 0.5, min_price=2) == 2


def test_skew_midpoint_ignores_non_positive_target() -> None:
    """A zero target disables the skew rather than dividing by zero."""
    assert dealer.skew_midpoint(37, 500, 0, 0.5) == 37


# ---- flow_stock_target -------------------------------------------------------


def test_flow_stock_target_without_history(commodity_registry, food_commodity) -> None:
    """A market with no history gets the neutral target."""
    market = Market()
    market.commodity_registry = commodity_registry

    assert dealer.flow_stock_target(market, food_commodity) == 25
    assert dealer.flow_stock_target(market, food_commodity, neutral=4) == 4


def test_flow_stock_target_floor(commodity_registry, food_commodity) -> None:
    """The floor raises a small target, e.g. an operator's one-tank minimum."""
    market = Market()
    market.commodity_registry = commodity_registry

    assert dealer.flow_stock_target(market, food_commodity, neutral=4, floor=60) == 60
    assert dealer.flow_stock_target(market, food_commodity, neutral=90, floor=60) == 90


def test_flow_stock_target_from_flow(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """With history the target is days of average volume plus one."""
    market, buyer, seller = _traded_market(commodity_registry, food_commodity, mock_sim)
    # has_history needs five turns with volume.
    for turn in range(5):
        market.set_current_turn(turn)
        seller.inventory.add_commodity(food_commodity, 4)
        market.place_sell_order(seller, food_commodity, 4, 5)
        market.place_buy_order(buyer, food_commodity, 4, 5)
        market.match_orders()
    assert market.has_history(food_commodity)

    target = dealer.flow_stock_target(market, food_commodity, days=10)
    average_volume = max(1, int(market.get_30_day_average_volume(food_commodity)))
    assert target == average_volume * 10 + 1


# ---- draw_spread -------------------------------------------------------------


def test_draw_spread_within_range() -> None:
    """Every draw lands inside the configured spread band."""
    rng = random.Random(1234)
    draws = [dealer.draw_spread(rng) for _ in range(200)]

    assert all(
        dealer.MIN_SPREAD_PERCENTAGE <= d <= dealer.MAX_SPREAD_PERCENTAGE for d in draws
    )
    assert len(set(draws)) > 1


# ---- ladder helpers ----------------------------------------------------------


def test_front_loaded_weights() -> None:
    """Weights descend from the touch."""
    assert dealer.front_loaded_weights(5) == [5, 4, 3, 2, 1]


def test_ladder_prices_both_sides() -> None:
    """Asks walk up and bids walk down from the midpoint, honoring the floor."""
    assert dealer.ladder_prices(100, 10, 3, 2, 2, ascending=True) == [110, 112, 114]
    assert dealer.ladder_prices(100, 10, 3, 2, 1, ascending=False) == [90, 88, 86]
    assert dealer.ladder_prices(5, 4, 3, 2, 1, ascending=False) == [1, 1, 1]


# ---- cost_basis --------------------------------------------------------------


def test_cost_basis_is_volume_weighted() -> None:
    """Buys average by volume, not by fill count."""
    units, total, per_unit = dealer.cost_basis(
        0, 0.0, [dealer.FillLot(10, 10), dealer.FillLot(30, 20)], []
    )

    assert units == 40
    assert total == pytest.approx(700.0)
    assert per_unit == pytest.approx(17.5)


def test_cost_basis_sell_reduces_pro_rata() -> None:
    """Selling retires units at the pool average, leaving the basis unchanged."""
    units, total, per_unit = dealer.cost_basis(
        0,
        0.0,
        [dealer.FillLot(10, 10), dealer.FillLot(30, 20)],
        [dealer.FillLot(20, 99)],
    )

    assert units == 20
    assert total == pytest.approx(350.0)
    assert per_unit == pytest.approx(17.5)


def test_cost_basis_rolls_forward() -> None:
    """A prior pool is carried into the next window."""
    units, total, per_unit = dealer.cost_basis(20, 350.0, [dealer.FillLot(20, 5)], [])

    assert units == 40
    assert total == pytest.approx(450.0)
    assert per_unit == pytest.approx(11.25)


def test_cost_basis_empty_pool() -> None:
    """Selling out, or selling more than held, empties the pool cleanly."""
    assert dealer.cost_basis(0, 0.0, [], []) == (0, 0.0, 0.0)
    assert dealer.cost_basis(
        0, 0.0, [dealer.FillLot(5, 10)], [dealer.FillLot(50, 1)]
    ) == (0, 0.0, 0.0)
