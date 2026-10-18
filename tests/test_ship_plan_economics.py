"""Tests for how a ship prices a trade plan.

Three rules the plan economics have to keep straight:
- Fuel: the *margin* carries the outbound leg only, since the return leg is
  capital the next trade spends, while the *cash gate* still withholds the
  whole round trip so a disappointing trade cannot leave a ship broke and dry.
- Prices: what the ship bids and what the cargo is expected to cost are
  different numbers. A cheap resting ask is the cost basis; the bid may sit
  above it to win units out of the turn's flow.
- Fuel plans: a nova_fuel plan counts tank fuel above the travel reserve as
  its load at the origin, so it can reach ``_plan_loaded`` and fly.
"""

from spacesim.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim.core.galaxy import StarLaneNetwork
from spacesim.core.market import Market
from spacesim.core.planet import Planet
from spacesim.core.ship import (
    ACCUMULATION_PATIENCE,
    DEMAND_HORIZON_TURNS,
    FLOW_CAPTURE_SHARE,
    FLOW_SHARE_EXTERNAL,
    FUEL_MILLI,
    Ship,
    milli_to_units_ceil,
    milli_to_units_floor,
    units_to_milli,
)


def _make_world(planet_specs):
    """Build a nova_fuel and food registry, planets with markets, and a mock sim."""
    registry = CommodityRegistry()
    fuel = CommodityDefinition(
        id="nova_fuel",
        name="NovaFuel",
        transportable=True,
        description="High-density energy source for starship travel.",
    )
    food = CommodityDefinition(
        id="food", name="Food", transportable=True, description="Basic sustenance."
    )
    components = CommodityDefinition(
        id="ship_components",
        name="Ship Components",
        transportable=True,
        description="Best-quality maintenance tier.",
    )
    registry.add_commodity(fuel)
    registry.add_commodity(food)
    registry.add_commodity(components)
    planets = [Planet(name, Market(), x, y) for name, x, y in planet_specs]
    sim = type(
        "MockSim",
        (object,),
        {
            "commodity_registry": registry,
            "planets": planets,
            "star_lanes": StarLaneNetwork.complete(planets),
            "current_turn": 0,
        },
    )()
    return sim, fuel, food, planets


def _make_ship(
    sim, planet, fuel_units=0, efficiency=1.0, money=1000, name="TestShip", hold_fuel=0
):
    ship = Ship(name, sim, planet, fuel_efficiency=efficiency, initial_money=money)
    planet.add_ship(ship)
    ship.fuel = units_to_milli(fuel_units)
    if hold_fuel:
        fuel = sim.commodity_registry.get_commodity("nova_fuel")
        ship.cargo.add_commodity(fuel, hold_fuel)
    ship.check_maintenance = lambda: False  # deterministic departures
    return ship


def _give_repair_kit(ship):
    """Stock one complete maintenance tier, which zeroes the arrival fuel buffer."""
    components = ship.simulation.commodity_registry.get_commodity("ship_components")
    ship.cargo.add_commodity(components, 1)


def _record_flow(market, commodity, price, volume):
    """Give ``commodity`` a believable clearing price and recent volume."""
    market.last_traded_prices[commodity] = [price]
    market.price_history[commodity] = [price]
    market.volume_history[commodity] = [volume]
    market._clear_history_read_caches()


# ---------------------------------------------------------------------------
# Fuel: one way in the margin, round trip in the cash gate
# ---------------------------------------------------------------------------


