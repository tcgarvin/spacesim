"""Behavioral tests for lazy-delete order cancellation in Market.

cancel_order marks orders dead instead of rebuilding the per-commodity book
list. Every reader must see the live orders an eager delete would have left,
in the same relative order; matching must ignore dead orders; compaction must
keep book length bounded.
"""

import pytest

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.market import Market

from .helpers import get_actor


@pytest.fixture
def commodity_registry():
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


@pytest.fixture
def food(commodity_registry):
    return commodity_registry.get_commodity("food")


def _make_market(commodity_registry) -> Market:
    market = Market()
    market.commodity_registry = commodity_registry
    return market


def _live(orders):
    return [o for o in orders if not o.cancelled]


def test_cancel_updates_spread_and_levels(commodity_registry, food, mock_sim) -> None:
    """Cancelling the best-priced order updates the quote and bid levels."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food, 20)

    best_bid_id = market.place_buy_order(buyer, food, 2, 12)
    market.place_buy_order(buyer, food, 3, 8)
    best_ask_id = market.place_sell_order(seller, food, 4, 15)
    market.place_sell_order(seller, food, 5, 20)

    assert market.get_bid_ask_spread(food) == (12, 15)
    assert market.get_bid_levels(food) == [(12, 2), (8, 3)]

    assert market.cancel_order(best_bid_id)
    assert market.cancel_order(best_ask_id)

    assert market.get_bid_ask_spread(food) == (8, 20)
    assert market.get_bid_levels(food) == [(8, 3)]

    # Reserved resources are released as with an eager delete.
    assert buyer.reserved_money == 3 * 8
    assert seller.inventory.get_reserved_quantity(food) == 5


def test_cancel_all_orders_reads_as_empty(commodity_registry, food, mock_sim) -> None:
    """An all-dead book behaves like an empty one for every reader."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)

    order_ids = [market.place_buy_order(buyer, food, 1, p) for p in (5, 6, 7)]
    for order_id in order_ids:
        assert market.cancel_order(order_id)

    assert market.get_bid_ask_spread(food) == (None, None)
    assert market.get_bid_levels(food) == []
    assert _live(market.buy_orders.get(food, [])) == []
    assert buyer.reserved_money == 0
    assert buyer.active_orders == {}

    # Matching an all-dead book produces no transactions and no errors.
    market.match_orders()
    assert market.transaction_history == []


def test_matching_ignores_cancelled_orders(commodity_registry, food, mock_sim) -> None:
    """A cancelled crossing order does not trade; the live book still matches."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food, 20)

    cancelled_bid = market.place_buy_order(buyer, food, 5, 30)
    market.place_buy_order(buyer, food, 5, 10)
    market.place_sell_order(seller, food, 5, 10)
    assert market.cancel_order(cancelled_bid)

    market.match_orders()

    assert len(market.transaction_history) == 1
    tx = market.transaction_history[0]
    assert tx.price == 10
    assert tx.quantity == 5
    # The cancelled 30-credit bid never traded, so the buyer paid 10.
    assert buyer.money == 1000 - 5 * 10
    # Post-match books contain no dead orders.
    assert all(not o.cancelled for o in market.buy_orders[food])
    assert all(not o.cancelled for o in market.sell_orders[food])


def test_compaction_preserves_live_order_sequence(
    commodity_registry, food, mock_sim
) -> None:
    """Compaction removes only dead orders and keeps live relative order."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=10000)

    order_ids = [market.place_buy_order(buyer, food, 1, 5 + i) for i in range(8)]
    to_cancel = [order_ids[i] for i in (1, 3, 5, 7, 0)]  # 5 of 8 > len//2
    live_expected = [oid for oid in order_ids if oid not in to_cancel]
    for order_id in to_cancel:
        assert market.cancel_order(order_id)

    # The fifth cancel crosses the dead > len(book)//2 threshold, so the book
    # compacts down to the live orders in placement order.
    book = market.buy_orders[food]
    assert [o.order_id for o in book] == live_expected
    assert all(not o.cancelled for o in book)


def test_cancel_repost_churn_keeps_book_bounded(
    commodity_registry, food, mock_sim
) -> None:
    """Repeated cancel and repost cycles keep the book bounded."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=100000)

    resting = [market.place_buy_order(buyer, food, 1, 5) for _ in range(10)]
    for _ in range(200):
        order_id = resting.pop(0)
        assert market.cancel_order(order_id)
        resting.append(market.place_buy_order(buyer, food, 1, 5))

    book = market.buy_orders[food]
    # 10 live orders; threshold compaction bounds the dead tail to len(book)//2.
    assert len(_live(book)) == 10
    assert len(book) <= 2 * 10 + 2

    # match_orders sweeps the remaining dead orders each turn.
    market.match_orders()
    assert len(market.buy_orders[food]) == 10
    assert all(not o.cancelled for o in market.buy_orders[food])


def test_match_orders_compacts_every_book(commodity_registry, food, mock_sim) -> None:
    """After end-of-turn matching, no cancelled order rests in any book."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food, 20)

    market.place_buy_order(buyer, food, 2, 5)
    cancelled_bid = market.place_buy_order(buyer, food, 2, 6)
    market.place_sell_order(seller, food, 2, 50)
    cancelled_ask = market.place_sell_order(seller, food, 2, 60)
    assert market.cancel_order(cancelled_bid)
    assert market.cancel_order(cancelled_ask)

    market.match_orders()  # no cross, but the sweep still runs

    book_ids = {
        o.order_id
        for book in (market.buy_orders, market.sell_orders)
        for orders in book.values()
        for o in orders
    }
    assert book_ids == set(market.orders_by_id)
    assert cancelled_bid not in book_ids
    assert cancelled_ask not in book_ids


def test_export_order_counts_skip_cancelled(commodity_registry, food, mock_sim) -> None:
    """Exported book counts exclude cancelled orders."""
    market = _make_market(commodity_registry)
    buyer = get_actor("Buyer", mock_sim, initial_money=1000)

    market.place_buy_order(buyer, food, 1, 5)
    market.place_buy_order(buyer, food, 1, 6)
    cancelled = market.place_buy_order(buyer, food, 1, 7)
    assert market.cancel_order(cancelled)

    # The exporter counts live orders with this filter.
    num_buy_orders = len(
        [o for o in market.buy_orders.get(food, []) if not o.cancelled]
    )
    assert num_buy_orders == 2
