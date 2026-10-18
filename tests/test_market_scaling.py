"""Tests for Market scaling: bounded histories, incremental quotes, cheap
order ids, and cached bid levels.

Caches must stay exact under place, cancel, and fill churn; histories must
stay bounded while still serving the windows consumers read; order ids must
stay unique.
"""

import random

import pytest

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.market import (
    HISTORY_KEEP,
    HISTORY_TRIM_THRESHOLD,
    ORDER_EVENTS_PER_ACTOR,
    Market,
)

from .helpers import get_actor


@pytest.fixture
def commodity_registry() -> CommodityRegistry:
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


@pytest.fixture
def food(commodity_registry):
    return commodity_registry.get_commodity("food")


def _brute_force_quote(market: Market, commodity) -> tuple:
    """Reference best bid and ask from a full book scan."""
    bids = [o.price for o in market.buy_orders.get(commodity, []) if not o.cancelled]
    asks = [o.price for o in market.sell_orders.get(commodity, []) if not o.cancelled]
    return (max(bids) if bids else None, min(asks) if asks else None)


def _brute_force_bid_levels(market: Market, commodity) -> list:
    levels = [
        (o.price, o.quantity)
        for o in market.buy_orders.get(commodity, [])
        if not o.cancelled
    ]
    levels.sort(key=lambda level: -level[0])
    return levels


class TestQuoteCacheExactness:
    """get_bid_ask_spread always equals a brute-force scan of the books."""

    def test_place_updates_best_quote(self, commodity_registry, food, mock_sim):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 1_000)

        assert market.get_bid_ask_spread(food) == (None, None)
        market.place_buy_order(buyer, food, 1, 5)
        assert market.get_bid_ask_spread(food) == (5, None)
        market.place_buy_order(buyer, food, 1, 9)
        assert market.get_bid_ask_spread(food) == (9, None)
        market.place_buy_order(buyer, food, 1, 7)  # non-improving bid
        assert market.get_bid_ask_spread(food) == (9, None)
        market.place_sell_order(seller, food, 1, 20)
        assert market.get_bid_ask_spread(food) == (9, 20)
        market.place_sell_order(seller, food, 1, 15)
        assert market.get_bid_ask_spread(food) == (9, 15)
        market.place_sell_order(seller, food, 1, 18)  # non-improving ask
        assert market.get_bid_ask_spread(food) == (9, 15)

    def test_cancel_of_best_and_non_best_orders(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000)

        low = market.place_buy_order(buyer, food, 1, 5)
        best = market.place_buy_order(buyer, food, 1, 9)
        market.get_bid_ask_spread(food)  # populate the cache

        market.cancel_order(low)  # non-best cancel: cache survives, stays exact
        assert market.get_bid_ask_spread(food) == _brute_force_quote(market, food)
        market.cancel_order(best)  # best cancel: forces a rescan
        assert market.get_bid_ask_spread(food) == _brute_force_quote(market, food)
        assert market.get_bid_ask_spread(food) == (None, None)

    def test_randomized_churn_matches_brute_force(
        self, commodity_registry, food, mock_sim
    ):
        """Under random place, cancel, and match churn the cached quote stays exact."""
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=1_000_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 1_000_000)
        rng = random.Random(42)
        open_orders: list[str] = []

        for turn in range(1, 60):
            market.set_current_turn(turn)
            for _ in range(rng.randint(1, 6)):
                action = rng.random()
                if action < 0.4:
                    oid = market.place_buy_order(
                        buyer, food, rng.randint(1, 3), rng.randint(5, 15)
                    )
                elif action < 0.8:
                    oid = market.place_sell_order(
                        seller, food, rng.randint(1, 3), rng.randint(5, 15)
                    )
                else:
                    oid = ""
                    if open_orders:
                        market.cancel_order(
                            open_orders.pop(rng.randrange(len(open_orders)))
                        )
                if oid:
                    open_orders.append(oid)
                assert market.get_bid_ask_spread(food) == _brute_force_quote(
                    market, food
                )
            market.match_orders()
            open_orders = [oid for oid in open_orders if oid in market.orders_by_id]
            assert market.get_bid_ask_spread(food) == _brute_force_quote(market, food)
            assert market.get_bid_levels(food) == _brute_force_bid_levels(market, food)


class TestBidLevelsCache:
    def test_levels_track_mutations(self, commodity_registry, food, mock_sim):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000)

        market.place_buy_order(buyer, food, 2, 7)
        first = market.get_bid_levels(food)
        assert first == [(7, 2)]

        oid = market.place_buy_order(buyer, food, 3, 9)
        assert market.get_bid_levels(food) == [(9, 3), (7, 2)]
        market.cancel_order(oid)
        assert market.get_bid_levels(food) == [(7, 2)]

    def test_returned_list_is_a_copy(self, commodity_registry, food, mock_sim):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000)
        market.place_buy_order(buyer, food, 2, 7)

        levels = market.get_bid_levels(food)
        levels.append((1, 1))  # caller mutation must not poison the cache
        assert market.get_bid_levels(food) == [(7, 2)]