def test_margin_charges_only_the_outbound_leg_of_fuel():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    # 10 a unit over a half-unit leg costs a whole number of credits, so the
    # doubling below is not a rounding artifact.
    a.market.place_sell_order(seller, fuel, 100, 10)
    buyer = _make_ship(sim, b, money=5000, name="Buyer")
    b.market.place_buy_order(buyer, food, 50, 30)

    trader = _make_ship(sim, a, money=5000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    # Nothing in the tank, so every unit of the outbound burn is bought here.
    assert plan.fuel_units_from_tank == 0
    # The burn is millifuel and the price is per market unit.
    assert plan.total_fuel_cost == round(
        plan.fuel_needed_one_way * plan.fuel_price_at_origin / FUEL_MILLI
    )
    assert plan.total_fuel_cost * 2 == round(
        plan.fuel_needed_round_trip * plan.fuel_price_at_origin / FUEL_MILLI
    )


def test_fuel_already_aboard_is_charged_at_the_reference_not_the_local_spike():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    # The only fuel for sale here is priced far above what fuel is worth.
    a.market.place_sell_order(seller, fuel, 100, 200)
    _record_flow(b.market, fuel, 20, 3)
    supplier_b = _make_ship(sim, b, hold_fuel=200, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 100, 20)
    buyer = _make_ship(sim, b, money=20000, name="Buyer")
    b.market.place_buy_order(buyer, food, 50, 40)

    # A full tank: the outbound leg costs the ship nothing at the local ask.
    trader = _make_ship(sim, a, fuel_units=6, money=20000, name="Trader")
    trader.brain._nav.refresh_market_facts()
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    assert plan.fuel_units_from_tank == plan.fuel_needed_one_way
    assert plan.fuel_price_at_origin == 200
    assert plan.fuel_price_from_tank < plan.fuel_price_at_origin
    assert plan.total_fuel_cost == round(
        plan.fuel_needed_one_way * plan.fuel_price_from_tank / FUEL_MILLI
    )


def test_cash_gate_still_withholds_the_whole_round_trip():
    # 400 lane units is 2 market units of fuel each way, so the outbound and
    # the round trip are distinguishable whole-unit purchases.
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 400, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 100, 40)
    buyer = _make_ship(sim, b, money=5000, name="Buyer")
    b.market.place_buy_order(buyer, food, 50, 30)

    rich = _make_ship(sim, a, money=5000, name="Rich")
    _give_repair_kit(rich)
    pair = rich.brain._pair_economics(a, b)
    assert pair is not None
    fuel_one_way = rich.fuel_required(rich.route_distance(a, b))
    # The tank is millifuel; the market sells whole units.
    assert pair.fuel_to_buy == milli_to_units_ceil(fuel_one_way * 2)

    # Enough cash for the outbound leg's fuel but not the return leg's: the
    # gate must refuse, or the ship strands at B with no way home.
    one_way_only = milli_to_units_ceil(fuel_one_way) * 40 + 10
    poor = _make_ship(sim, a, money=one_way_only, name="Poor")
    _give_repair_kit(poor)
    assert poor.brain._pair_economics(a, b) is None


# ---------------------------------------------------------------------------
# Prices: bid versus evaluation
# ---------------------------------------------------------------------------


