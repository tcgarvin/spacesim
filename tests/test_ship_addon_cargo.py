"""Add-on cargo: a plan fills the rest of the hold with other goods for its destination.

- With money and space left after the plan's own bid, the ship bids for a
  second commodity the destination also wants.
- A commodity the destination does not bid for is not added.
- Add-on cargo already aboard is not listed for sale at the origin while the
  plan accumulates.
- Fuel is never an add-on; the tank handles fuel.
"""

from spacesim.core.commodity import CommodityDefinition
from tests.test_ship_fuel import _make_ship, _make_world


def _add_commodity(sim, commodity_id, name):
    commodity = CommodityDefinition(
        id=commodity_id, name=name, transportable=True, description=name
    )
    sim.commodity_registry.add_commodity(commodity)
    return commodity


def _addon_world(tools_bid_at_destination=True, food_depth=100):
    """A at the origin sells fuel, food and tools; B bids for food (and tools)."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    tools = _add_commodity(sim, "simple_tools", "Tools")

    fuel_supplier = _make_ship(sim, a, hold_fuel=400, name="FuelSupplier")
    a.market.place_sell_order(fuel_supplier, fuel, 200, 10)
    food_seller = _make_ship(sim, a, name="FoodSeller")
    food_seller.cargo.add_commodity(food, food_depth)
    a.market.place_sell_order(food_seller, food, food_depth, 10)
    # Food also clears here at 5 units a turn, so a plan may be sized above
    # the resting depth and fill over several docked turns.
    a.market.last_traded_prices[food] = [10]
    a.market.price_history[food] = [10]
    a.market.volume_history[food] = [5]
    a.market._clear_history_read_caches()
    tools_seller = _make_ship(sim, a, name="ToolsSeller")
    tools_seller.cargo.add_commodity(tools, 50)
    a.market.place_sell_order(tools_seller, tools, 50, 20)

    buyer = _make_ship(sim, b, money=50000, name="Buyer")
    b.market.place_buy_order(buyer, food, 50, 25)
    if tools_bid_at_destination:
        b.market.place_buy_order(buyer, tools, 30, 40)

    trader = _make_ship(sim, a, money=3000, name="Trader")
    trader.fuel = trader.fuel_capacity
    trader.brain._nav.refresh_market_facts()
    return sim, fuel, food, tools, a, b, trader


def _orders_by(market, side, commodity, ship):
    book = market.buy_orders if side == "buy" else market.sell_orders
    return [o for o in book[commodity] if o.actor is ship]


def test_plan_bids_for_a_second_commodity_the_destination_wants():
    sim, fuel, food, tools, a, b, trader = _addon_world()

    trader.brain.decide_trade_actions()

    plan = trader.brain._current_plan
    assert plan is not None
    assert plan.commodity is food and plan.destination is b
    food_bids = _orders_by(a.market, "buy", food, trader)
    tools_bids = _orders_by(a.market, "buy", tools, trader)
    assert food_bids and tools_bids
    assert 0 < sum(o.quantity for o in tools_bids) <= 30
    assert trader.brain._addon_commodities == {tools}
    # The add-on fits in the space and money the plan left over.
    total_units = sum(o.quantity for o in food_bids + tools_bids)
    assert total_units <= trader.cargo_capacity


def test_no_addon_without_destination_demand():
    sim, fuel, food, tools, a, b, trader = _addon_world(tools_bid_at_destination=False)

    trader.brain.decide_trade_actions()

    assert trader.brain._current_plan is not None
    assert not _orders_by(a.market, "buy", tools, trader)
    assert trader.brain._addon_commodities == set()


def test_addon_cargo_is_not_sold_at_the_origin_while_accumulating():
    # Only 30 food rests at A, so the plan the flow supports accumulates.
    sim, fuel, food, tools, a, b, trader = _addon_world(food_depth=30)

    trader.brain.decide_trade_actions()
    assert trader.brain._current_plan is not None
    assert _orders_by(a.market, "buy", tools, trader)
    a.market.match_orders()
    assert trader.cargo.get_quantity(tools) > 0
    assert 0 < trader.cargo.get_quantity(food) < trader.brain._current_plan.quantity

    trader.brain.decide_trade_actions()

    assert trader.brain._current_plan is not None
    assert not trader.brain._plan_loaded
    assert not _orders_by(a.market, "sell", tools, trader)
    assert not _orders_by(a.market, "sell", food, trader)


def test_fuel_is_never_an_addon():
    sim, fuel, food, tools, a, b, trader = _addon_world()
    buyer = _make_ship(sim, b, money=50000, name="FuelBuyer")
    b.market.place_buy_order(buyer, fuel, 40, 60)
    trader.brain._nav.refresh_market_facts()

    trader.brain.decide_trade_actions()

    plan = trader.brain._current_plan
    assert plan is not None
    assert fuel not in trader.brain._addon_commodities
