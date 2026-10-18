import pytest

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.market import Market

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


def test_market_initialization() -> None:
    """A new market has empty books and no history."""
    market = Market()
    assert len(market.buy_orders) == 0
    assert len(market.sell_orders) == 0
    assert len(market.transaction_history) == 0


def test_place_buy_order(commodity_registry, food_commodity, mock_sim) -> None:
    """place_buy_order stores a buy order with the given terms."""
    market = Market()
    market.commodity_registry = commodity_registry
    actor = get_actor("Buyer", mock_sim, initial_money=100)

    market.place_buy_order(actor, food_commodity, 10, 5)

    assert len(market.buy_orders[food_commodity]) == 1
    order = market.buy_orders[food_commodity][0]
    assert order.actor == actor
    assert order.commodity_type == food_commodity
    assert order.quantity == 10
    assert order.price == 5
    assert order.is_buy is True


def test_place_sell_order(commodity_registry, food_commodity, mock_sim) -> None:
    """place_sell_order stores a sell order with the given terms."""
    market = Market()
    market.commodity_registry = commodity_registry
    actor = get_actor("Seller", mock_sim)

    actor.inventory.add_commodity(food_commodity, 10)

    market.place_sell_order(actor, food_commodity, 10, 5)

    assert len(market.sell_orders[food_commodity]) == 1
    order = market.sell_orders[food_commodity][0]
    assert order.actor == actor
    assert order.commodity_type == food_commodity
    assert order.quantity == 10
    assert order.price == 5
    assert order.is_buy is False


def test_order_matching(commodity_registry, food_commodity, mock_sim) -> None:
    """A crossing buy and sell trade at the ask and settle money and goods."""
    market = Market()
    market.commodity_registry = commodity_registry

    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    seller = get_actor("Seller", mock_sim)

    seller.inventory.add_commodity(food_commodity, 10)

    market.place_buy_order(buyer, food_commodity, 5, 10)
    market.place_sell_order(seller, food_commodity, 5, 8)

    market.match_orders()

    assert len(market.transaction_history) == 1
    tx = market.transaction_history[0]
    assert tx.buyer == buyer
    assert tx.seller == seller
    assert tx.commodity_type == food_commodity
    assert tx.quantity == 5
    assert tx.price == 8  # the sell price
    assert tx.total_amount == 40  # 5 * 8

    # Price difference refund: 100 - 5*10 + 5*(10-8) = 60
    assert buyer.money == 60
    assert buyer.inventory.get_quantity(food_commodity) == 5
    assert seller.money == 90  # default 50 + 40
    assert seller.inventory.get_quantity(food_commodity) == 5


def test_order_partial_matching(commodity_registry, food_commodity, mock_sim) -> None:
    """A smaller buy fills partially and leaves the sell remainder resting."""
    market = Market()
    market.commodity_registry = commodity_registry

    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    seller = get_actor("Seller", mock_sim)

    seller.inventory.add_commodity(food_commodity, 10)

    market.place_buy_order(buyer, food_commodity, 3, 10)
    market.place_sell_order(seller, food_commodity, 8, 8)

    market.match_orders()

    assert len(market.transaction_history) == 1
    tx = market.transaction_history[0]
    assert tx.quantity == 3  # the buy quantity

    assert buyer.inventory.get_quantity(food_commodity) == 3
    assert seller.inventory.get_quantity(food_commodity) == 7  # 10 - 3

    assert len(market.sell_orders[food_commodity]) == 1
    assert market.sell_orders[food_commodity][0].quantity == 5  # 8 - 3