def test_evaluation_price_uses_the_resting_ask_while_the_bid_chases_the_flow():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    # A real, deep resting ask at 10 ...
    a.market.place_sell_order(seller, food, 40, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    # ... under a flow that has been clearing at 20.
    _record_flow(a.market, food, 20, 6)
    buyer = _make_ship(sim, b, money=20000, name="Buyer")
    b.market.place_buy_order(buyer, food, 40, 30)

    trader = _make_ship(sim, a, money=20000, name="Trader")
    _give_repair_kit(trader)
    acquisition = trader.brain._origin_acquisition(a, food)
    assert acquisition is not None
    assert acquisition.entry_price == 10
    assert acquisition.bid_price == 20  # posted high to win flow units

    plan = trader.brain._evaluate_trade_opportunity(a, b, food)
    assert plan is not None
    assert plan.bid_price_per_unit == 20
    # The cargo fits inside the resting ask, so it is expected to cost 10.
    assert plan.quantity <= 40
    assert plan.purchase_price_per_unit == 10
    assert plan.total_purchase_cost == plan.quantity * 10


def test_evaluation_price_walks_past_a_thin_ask_into_the_flow():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 60)
    # Only two units resting cheap; the rest has to be won out of the flow.
    a.market.place_sell_order(seller, food, 2, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    _record_flow(a.market, food, 20, 6)
    buyer = _make_ship(sim, b, money=20000, name="Buyer")
    b.market.place_buy_order(buyer, food, 40, 30)

    trader = _make_ship(sim, a, money=20000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    assert plan.quantity > 2
    # Two units at 10, the remainder at the 20 bid, rounded up.
    expected = -(-(2 * 10 + (plan.quantity - 2) * 20) // plan.quantity)
    assert plan.purchase_price_per_unit == expected
    assert 10 < plan.purchase_price_per_unit <= 20


# ---------------------------------------------------------------------------
# Fuel plans load at the origin
# ---------------------------------------------------------------------------


def test_fuel_plan_load_is_hold_fuel_not_the_tank():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=400, name="Seller")
    a.market.place_sell_order(seller, fuel, 200, 10)
    # B pays well for fuel and has none of its own.
    buyer = _make_ship(sim, b, money=20000, name="Buyer")
    b.market.place_buy_order(buyer, fuel, 60, 40)
    _record_flow(b.market, fuel, 40, 4)

    # A full tank counts for nothing as load: the plan has to buy its whole
    # quantity into the hold.
    trader = _make_ship(sim, a, money=5000, name="Trader")
    trader.fuel = trader.fuel_capacity
    trader.brain._nav.refresh_market_facts()
    plan = trader.brain._evaluate_trade_opportunity(a, b, fuel)
    assert plan is not None

    trader.brain._current_plan = plan
    trader.brain._plan_loaded = False
    trader.brain._plan_turns_left = ACCUMULATION_PATIENCE
    assert trader.brain._sellable_quantity(fuel) == 0

    trader.brain.decide_trade_actions()
    bids = [o for o in a.market.buy_orders[fuel] if o.actor is trader]
    assert [o.quantity for o in bids] == [plan.quantity]
    assert not [o for o in a.market.sell_orders[fuel] if o.actor is trader]

    # Once the load is in the hold the pump leaves it there, the plan is
    # loaded, and the ship flies.
    trader.cargo.add_commodity(fuel, plan.quantity)
    trader.brain.decide_trade_actions()
    assert trader.brain._plan_loaded is True
    assert trader.cargo.get_quantity(fuel) == plan.quantity
    assert not [o for o in a.market.sell_orders[fuel] if o.actor is trader]
    assert trader.brain.decide_travel() is b


def test_hold_fuel_without_a_plan_is_pumped_into_the_tank_first():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=400, name="Seller")
    a.market.place_sell_order(seller, fuel, 200, 10)

    trader = _make_ship(sim, a, money=5000, name="Trader")
    trader.cargo.add_commodity(fuel, milli_to_units_floor(trader.fuel_capacity) + 5)
    trader.brain._nav.refresh_market_facts()
    assert trader.brain._current_plan is None

    trader.brain.decide_trade_actions()

    # The tank takes what it can; only the remainder is trade cargo.
    assert trader.fuel == trader.fuel_capacity
    assert trader.brain._sellable_quantity(fuel) == 5


# ---------------------------------------------------------------------------
# A ship never prices an entry off its own ask
# ---------------------------------------------------------------------------


def test_own_ask_is_not_an_acquisition_opportunity():
    """The ship's own resting ask is not something it can buy from."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    trader = _make_ship(sim, a, money=5000, name="Trader")
    trader.cargo.add_commodity(food, 10)
    a.market.place_sell_order(trader, food, 10, 12)

    # The only ask in the book is ours, and nothing has traded here, so there
    # is nothing to acquire - not a cost basis of 12.
    assert trader.brain._origin_acquisition(a, food) is None


def test_acquisition_walks_past_its_own_ask_to_the_real_one():
    """Own asks drop out of the walk; a stranger's ask still sets the entry."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, name="Seller")
    seller.cargo.add_commodity(food, 40)
    a.market.place_sell_order(seller, food, 40, 20)

    trader = _make_ship(sim, a, money=5000, name="Trader")
    trader.cargo.add_commodity(food, 10)
    a.market.place_sell_order(trader, food, 10, 5)  # our own, cheaper, ask

    acquisition = trader.brain._origin_acquisition(a, food)
    assert acquisition is not None
    assert acquisition.entry_price == 20
    assert acquisition.ask_levels == [(20, 40)]


# ---------------------------------------------------------------------------
# Quantity is capped by what the origin can supply
# ---------------------------------------------------------------------------


def test_plan_quantity_is_capped_by_origin_ask_depth_and_flow():
    """A deep destination and a full wallet do not buy units nobody is selling."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 5)
    a.market.place_sell_order(seller, food, 5, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    _record_flow(a.market, food, 10, 2)  # 2 units a turn clear here
    buyer = _make_ship(sim, b, money=100000, name="Buyer")
    b.market.place_buy_order(buyer, food, 200, 40)

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    acquisition = trader.brain._origin_acquisition(a, food)
    assert acquisition is not None
    expected = 5 + int(2 * ACCUMULATION_PATIENCE * FLOW_CAPTURE_SHARE)
    assert acquisition.acquirable == expected

    plan = trader.brain._evaluate_trade_opportunity(a, b, food)
    assert plan is not None
    assert plan.quantity == expected


def test_origin_supply_does_not_bind_when_the_book_is_deeper_than_demand():
    """The destination's demand still sizes the plan on a deep origin book."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 200)
    a.market.place_sell_order(seller, food, 200, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    buyer = _make_ship(sim, b, money=100000, name="Buyer")
    b.market.place_buy_order(buyer, food, 6, 40)

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    acquisition = trader.brain._origin_acquisition(a, food)
    assert acquisition is not None
    assert acquisition.acquirable == 200

    plan = trader.brain._evaluate_trade_opportunity(a, b, food)
    assert plan is not None
    assert plan.quantity == 6  # the destination's bid depth, not the book's


def test_no_plan_when_origin_supply_rounds_to_nothing():
    """A price signal with a trickle of flow and no resting ask buys nothing."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    a.market.place_sell_order(seller, fuel, 100, 5)
    # A believable price, but one unit cleared in five turns: under the
    # capture share, eight docked turns win no whole unit.
    a.market.last_traded_prices[food] = [10]
    a.market.price_history[food] = [10]
    a.market.volume_history[food] = [0, 0, 0, 0, 1]
    a.market._clear_history_read_caches()
    buyer = _make_ship(sim, b, money=100000, name="Buyer")
    b.market.place_buy_order(buyer, food, 200, 40)

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    assert trader.brain._origin_acquisition(a, food) is None
    assert trader.brain._evaluate_trade_opportunity(a, b, food) is None


# ---------------------------------------------------------------------------
# Destination flow: only what undercutting the local sellers can win
# ---------------------------------------------------------------------------


def _flow_world():
    """A, with 50 cheap units of food for sale, and B, with a food flow."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    return sim, fuel, food, a, b


def test_destination_flow_is_credited_under_the_local_ask():
    """Flow counts when the haircut price still undercuts what locals ask."""
    sim, fuel, food, a, b = _flow_world()
    _record_flow(b.market, food, 100, 4)  # 4 units a turn clear at 100
    local = _make_ship(sim, b, hold_fuel=0, name="LocalSeller")
    local.cargo.add_commodity(food, 100)
    b.market.place_sell_order(local, food, 100, 200)  # asks well above the flow

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    # The haircut flow price (90) is under the local ask, so it stands.
    assert plan.expected_sell_price_per_unit == 90
    # Half the horizon's flow: 4 a turn x 15 turns x FLOW_SHARE_EXTERNAL.
    assert plan.quantity == int(4 * DEMAND_HORIZON_TURNS * FLOW_SHARE_EXTERNAL)


def test_destination_flow_credits_nothing_when_locals_undersell_the_entry():
    """A ship cannot take flow at a price under what the cargo cost it."""
    sim, fuel, food, a, b = _flow_world()
    _record_flow(b.market, food, 100, 4)
    local = _make_ship(sim, b, hold_fuel=0, name="LocalSeller")
    local.cargo.add_commodity(food, 100)
    # Locals rest at 10, the entry price at A: undercutting them loses money.
    b.market.place_sell_order(local, food, 100, 10)

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    assert trader.brain._creditable_flow(b.market, food, 10) == (0, 0)
    # No resting bid either, so the flow was the whole case for the plan.
    assert trader.brain._evaluate_trade_opportunity(a, b, food) is None


def test_flow_price_is_capped_one_tick_under_the_local_ask():
    """A seller resting below the flow price sets what the ship can get."""
    sim, fuel, food, a, b = _flow_world()
    _record_flow(b.market, food, 100, 4)
    local = _make_ship(sim, b, hold_fuel=0, name="LocalSeller")
    local.cargo.add_commodity(food, 100)
    b.market.place_sell_order(local, food, 100, 60)

    trader = _make_ship(sim, a, money=100000, name="Trader")
    _give_repair_kit(trader)
    assert trader.brain._creditable_flow(b.market, food, 10) == (
        59,
        int(4 * DEMAND_HORIZON_TURNS * FLOW_SHARE_EXTERNAL),
    )
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)
    assert plan is not None
    assert plan.expected_sell_price_per_unit == 59