class TestBoundedHistories:
    def _trade_once(self, market, buyer, seller, food, price: int) -> None:
        market.place_buy_order(buyer, food, 1, price)
        market.place_sell_order(seller, food, 1, price)
        market.match_orders()

    def test_price_history_stays_bounded_and_serves_windows(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 1_000_000)

        turns = HISTORY_TRIM_THRESHOLD + 100
        for turn in range(1, turns + 1):
            market.set_current_turn(turn)
            self._trade_once(market, buyer, seller, food, 10)

        assert len(market.price_history[food]) <= HISTORY_TRIM_THRESHOLD
        assert len(market.price_history[food]) >= HISTORY_KEEP
        assert len(market.volume_history[food]) == len(market.price_history[food])
        # The windows consumers read still work after trimming.
        assert market.get_30_day_average_price(food) == pytest.approx(10.0)
        assert market.get_30_day_average_volume(food) == pytest.approx(1.0)
        assert market.volume_history[food][-10:] == [1] * 10
        assert market.has_price_signal(food)
        assert market.get_avg_price(food) == 10

    def test_has_history_counts_lifetime_active_days(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 1_000_000)

        for turn in range(1, 5):
            market.set_current_turn(turn)
            self._trade_once(market, buyer, seller, food, 10)
            assert not market.has_history(food)  # 4 active days: not enough

        market.set_current_turn(5)
        self._trade_once(market, buyer, seller, food, 10)
        assert market.has_history(food)  # 5th active day crosses the threshold

        # Quiet turns append zero-volume entries but never lose the signal.
        for turn in range(6, 20):
            market.set_current_turn(turn)
            market.match_orders()
        assert market.has_history(food)

    def test_order_events_bounded_but_current_turn_complete(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000_000)

        for turn in range(1, 300):
            market.set_current_turn(turn)
            oid = market.place_buy_order(buyer, food, 1, 5)
            market.cancel_order(oid)

        events = market.order_events_by_actor[buyer.name]
        assert len(events) <= ORDER_EVENTS_PER_ACTOR

        # The data logger reads the current turn's events.
        current = market.get_actor_order_events(buyer, since_turn=299)
        assert [e.event_type for e in current] == ["created", "cancelled"]

    def test_transaction_histories_bounded(self, commodity_registry, food, mock_sim):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=100_000_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 10_000_000)

        for turn in range(1, 1300):
            market.set_current_turn(turn)
            self._trade_once(market, buyer, seller, food, 1)

        # Trim runs at the start of match_orders, so at most keep + 1 remain.
        assert len(market.transaction_history) <= 1001
        assert len(market.actor_transaction_history[buyer.name]) <= 101
        # Most recent transactions retained, oldest dropped.
        assert market.transaction_history[-1].turn == 1299


class TestOrderIds:
    def test_ids_unique_and_nonempty(self, commodity_registry, food, mock_sim):
        market_a = Market()
        market_b = Market()
        market_a.commodity_registry = commodity_registry
        market_b.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=10_000_000)

        ids = set()
        for market in (market_a, market_b):
            for _ in range(500):
                oid = market.place_buy_order(buyer, food, 1, 5)
                assert oid  # non-empty string
                assert isinstance(oid, str)
                assert oid not in ids  # unique across markets too
                ids.add(oid)


class TestMatchingBehaviorPreserved:
    def test_price_time_priority_and_fill_results(
        self, commodity_registry, food, mock_sim
    ):
        """Matching takes the best price first, then the oldest timestamp."""
        market = Market()
        market.commodity_registry = commodity_registry
        buyer_a = get_actor("BuyerA", mock_sim, initial_money=1_000)
        buyer_b = get_actor("BuyerB", mock_sim, initial_money=1_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 100)

        market.set_current_turn(1)
        market.place_buy_order(buyer_a, food, 5, 10)
        market.set_current_turn(2)
        market.place_buy_order(buyer_b, food, 5, 10)  # same price, younger
        market.place_sell_order(seller, food, 7, 8)
        market.match_orders()

        # The older BuyerA fills fully first; BuyerB gets the remaining 2.
        assert buyer_a.inventory.get_quantity(food) == 5
        assert buyer_b.inventory.get_quantity(food) == 2
        assert seller.money == 50 + 7 * 8
        # Partial buy order still resting with reduced quantity.
        remaining = market.buy_orders[food]
        assert len(remaining) == 1
        assert remaining[0].actor is buyer_b
        assert remaining[0].quantity == 3
        assert market.get_bid_ask_spread(food) == (10, None)

    def test_empty_book_commodities_still_roll_history_and_decay_pressure(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=1_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 100)

        market.set_current_turn(1)
        market.place_buy_order(buyer, food, 1, 10)
        market.place_sell_order(seller, food, 1, 10)
        market.match_orders()
        assert market.volume_history[food] == [1]

        # No orders at all: the fast path must still append a zero-volume
        # entry, carry the last price, and decay scarcity pressure.
        market.scarcity_pressure[food] = 1.0
        market.set_current_turn(2)
        market.match_orders()
        assert market.volume_history[food] == [1, 0]
        assert market.price_history[food] == [10, 10]
        assert market.scarcity_pressure_for(food) == pytest.approx(0.9)

    def test_uncrossed_books_produce_no_trades_but_track_pressure(
        self, commodity_registry, food, mock_sim
    ):
        market = Market()
        market.commodity_registry = commodity_registry
        buyer = get_actor("Buyer", mock_sim, initial_money=1_000)
        seller = get_actor("Seller", mock_sim)
        seller.inventory.add_commodity(food, 100)

        market.set_current_turn(1)
        market.place_buy_order(buyer, food, 2, 5)
        market.place_sell_order(seller, food, 2, 9)  # ask above bid: no cross
        market.match_orders()

        assert len(market.transaction_history) == 0
        assert len(market.buy_orders[food]) == 1
        assert len(market.sell_orders[food]) == 1
        # Unmet demand grows scarcity pressure.
        assert market.scarcity_pressure_for(food) == pytest.approx(0.5)