def test_no_match_when_bid_too_low(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """Orders do not match when the bid is below the ask."""
    market = Market()
    market.commodity_registry = commodity_registry

    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    seller = get_actor("Seller", mock_sim)

    seller.inventory.add_commodity(food_commodity, 10)

    market.place_buy_order(buyer, food_commodity, 5, 7)
    market.place_sell_order(seller, food_commodity, 5, 8)

    market.match_orders()

    assert len(market.transaction_history) == 0

    assert len(market.buy_orders[food_commodity]) == 1
    assert len(market.sell_orders[food_commodity]) == 1


def test_get_avg_price(commodity_registry, food_commodity) -> None:
    """get_avg_price returns the default 10 with no trades, else the mean."""
    market = Market()
    market.commodity_registry = commodity_registry

    assert market.get_avg_price(food_commodity) == 10

    # In production only match_orders writes this list. Ticking the turn
    # clears the per-turn history-read memo as run_turn does.
    market.last_traded_prices[food_commodity] = [8, 9, 10]
    market.set_current_turn(1)

    assert market.get_avg_price(food_commodity) == 9


def test_has_price_signal(commodity_registry, food_commodity) -> None:
    """has_price_signal distinguishes a real traded price from the default 10."""
    market = Market()
    market.commodity_registry = commodity_registry

    # Never traded: get_avg_price returns 10, but there is no real signal.
    assert market.get_avg_price(food_commodity) == 10
    assert market.has_price_signal(food_commodity) is False

    # A recorded trade is a real signal. In production only match_orders
    # writes this list. Ticking the turn clears the per-turn memo as run_turn
    # does.
    market.last_traded_prices[food_commodity] = [8, 9, 10]
    market.set_current_turn(1)
    assert market.has_price_signal(food_commodity) is True

    # price_history alone, after recent trades age out, also counts.
    market.last_traded_prices[food_commodity] = []
    market.price_history[food_commodity] = [9]
    market.set_current_turn(2)
    assert market.has_price_signal(food_commodity) is True


def test_self_crossing_pair_does_not_execute(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """An actor never fills its own ask, even when its bid is the best."""
    market = Market()
    market.commodity_registry = commodity_registry

    trader = get_actor("Trader", mock_sim, initial_money=100)
    trader.inventory.add_commodity(food_commodity, 5)

    market.place_buy_order(trader, food_commodity, 5, 10)
    market.place_sell_order(trader, food_commodity, 5, 8)

    market.match_orders()

    assert market.transaction_history == []
    # Both orders survive untouched and stay available to third parties.
    assert market.buy_orders[food_commodity][0].quantity == 5
    assert market.sell_orders[food_commodity][0].quantity == 5
    assert trader.inventory.get_quantity(food_commodity) == 5


def test_third_party_still_fills_at_the_self_crossing_price(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """Stepping over a self-cross must not cost a third party its fill.

    The self-crossing actor holds both the best bid and the best ask. A
    second seller quoting the same ask price must still trade with that bid,
    and a second buyer bidding the same price must still lift the first
    actor's ask.
    """
    market = Market()
    market.commodity_registry = commodity_registry

    trader = get_actor("Trader", mock_sim, initial_money=100)
    trader.inventory.add_commodity(food_commodity, 5)
    other_seller = get_actor("OtherSeller", mock_sim)
    other_seller.inventory.add_commodity(food_commodity, 5)
    other_buyer = get_actor("OtherBuyer", mock_sim, initial_money=100)

    market.place_buy_order(trader, food_commodity, 5, 10)
    market.place_sell_order(trader, food_commodity, 5, 8)
    # Exact (price, timestamp) ties are broken at random, so the third
    # parties arrive a turn later: the self-crossing actor holds strict time
    # priority on both sides, which is the case the guard has to get right.
    market.current_turn += 1
    market.place_sell_order(other_seller, food_commodity, 5, 8)
    market.place_buy_order(other_buyer, food_commodity, 5, 10)

    market.match_orders()

    pairs = {(tx.buyer.name, tx.seller.name) for tx in market.transaction_history}
    assert pairs == {("Trader", "OtherSeller"), ("OtherBuyer", "Trader")}
    assert all(tx.buyer is not tx.seller for tx in market.transaction_history)
    assert sum(tx.quantity for tx in market.transaction_history) == 10


def test_self_cross_does_not_block_a_cheaper_own_ask_for_a_later_bidder(
    commodity_registry, food_commodity, mock_sim
) -> None:
    """An ask stepped over for its owner's bid stays available to the next bid."""
    market = Market()
    market.commodity_registry = commodity_registry

    trader = get_actor("Trader", mock_sim, initial_money=100)
    trader.inventory.add_commodity(food_commodity, 3)
    other_seller = get_actor("OtherSeller", mock_sim)
    other_seller.inventory.add_commodity(food_commodity, 3)
    later_buyer = get_actor("LaterBuyer", mock_sim, initial_money=100)

    # Trader's bid is best and crosses its own cheap ask; the only other ask
    # is above its bid, so the matcher would otherwise stop at the self-cross.
    market.place_buy_order(trader, food_commodity, 3, 6)
    market.place_sell_order(trader, food_commodity, 3, 4)
    market.place_sell_order(other_seller, food_commodity, 3, 9)
    market.place_buy_order(later_buyer, food_commodity, 3, 5)

    market.match_orders()

    assert len(market.transaction_history) == 1
    tx = market.transaction_history[0]
    assert tx.buyer is later_buyer
    assert tx.seller is trader
    assert tx.price == 4
