"""Tests for the float stdev rewrite and the per-turn history-read memo.

Five trade-history reads are memoized per turn: get_avg_price,
has_price_signal, get_30_day_average_price, get_30_day_average_volume, and
get_30_day_standard_deviation. Both set_current_turn and match_orders must
invalidate the memo so callers always see the uncached values.
"""

import math
import statistics

import pytest

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market

from .helpers import get_actor


@pytest.fixture
def food_commodity() -> CommodityDefinition:
    return CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )


@pytest.fixture
def market(food_commodity: CommodityDefinition) -> Market:
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food_commodity
    return market


def test_stdev_matches_statistics_stdev(
    market: Market, food_commodity: CommodityDefinition
) -> None:
    """Float stdev agrees with statistics.stdev on int prices."""
    prices = [10, 12, 15, 11, 13, 20, 9, 14]
    market.price_history[food_commodity] = list(prices)

    assert math.isclose(
        market.get_30_day_standard_deviation(food_commodity),
        statistics.stdev(prices),
        rel_tol=1e-12,
    )


def test_stdev_uses_last_30_prices(
    market: Market, food_commodity: CommodityDefinition
) -> None:
    """With more than 30 entries, only the last 30 feed the calculation."""
    prices = list(range(1, 51))
    market.price_history[food_commodity] = list(prices)

    assert math.isclose(
        market.get_30_day_standard_deviation(food_commodity),
        statistics.stdev(prices[-30:]),
        rel_tol=1e-12,
    )


def test_stdev_short_history_default(
    market: Market, food_commodity: CommodityDefinition
) -> None:
    """Fewer than 2 prices falls back to max(1.0, 10% of average price)."""
    assert market.get_30_day_standard_deviation(food_commodity) == 1.0

    market.price_history[food_commodity] = [100]
    market.set_current_turn(1)  # clear the memo from the read above
    assert market.get_30_day_standard_deviation(food_commodity) == pytest.approx(10.0)


def test_history_reads_refresh_after_match_orders(
    market: Market, food_commodity: CommodityDefinition, mock_sim
) -> None:
    """A read after match_orders reflects the trade that just cleared."""
    # Prime the memo with the no-history defaults.
    assert market.get_avg_price(food_commodity) == 10
    assert market.has_price_signal(food_commodity) is False
    assert market.get_30_day_average_price(food_commodity) == 10.0
    assert market.get_30_day_average_volume(food_commodity) == 1.0

    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food_commodity, 10)

    market.place_buy_order(buyer, food_commodity, 5, 8)
    market.place_sell_order(seller, food_commodity, 5, 8)
    market.match_orders()

    # Post-match readers such as export and logging see price 8, volume 5.
    assert market.get_avg_price(food_commodity) == 8
    assert market.has_price_signal(food_commodity) is True
    assert market.get_30_day_average_price(food_commodity) == 8.0
    assert market.get_30_day_average_volume(food_commodity) == 5.0
    # Only one price point yet, so stdev falls back to max(1.0, 8 * 0.1).
    assert market.get_30_day_standard_deviation(food_commodity) == 1.0


def test_history_reads_refresh_on_new_turn(
    market: Market, food_commodity: CommodityDefinition
) -> None:
    """set_current_turn clears the memo so a new turn recomputes from state."""
    market.price_history[food_commodity] = [10, 20]
    market.volume_history[food_commodity] = [4]

    assert market.get_30_day_average_price(food_commodity) == 15.0
    assert market.get_30_day_average_volume(food_commodity) == 4.0

    # Change state as match_orders would, then tick the turn.
    market.price_history[food_commodity].append(50)
    market.volume_history[food_commodity].append(8)
    market.set_current_turn(2)

    assert market.get_30_day_average_price(food_commodity) == pytest.approx(80 / 3)
    assert market.get_30_day_average_volume(food_commodity) == 6.0
