"""Tests for the ship fuel system.

- fuel_required: planning uses the same efficiency-adjusted number as
  consumption at departure.
- Fuel-aware route planning: destinations without purchasable fuel are only
  accepted when an escape route remains; plans that need fuel the origin
  cannot sell are rejected; docked ships top up toward a full tank.
- Standing fuel bids: a stranded ship posts a resting buy order priced to
  make delivery profitable for another trader, re-posts it each turn, and
  escalates the fallback price with scarcity pressure while the galaxy has no
  believable fuel price to cap the bid against.
"""

import math
from types import SimpleNamespace

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.galaxy import StarLaneNetwork
from spacesim2.core.market import Market
from spacesim2.core.navigation import FUEL_BID_MARGIN, get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.ship import (
    ACCUMULATION_PATIENCE,
    BASE_FUEL_CAPACITY,
    DISTRESS_PATIENCE,
    FUEL_BID_FALLBACK_FLOOR,
    MAINTENANCE_FUEL_MILLI,
    Ship,
    TradePlan,
    milli_to_units_ceil,
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
    sim,
    planet,
    fuel_units=0,
    efficiency=1.0,
    money=1000,
    name="TestShip",
    hold_fuel=0,
    fuel_milli=0,
):
    """Dock a ship at ``planet`` with a stocked tank.

    ``fuel_units`` is whole market units of nova_fuel in the tank;
    ``fuel_milli`` adds thousandths on top, for scenarios pitched against a
    single leg's burn, which is a fraction of a unit at these distances.
    """
    ship = Ship(name, sim, planet, fuel_efficiency=efficiency, initial_money=money)
    planet.add_ship(ship)
    ship.fuel = units_to_milli(fuel_units) + fuel_milli
    if hold_fuel:
        fuel = sim.commodity_registry.get_commodity("nova_fuel")
        ship.cargo.add_commodity(fuel, hold_fuel)
    ship.check_maintenance = lambda: False  # deterministic departures
    return ship


def _give_repair_kit(ship):
    """Stock one complete maintenance tier, which zeroes the arrival fuel buffer."""
    components = ship.simulation.commodity_registry.get_commodity("ship_components")
    ship.cargo.add_commodity(components, 1)


# ---------------------------------------------------------------------------
# fuel_required matches consumption
# ---------------------------------------------------------------------------


def test_fuel_required_applies_efficiency():
    sim, _, _, (a, _) = _make_world([("A", 0, 0), ("B", 50, 0)])
    nominal = _make_ship(sim, a, efficiency=1.0)
    inefficient = _make_ship(sim, a, efficiency=0.8)
    efficient = _make_ship(sim, a, efficiency=1.2)

    # Millifuel, one ceiling: ceil(distance * 1000 / (200 * efficiency)).
    assert Ship.calculate_fuel_needed(50.0) == 250
    assert nominal.fuel_required(50.0) == 250
    assert inefficient.fuel_required(50.0) == 313  # ceil(50000 / 160)
    assert efficient.fuel_required(50.0) == 209  # ceil(50000 / 240)


def test_departure_consumes_exactly_the_planned_fuel():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=1, efficiency=0.8)
    needed = ship.fuel_required(ship.route_distance(a, b))

    assert ship.start_journey(b)
    assert ship.fuel == units_to_milli(1) - needed


def test_trade_plan_reserves_efficiency_adjusted_fuel():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, hold_fuel=100, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 50, 5)
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    b.market.place_buy_order(buyer, food, 50, 25)

    trader = _make_ship(sim, a, efficiency=0.8, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    distance = trader.route_distance(a, b)
    assert plan.fuel_needed_one_way == trader.fuel_required(distance)
    assert plan.fuel_needed_one_way == 625  # ceil(100 * 1000 / (200 * 0.8))


# ---------------------------------------------------------------------------
# Fuel-aware route planning
# ---------------------------------------------------------------------------


def test_plan_rejected_when_origin_cannot_sell_needed_fuel():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    # Plans need visible profitable bids at the destination.
    buyer_b = _make_ship(sim, b, money=2000, name="BuyerB")
    b.market.place_buy_order(buyer_b, food, 50, 25)
    # Fuel is only for sale at the destination, not the origin.
    supplier_b = _make_ship(sim, b, hold_fuel=100, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 50, 12)

    trader = _make_ship(sim, a, fuel_units=0, name="Trader")
    assert trader.brain._evaluate_trade_opportunity(a, b, food) is None

    # With round-trip fuel already on board, the same trade is feasible.
    trader.fuel += 4 * trader.fuel_required(trader.route_distance(a, b))
    assert trader.brain._evaluate_trade_opportunity(a, b, food) is not None


def test_fuel_safe_destination_requires_escape_route():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=100, name="Supplier")
    ship = _make_ship(sim, a, name="Trader")
    _give_repair_kit(ship)
    escape_cost = ship.fuel_required(ship.route_distance(a, b))

    # No fuel for sale anywhere: the minimum requirement is keeping the
    # return leg.
    assert not ship.brain._fuel_safe_destination(b, a, escape_cost - 1)
    assert ship.brain._fuel_safe_destination(b, a, escape_cost)

    # Fuel for sale at A: B is safe only with enough fuel to get back to A.
    # Market facts are a per-turn shared snapshot, so tests that change the
    # books mid-turn refresh the navigator explicitly.
    a.market.place_sell_order(supplier, fuel, 50, 10)
    ship.brain._nav.refresh_market_facts()
    assert not ship.brain._fuel_safe_destination(b, a, escape_cost - 1)
    assert ship.brain._fuel_safe_destination(b, a, escape_cost)

    # A trickle ask at B does not waive the reserve: one unit cannot refill a
    # tank, so A is still the nearest station and the escape leg back stands.
    supplier_b = _make_ship(sim, b, hold_fuel=100, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 1, 10)
    ship.brain._nav.refresh_market_facts()
    assert not ship.brain._fuel_safe_destination(b, a, 0)
    assert ship.brain._fuel_safe_destination(b, a, escape_cost)

    # Station depth at B does waive it: a burn there is re-buyable.
    b.market.place_sell_order(supplier_b, fuel, 50, 10)
    ship.brain._nav.refresh_market_facts()
    assert ship.brain._nav.fuel_station_at(b)
    assert ship.brain._fuel_safe_destination(b, a, 0)


def test_decide_travel_avoids_fuel_dead_end():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    # One and a half legs: enough to reach B, short of the return leg, so B
    # is a dead end while no one sells fuel.
    leg = Ship.calculate_fuel_needed(100.0)
    ship = _make_ship(sim, a, fuel_milli=leg + leg // 2, name="Trader")
    ship.cargo.add_commodity(food, 20)
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    b.market.place_buy_order(buyer, food, 20, 30)

    # B pays well for the cargo but neither planet sells fuel, so flying
    # there would strand the ship.
    assert ship.brain.decide_travel() is None

    # Once fuel is for sale at B, the trip is safe. Refresh the per-turn
    # snapshot so the mid-turn book change is visible now.
    supplier_b = _make_ship(sim, b, hold_fuel=100, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 50, 10)
    ship.brain._nav.refresh_market_facts()
    assert ship.brain.decide_travel() is b


def test_docked_ship_tops_up_toward_the_bunker_target():
    """Cheap fuel fills to the bunker target, not to the much larger tank."""
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)

    # Below the target the ship tops up toward it.
    ship = _make_ship(sim, a, fuel_milli=0, name="Trader")
    target = ship.brain._fuel_bunker_target()
    assert target < ship.fuel_capacity
    ship.fuel = target // 2
    ship.brain.decide_trade_actions()

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(target - ship.fuel)

    a.market.match_orders()
    ship.pump_fuel()
    assert ship.fuel == target


def test_tank_fuel_is_not_trade_cargo_at_ordinary_prices():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    # An ordinary low bid, below the scarcity floor of 15.
    b.market.place_buy_order(buyer, fuel, 20, 10)

    # A well-fueled ship with no delivery plan must not dump its tank into
    # the local bid, which would bleed the spread on the later re-buy, and
    # must not post a standing bid either.
    ship = _make_ship(sim, b, fuel_milli=BASE_FUEL_CAPACITY, name="Trader")
    ship.brain.decide_trade_actions()

    assert not any(o.actor is ship for o in b.market.sell_orders[fuel])
    assert not any(o.actor is ship for o in b.market.buy_orders[fuel])


def test_hold_fuel_is_trade_cargo_and_the_tank_is_not():
    """Fuel the pump cannot fit in the tank sells like any cargo; the tank never does."""
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 20)
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    b.market.place_buy_order(buyer, fuel, 20, 30)

    ship = _make_ship(sim, b, hold_fuel=12, name="Trader")
    ship.fuel = ship.fuel_capacity
    ship.brain.decide_trade_actions()

    sells = [o for o in b.market.sell_orders[fuel] if o.actor is ship]
    assert sum(o.quantity for o in sells) == 12
    assert ship.fuel == ship.fuel_capacity


# ---------------------------------------------------------------------------
# Standing fuel bids
# ---------------------------------------------------------------------------


def test_stranded_ship_posts_bid_profitable_for_deliverer():
    # A lane long enough that the survival target is several market units:
    # a one-unit rescue run cannot amortize the deliverer's burn, which the
    # bid pricing itself says.
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 300, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)

    stranded = _make_ship(sim, b, fuel_units=0, money=1000, name="Stranded")
    stranded.brain.decide_trade_actions()

    bids = [o for o in b.market.buy_orders[fuel] if o.actor is stranded]
    assert len(bids) == 1
    bid = bids[0]
    assert bid.quantity > 0
    assert bid.quantity <= milli_to_units_ceil(stranded.fuel_capacity)
    # Priced at least the source ask plus the delivery margin.
    assert bid.price >= math.ceil(10 * (1 + FUEL_BID_MARGIN))
    # Funds for the bid were reserved, not overspent.
    assert stranded.money >= 0
    assert stranded.reserved_money == bid.quantity * bid.price

    # A trader on the fuel-rich planet sees the resting bid and rates the
    # delivery run as its best, profitable plan. The stranded ship's decision
    # snapshotted the books before its bid was posted, so refresh to see it.
    deliverer = _make_ship(
        sim, a, fuel_milli=BASE_FUEL_CAPACITY, money=1000, name="Deliverer"
    )
    # A kit, so the arrival gate asks for the escape leg and not the five
    # units a maintenance roll could burn on top of it.
    _give_repair_kit(deliverer)
    deliverer.brain._nav.refresh_market_facts()
    plan = deliverer.brain._find_best_trade_plan()
    assert plan is not None
    assert plan.commodity.id == "nova_fuel"
    assert plan.destination is b
    assert plan.is_profitable()


def test_standing_bid_fires_below_reserve_not_only_at_zero():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)

    # Below the round-trip reserve of 2 * fuel_required(60) = 600 millifuel,
    # the bid fires.
    low = _make_ship(sim, b, fuel_milli=200, money=1000, name="Low")
    low.brain.decide_trade_actions()
    assert any(o.actor is low for o in b.market.buy_orders[fuel])

    # Above the reserve: no bid.
    healthy = _make_ship(
        sim, b, fuel_milli=BASE_FUEL_CAPACITY, money=1000, name="Healthy"
    )
    healthy.brain.decide_trade_actions()
    assert not any(o.actor is healthy for o in b.market.buy_orders[fuel])


def test_standing_bid_reposts_and_escalates_when_unfilled():
    """With no believable fuel price anywhere, the bid still escalates.

    The bid ceiling is what stops a spiral, and it needs a galaxy reference to
    measure against. A bootstrap galaxy has none, and the escalation is how
    the first fuel is called into existence; see
    tests/test_ship_fuel_discipline.py for the capped case.
    """
    # No fuel ask anywhere in the galaxy: fallback pricing applies.
    sim, fuel, _, (_, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    stranded = _make_ship(sim, b, fuel_units=0, money=1000, name="Stranded")
    assert get_navigator(sim).fuel_value_reference() is None

    stranded.brain.decide_trade_actions()
    first = [o for o in b.market.buy_orders[fuel] if o.actor is stranded]
    assert len(first) == 1
    first_price = first[0].price

    # End of turn: the bid goes unfilled, scarcity pressure builds.
    b.market.match_orders()

    stranded.brain.decide_trade_actions()
    second = [o for o in b.market.buy_orders[fuel] if o.actor is stranded]
    assert len(second) == 1
    assert second[0].price > first_price


def test_deliverer_sells_its_hold_load_and_keeps_the_tank():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    b.market.place_buy_order(buyer, fuel, 40, 18)

    leg = Ship.calculate_fuel_needed(60.0)
    deliverer = _make_ship(sim, b, fuel_milli=2 * leg, hold_fuel=34, name="Deliverer")
    deliverer.brain._current_plan = TradePlan(
        origin=a,
        destination=b,
        commodity=fuel,
        quantity=34,
        bid_price_per_unit=10,
        purchase_price_per_unit=10,
        expected_sell_price_per_unit=18,
        distance=60.0,
        fuel_needed_one_way=leg,
        fuel_price_at_origin=10,
        fuel_units_from_tank=leg,
        fuel_price_from_tank=10,
    )
    deliverer.brain.decide_trade_actions()

    # The load stays in the hold for sale; the pump does not eat it, and the
    # sale does not touch the tank.
    sells = [o for o in b.market.sell_orders[fuel] if o.actor is deliverer]
    assert sum(o.quantity for o in sells) == 34
    assert deliverer.fuel == 2 * leg


# ---------------------------------------------------------------------------
# Deadlock fixes: stale-order cleanup, hold/travel consistency, maintenance
# rescue bids
# ---------------------------------------------------------------------------


def test_departure_cancels_resting_orders():
    """Departing reclaims reserved cargo and money from unfilled local orders.

    Cancellation is local-market-only, so orders left behind hold their
    reserves forever if the ship never returns. Fuel reserved in a stale ask
    otherwise deadlocks a ship in needs_maintenance.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=1, hold_fuel=10)
    a.market.place_sell_order(ship, fuel, 8, 99)
    assert ship.cargo.get_available_quantity(fuel) == 2  # the rest is reserved

    assert ship.start_journey(b)

    assert not a.market.sell_orders[fuel]
    assert ship.cargo.get_reserved_quantity(fuel) == 0
    assert ship.fuel == units_to_milli(1) - Ship.calculate_fuel_needed(50.0)


def test_sells_locally_when_better_price_is_fuel_unsafe():
    """A better price at a fuel dead end does not keep cargo on hold forever.

    decide_travel vetoes unsafe destinations, so the hold decision must apply
    the same veto or the ship waits for a trip it never takes.
    """
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, b, money=5000, name="RemoteBuyer")
    b.market.place_buy_order(remote_buyer, food, 20, 100)

    # Exactly one-way fuel: B is reachable but leaves no escape route.
    ship = _make_ship(
        sim, a, fuel_milli=Ship.calculate_fuel_needed(50.0), name="Holder"
    )
    ship.cargo.add_commodity(food, 10)

    ship.brain.decide_trade_actions()

    assert any(o.actor is ship for o in a.market.sell_orders[food])
    assert ship.brain.decide_travel() is None


def test_no_departure_same_turn_as_local_sell():
    """Once the brain commits to selling here, it does not depart this turn."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, b, money=5000, name="RemoteBuyer")
    # Only 10% better is not enough to hold, so the ship sells locally.
    b.market.place_buy_order(remote_buyer, food, 20, 11)

    ship = _make_ship(sim, a, fuel_units=10, name="Seller")
    ship.cargo.add_commodity(food, 10)

    ship.brain.decide_trade_actions()
    assert any(o.actor is ship for o in a.market.sell_orders[food])

    # It must not also fly to B and strand the just-placed sell order.
    assert ship.brain.decide_travel() is None


def test_maintenance_standing_bid_when_no_asks():
    """A broken ship with no supplies for sale posts an escalating bid."""
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=0, money=1000)

    ship._buy_maintenance_supplies()
    bids = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(bids) == 1
    assert bids[0].quantity == 1  # the fuel tier, rounded up to a market unit
    first_price = bids[0].price
    assert first_price >= 10

    # Unfilled at end of turn: scarcity pressure builds and the re-posted bid
    # escalates.
    a.market.match_orders()
    ship._buy_maintenance_supplies()
    bids = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(bids) == 1
    assert bids[0].price > first_price


def test_topup_rations_fuel_at_spike_prices():
    """A scarcity-priced local ask buys the way out and nothing more.

    Above the bunker ceiling the ship buys only the escape target: the leg
    to the nearest fuel seller plus that planet's own arrival floor. Filling
    a tank, or even a full survival target, at spike prices bankrupts ships.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    # Cheap fuel at B sets the galaxy reference near 10.
    remote_supplier = _make_ship(sim, b, hold_fuel=200, name="RemoteSupplier")
    b.market.place_sell_order(remote_supplier, fuel, 100, 10)
    # The local ask at A is spike-priced.
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    a.market.place_sell_order(local_supplier, fuel, 100, 60)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    escape_target = ship.brain._fuel_escape_target()
    assert escape_target is not None
    assert escape_target < ship.brain._fuel_survival_target()

    ship.brain.decide_trade_actions()

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(escape_target)
    assert buys[0].quantity < milli_to_units_ceil(ship.brain._fuel_bunker_target())

    # The rationed tank still clears the departure gate for the escape hop.
    a.market.match_orders()
    ship.pump_fuel()
    assert ship.fuel >= ship.brain._departure_fuel_requirement(b)


def test_topup_buys_the_full_survival_target_within_the_premium():
    """At or below the bunker ceiling the survival target is not rationed.

    Rationing exists to stop spike spending; a fairly priced market should
    leave the ship as fuelled as it ever was.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    remote_supplier = _make_ship(sim, b, hold_fuel=200, name="RemoteSupplier")
    b.market.place_sell_order(remote_supplier, fuel, 100, 10)
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    # Within FUEL_BUNKER_PREMIUM of the reference near 10.
    a.market.place_sell_order(local_supplier, fuel, 100, 13)

    ship = _make_ship(sim, a, fuel_units=0, money=400, name="Trader")
    survival_target = ship.brain._fuel_survival_target()
    escape_target = ship.brain._fuel_escape_target()
    assert escape_target is not None and escape_target < survival_target

    ship.brain.decide_trade_actions()

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity >= milli_to_units_ceil(survival_target)


def test_topup_falls_back_to_survival_target_without_a_safe_escape():
    """With nowhere to escape to, the ship overpays rather than dying.

    Other planets have a cheap price signal, so the reference is low and the
    local ask reads as a spike, but none of them has a live ask, so there is
    no escape hop to fund. Overpaying beats stranding.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    # A cheap history at B, but no resting ask: reference is low, and fuel is
    # purchasable nowhere but A.
    b.market.price_history[fuel] = [10] * 30
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    a.market.place_sell_order(local_supplier, fuel, 100, 60)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    assert ship.brain._fuel_escape_target() is None

    ship.brain.decide_trade_actions()

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(ship.brain._fuel_survival_target())


def test_committed_destination_fuel_is_still_bought_at_a_spike():
    """Fuel for a trip the ship already committed cargo to escapes rationing.

    The hold-or-sell comparison proved that trip profitable at this ask, so
    the escape-leg ration must not strand the cargo.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    remote_supplier = _make_ship(sim, b, hold_fuel=200, name="RemoteSupplier")
    b.market.place_sell_order(remote_supplier, fuel, 100, 10)
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    a.market.place_sell_order(local_supplier, fuel, 100, 60)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    committed = ship.fuel_capacity
    ship.brain._committed_fuel_need = committed
    assert committed > ship.brain._fuel_survival_target()

    assert ship.brain._opportunistic_fuel_topup() is not None

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == min(milli_to_units_ceil(committed), 5000 * 9 // 10 // 60)


def test_topup_bunkers_at_cheap_prices():
    """Near the galaxy reference price, the ship fills to the bunker target."""
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    ship.brain.decide_trade_actions()

    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(ship.brain._fuel_bunker_target())


def test_topup_bunkers_near_the_median_despite_a_cheap_outlier():
    """One cheap planet elsewhere no longer blocks bunkering everywhere.

    The reference is the median believable valuation, so a local ask close
    to what fuel typically costs still buys the whole bunker target even when
    some distant planet sells far cheaper.
    """
    specs = [(f"P{i}", 100 * i, 0) for i in range(5)]
    sim, fuel, _, planets = _make_world(specs)
    home = planets[0]
    # One cheap outlier, the rest of the galaxy near 30.
    outlier = _make_ship(sim, planets[1], hold_fuel=200, name="Outlier")
    planets[1].market.place_sell_order(outlier, fuel, 100, 5)
    for planet in planets[2:]:
        supplier = _make_ship(sim, planet, hold_fuel=200, name=f"S{planet.name}")
        planet.market.place_sell_order(supplier, fuel, 100, 30)
    local = _make_ship(sim, home, hold_fuel=200, name="LocalSupplier")
    home.market.place_sell_order(local, fuel, 100, 33)

    ship = _make_ship(sim, home, fuel_units=0, money=20000, name="Trader")
    ship.brain.decide_trade_actions()

    buys = [o for o in home.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(ship.brain._fuel_bunker_target())

    # An ask far above the median only buys the escape target.
    home.market.cancel_order(buys[0].order_id)
    for order in list(home.market.sell_orders[fuel]):
        home.market.cancel_order(order.order_id)
    spiker = _make_ship(sim, home, hold_fuel=200, name="Spiker")
    home.market.place_sell_order(spiker, fuel, 100, 150)
    ship.brain._nav.refresh_market_facts()
    ship.brain.decide_trade_actions()

    buys = [o for o in home.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    assert buys[0].quantity == milli_to_units_ceil(ship.brain._fuel_escape_target())


def test_plan_quantity_capped_by_destination_bid_depth():
    """Plans buy no more cargo than the destination book can absorb."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 80)
    a.market.place_sell_order(seller, food, 80, 10)
    a.market.place_sell_order(seller, fuel, 50, 5)
    # Destination demand: only 7 units bid above cost.
    buyer = _make_ship(sim, b, money=2000, name="Buyer")
    b.market.place_buy_order(buyer, food, 7, 30)

    trader = _make_ship(sim, a, money=2000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    assert plan.quantity == 7
    assert plan.expected_sell_price_per_unit == 30


def test_plan_revenue_walks_the_bid_book():
    """Expected revenue uses each level's price, not top-of-book for all units."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 80)
    a.market.place_sell_order(seller, food, 80, 10)
    a.market.place_sell_order(seller, fuel, 50, 5)
    buyer = _make_ship(sim, b, money=5000, name="Buyer")
    b.market.place_buy_order(buyer, food, 5, 40)
    b.market.place_buy_order(buyer, food, 10, 20)

    trader = _make_ship(sim, a, money=5000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    assert plan.quantity == 15
    # Weighted average of 5@40 + 10@20 = 400/15 = 26.67 -> floor 26.
    assert plan.expected_sell_price_per_unit == (5 * 40 + 10 * 20) // 15


def test_plan_prices_in_expected_maintenance():
    """Round-trip maintenance risk is part of the plan's cost."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 50, 20)
    buyer = _make_ship(sim, b, money=5000, name="Buyer")
    b.market.place_buy_order(buyer, food, 20, 30)

    trader = _make_ship(sim, a, money=5000, name="Trader")
    _give_repair_kit(trader)
    plan = trader.brain._evaluate_trade_opportunity(a, b, food)

    assert plan is not None
    # 2 departures x 10% x 0.5 fuel units x fuel ask 20 = 2.
    assert plan.expected_maintenance_cost == 2
    assert (
        plan.expected_profit
        == plan.expected_revenue - plan.total_purchase_cost - plan.total_fuel_cost - 2
    )


def test_standing_fuel_bid_capped_at_survival_target():
    """Rescue bids ask for mobility, not a full tank of reserved money."""
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 60, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)

    stranded = _make_ship(sim, b, fuel_units=0, money=10000, name="Stranded")
    stranded.brain.decide_trade_actions()

    bids = [o for o in b.market.buy_orders[fuel] if o.actor is stranded]
    assert len(bids) == 1
    assert bids[0].quantity == milli_to_units_ceil(
        stranded.brain._fuel_survival_target()
    )
    assert bids[0].quantity < milli_to_units_ceil(stranded.fuel_capacity)


def test_maintenance_bids_prefer_produced_tiers_and_cover_all():
    """Standing maintenance bids cover every fundable tier, produced first.

    Bidding only on the cheapest tier leaves a ship dead when nobody makes
    that tier; a simultaneous fuel-tier bid frees it.
    """
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 50, 0)])
    registry = sim.commodity_registry
    components = CommodityDefinition(
        id="ship_components",
        name="Ship Components",
        transportable=True,
        description="Precision ship repair components.",
    )
    registry.add_commodity(components)
    # Fuel has traded here; components never traded anywhere.
    a.market.last_traded_prices[fuel] = [12]

    ship = _make_ship(sim, a, fuel_units=0, money=1000)
    ship._buy_maintenance_supplies()

    fuel_bids = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    comp_bids = [o for o in a.market.buy_orders[components] if o.actor is ship]
    assert len(fuel_bids) == 1
    assert fuel_bids[0].quantity == 1
    assert len(comp_bids) == 1
    assert comp_bids[0].quantity == 1
    # The produced fuel tier was funded first. With money to spare both rest
    # in the book, so either can fill.


def test_survival_reposition_leaves_fuel_desert():
    """With no trades anywhere, a ship in a fuel desert flies to a fuel source."""
    sim, fuel, _, (desert, oasis) = _make_world([("A", 0, 0), ("B", 50, 0)])
    supplier = _make_ship(sim, oasis, hold_fuel=100, name="Supplier")
    oasis.market.place_sell_order(supplier, fuel, 50, 12)

    ship = _make_ship(sim, desert, fuel_units=10, name="Idler")
    assert ship.brain.decide_travel() is oasis

    # Where fuel is locally purchasable, idling is fine. Refresh the per-turn
    # snapshot to see the mid-turn book change.
    desert.market.place_sell_order(
        _make_ship(sim, desert, hold_fuel=20, name="LocalSupplier"), fuel, 10, 12
    )
    ship.brain._nav.refresh_market_facts()
    assert ship.brain.decide_travel() is None


# ---------------------------------------------------------------------------
# Fuel availability is a live ask, not a memory of one
# ---------------------------------------------------------------------------


def _drain_fuel_ask(planet, fuel, supplier, buyer, quantity=20, price=10):
    """Trade ``quantity`` fuel at ``planet`` so volume is recorded and the ask goes."""
    planet.market.place_sell_order(supplier, fuel, quantity, price)
    planet.market.place_buy_order(buyer, fuel, quantity, price)
    planet.market.match_orders()


def test_fuel_not_purchasable_on_recent_volume_without_a_resting_ask():
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    buyer = _make_ship(sim, a, money=5000, name="Buyer")
    nav = _make_ship(sim, b, name="Observer").brain._nav

    _drain_fuel_ask(a, fuel, supplier, buyer)
    nav.refresh_market_facts()

    # The trade that shows in the volume window is the one that emptied the
    # book, so it says nothing about what an arriving ship could buy.
    assert a.market.get_bid_ask_spread(fuel)[1] is None
    assert nav.fuel_traded_recently(a)
    assert not nav.fuel_purchasable_at(a)

    # A live ask restores purchasability.
    a.market.place_sell_order(supplier, fuel, 5, 10)
    nav.refresh_market_facts()
    assert nav.fuel_purchasable_at(a)


def test_fuel_ask_depth_counts_resting_sell_quantity():
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    nav = supplier.brain._nav

    assert nav.fuel_ask_depth_at(a) == 0
    a.market.place_sell_order(supplier, fuel, 3, 10)
    a.market.place_sell_order(supplier, fuel, 4, 12)
    nav.refresh_market_facts()
    assert nav.fuel_ask_depth_at(a) == 7


def test_arrival_requirement_holds_the_escape_leg_against_a_trickle_ask():
    """A one-unit ask does not waive the escape reserve; the kit sets the buffer.

    One unit cannot refill a tank, so the destination is not a station and
    the leg to the nearest one stands. On top of it sits the maintenance
    buffer, which a ship carrying a repair kit does not owe.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    ship = _make_ship(sim, a, name="Trader")
    escape_cost = ship.fuel_required(ship.route_distance(a, b))
    assert escape_cost == 500

    # A is a station, so the escape leg out of B is escape_cost.
    a.market.place_sell_order(supplier_a, fuel, 50, 10)
    b.market.place_sell_order(supplier_b, fuel, 1, 10)
    ship.brain._nav.refresh_market_facts()
    assert ship.brain._nav.fuel_purchasable_at(b)
    assert not ship.brain._nav.fuel_station_at(b)

    # No kit: a maintenance roll could burn MAINTENANCE_FUEL_MILLI of the
    # reserve after landing, so the requirement carries it.
    assert (
        ship.brain._arrival_fuel_requirement(b, a)
        == escape_cost + MAINTENANCE_FUEL_MILLI
    )

    _give_repair_kit(ship)
    assert ship.brain._arrival_fuel_requirement(b, a) == escape_cost
    assert not ship.brain._fuel_safe_destination(b, a, escape_cost - 1)
    assert ship.brain._fuel_safe_destination(b, a, escape_cost)


def test_departure_requirement_funds_the_escape_leg_to_the_nearest_station():
    """Hold-cargo commitment covers leg plus escape floor, not just the leg."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    a.market.place_sell_order(supplier_a, fuel, 200, 10)
    # A trickle ask at B, so B is not a station and the escape leg stands.
    b.market.place_sell_order(supplier_b, fuel, 1, 10)
    ship = _make_ship(sim, a, name="Trader")
    _give_repair_kit(ship)
    ship.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts()

    leg = ship.fuel_required(ship.route_distance(a, b))
    assert ship.brain._departure_fuel_requirement(b) == leg + leg

    # And the hold-cargo path commits exactly that, so the top-up buys it.
    # B pays far more than A, so the cargo is held for the trip to B.
    local_buyer = _make_ship(sim, a, money=9000, name="BuyerAtA")
    a.market.place_buy_order(local_buyer, food, 10, 20)
    buyer = _make_ship(sim, b, money=9000, name="BuyerAtB")
    b.market.place_buy_order(buyer, food, 10, 200)
    ship.fuel += leg + leg
    ship.brain._nav.refresh_market_facts()
    should_sell_here, committed = ship.brain._cargo_disposition(a.market)
    assert not should_sell_here
    assert committed == leg + leg

    # One unit short of the escape floor and the trip is out of reach, so
    # the cargo is sold here instead of held for a departure that is refused.
    dry = _make_ship(sim, a, fuel_milli=leg + leg - 1, money=0, name="DryTrader")
    _give_repair_kit(dry)
    dry.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts()
    assert dry.brain._cargo_disposition(a.market) == (True, 0)


def test_reposition_target_rejected_when_it_leaves_no_escape():
    """An empty reposition only picks an origin the ship can leave again."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    a.market.place_sell_order(supplier_a, fuel, 200, 10)
    # A trickle ask at B, so B is not a station and the escape leg stands.
    b.market.place_sell_order(supplier_b, fuel, 1, 10)
    # Penniless, so reach is the tank alone: repositioning counts fuel it
    # could buy here, and A has an ask, so any purse would fund the trip.
    ship = _make_ship(sim, a, money=0, name="Trader")
    _give_repair_kit(ship)
    ship.brain._nav.refresh_market_facts()

    # A warm galaxy where every origin backs a lucrative plan, so the fuel
    # gate is the only thing that can reject a candidate.
    ship.brain._nav.has_any_trade_signal = lambda: True
    ship.brain._best_plan_from = lambda origin: SimpleNamespace(
        expected_profit=1000, origin=origin, destination=b, quantity=0
    )

    leg = ship.fuel_required(ship.route_distance(a, b))

    # Exactly the leg: B is reachable, but the ship would land with nothing
    # left for the escape leg back to A.
    assert ship.brain._find_reposition_target(leg, fuel) is None

    # Leg plus escape floor: the same trip is accepted.
    assert ship.brain._find_reposition_target(leg + leg, fuel) is b


# ---------------------------------------------------------------------------
# Fuel upkeep runs whatever the plan state
# ---------------------------------------------------------------------------


def _accumulating_plan(ship, origin, destination, commodity, quantity=10):
    """Adopt a fresh, unloaded plan on ``ship`` as decide_trade_actions would."""
    distance = ship.route_distance(origin, destination)
    plan = TradePlan(
        origin=origin,
        destination=destination,
        commodity=commodity,
        quantity=quantity,
        bid_price_per_unit=10,
        purchase_price_per_unit=10,
        expected_sell_price_per_unit=30,
        distance=distance,
        fuel_needed_one_way=ship.fuel_required(distance),
        fuel_price_at_origin=10,
        fuel_units_from_tank=0,
        fuel_price_from_tank=10,
    )
    ship.brain._current_plan = plan
    ship.brain._plan_loaded = False
    ship.brain._plan_turns_left = ACCUMULATION_PATIENCE
    return plan


def test_accumulating_plan_still_posts_a_standing_fuel_bid():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)

    ship = _make_ship(sim, a, fuel_milli=200, name="Trader")
    _accumulating_plan(ship, a, b, food)
    assert ship.fuel < ship.brain._fuel_reserve_need()

    ship.brain.decide_trade_actions()

    # Exactly one fuel bid: the upkeep owns the fuel side of the book, and
    # the plan's own fuel step stands down rather than double-buying.
    fuel_buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(fuel_buys) == 1
    assert fuel_buys[0].quantity > 0
    # The plan is still being worked: its cargo bid went in too.
    assert [o for o in a.market.buy_orders[food] if o.actor is ship]


def test_accumulating_plan_still_tops_up_from_a_local_ask():
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=200, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 100, 10)
    seller = _make_ship(sim, a, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)

    ship = _make_ship(sim, a, fuel_milli=200, name="Trader")
    _accumulating_plan(ship, a, b, food)

    ship.brain.decide_trade_actions()

    fuel_buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(fuel_buys) == 1
    a.market.match_orders()
    ship.pump_fuel()
    assert ship.fuel >= ship.brain._fuel_reserve_need()


def test_distress_is_not_blocked_by_a_plan_that_never_loads():
    sim, _, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    # Poorer than one short round trip of fuel, which is the distress floor.
    ship = _make_ship(sim, a, fuel_milli=BASE_FUEL_CAPACITY, money=10, name="Trader")

    for _ in range(DISTRESS_PATIENCE):
        assert not ship.brain.is_distressed
        _accumulating_plan(ship, a, b, food)
        ship.brain.decide_trade_actions()

    assert ship.brain.is_distressed


# ---------------------------------------------------------------------------
# is_stranded
# ---------------------------------------------------------------------------


def test_is_stranded_true_when_docked_out_of_fuel_and_no_ask():
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, fuel_units=0, name="Trader")

    assert ship.brain.is_stranded()


def test_is_stranded_false_once_fuel_exceeds_reserve():
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, fuel_units=0, name="Trader")
    reserve = ship.brain._fuel_reserve_need()
    ship.fuel += reserve + 1

    assert not ship.brain.is_stranded()


# ---------------------------------------------------------------------------
# The arrival floor is waived only for a market that is stocked *and* restocked
# ---------------------------------------------------------------------------


def _make_working_fuel_market(sim, planet, fuel, resting=50, price=10):
    """Give ``planet`` both fuel signals: recent trades and a live resting ask."""
    supplier = _make_ship(sim, planet, hold_fuel=500, name=f"Supplier{planet.name}")
    buyer = _make_ship(sim, planet, money=9000, name=f"Buyer{planet.name}")
    _drain_fuel_ask(planet, fuel, supplier, buyer, price=price)
    planet.market.place_sell_order(supplier, fuel, resting, price)
    return supplier


def test_arrival_floor_waived_only_at_a_fuel_station():
    """Only station depth waives the escape floor; weaker signals do not.

    Requiring the escape leg even of a fuel seller is self-ratcheting: a ship
    holding exactly its escape leg can never spend it, because the hop to the
    seller demands that seller's escape leg on arrival. A station is depth
    enough to refill a tank at a sane price, which is what makes the waiver
    safe; a resting ask or a recent trade is not.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    buyer_b = _make_ship(sim, b, money=9000, name="BuyerB")
    ship = _make_ship(sim, a, name="Trader")
    _give_repair_kit(ship)
    escape_cost = ship.fuel_required(ship.route_distance(a, b))
    nav = ship.brain._nav

    # A is a station, so the escape leg out of B is escape_cost.
    a.market.place_sell_order(supplier_a, fuel, 50, 10)

    # A trickle ask at B: liftable, but not a tank's worth. Floor stands.
    b.market.place_sell_order(supplier_b, fuel, 1, 10)
    nav.refresh_market_facts()
    assert nav.fuel_purchasable_at(b) and not nav.fuel_station_at(b)
    assert ship.brain._arrival_fuel_requirement(b, a) == escape_cost

    # Recent trades at B, but the ask that made them is gone: floor stands.
    b.market.place_buy_order(buyer_b, fuel, 1, 10)
    b.market.match_orders()
    nav.refresh_market_facts()
    assert nav.fuel_traded_recently(b) and not nav.fuel_purchasable_at(b)
    assert ship.brain._arrival_fuel_requirement(b, a) == escape_cost

    # Station depth at a sane price: the floor is waived.
    b.market.place_sell_order(supplier_b, fuel, 50, 10)
    nav.refresh_market_facts()
    assert nav.fuel_station_at(b)
    assert ship.brain._arrival_fuel_requirement(b, a) == 0
    assert ship.brain._fuel_safe_destination(b, a, 0)


def _fuel_desert_world(sim_specs=(("D", 0, 0), ("C", 50, 0), ("F", 300, 0))):
    """A desert, a near candidate, and a distant working fuel market."""
    sim, fuel, _, (desert, near, far) = _make_world(list(sim_specs))
    _make_working_fuel_market(sim, far, fuel)
    ship = _make_ship(sim, desert, name="Idler")
    leg = ship.fuel_required(ship.route_distance(desert, near))
    # Only the near planet is in range; the ship would land there dry.
    assert leg < ship.fuel_required(ship.route_distance(desert, far))
    return sim, fuel, desert, near, ship, leg


def test_survival_reposition_drops_unsafe_history_only_candidate():
    """An unsafe last-resort hop is refused when only history vouches for it.

    Landing under the arrival floor on a planet with nothing to lift is a
    sink; ships that took those hops sat for hundreds of turns.
    """
    sim, fuel, desert, near, ship, leg = _fuel_desert_world()
    supplier = _make_ship(sim, near, hold_fuel=500, name="SupplierC")
    buyer = _make_ship(sim, near, money=9000, name="BuyerC")
    _drain_fuel_ask(near, fuel, supplier, buyer)
    ship.brain._nav.refresh_market_facts()

    assert ship.brain._nav.fuel_traded_recently(near)
    assert not ship.brain._nav.fuel_purchasable_at(near)
    assert not ship.brain._fuel_safe_destination(near, desert, 0)
    assert ship.brain._survival_reposition_target(leg) is None


def test_survival_reposition_accepts_unsafe_live_ask_candidate():
    """The same unsafe hop is worth taking when there is an ask to land against."""
    sim, fuel, desert, near, ship, leg = _fuel_desert_world()
    supplier = _make_ship(sim, near, hold_fuel=500, name="SupplierC")
    near.market.place_sell_order(supplier, fuel, 1, 10)
    ship.brain._nav.refresh_market_facts()

    assert ship.brain._nav.fuel_purchasable_at(near)
    assert not ship.brain._nav.fuel_traded_recently(near)
    assert not ship.brain._fuel_safe_destination(near, desert, 0)
    assert ship.brain._survival_reposition_target(leg) is near


def test_rationed_escape_hop_clears_the_departure_gate():
    """A ship rationed at a spiked ask can actually fly to the fuel seller.

    End to end: spiked ask at the origin, a working fuel market one hop away,
    so the escape target is the bare leg and the arrival gate approves it.
    """
    sim, fuel, _, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    _make_working_fuel_market(sim, b, fuel, resting=100, price=10)
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    a.market.place_sell_order(local_supplier, fuel, 100, 60)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    ship.brain._nav.refresh_market_facts()
    leg = ship.fuel_required(ship.route_distance(a, b))

    assert ship.brain._arrival_fuel_requirement(b, a) == 0
    escape_target = ship.brain._fuel_escape_target()
    assert escape_target == leg

    ship.brain.decide_trade_actions()
    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1 and buys[0].quantity == milli_to_units_ceil(escape_target)

    a.market.match_orders()
    ship.pump_fuel()
    on_hand = ship.fuel
    # The book sells whole units, so the ration lands on the unit covering
    # the escape leg.
    assert on_hand == units_to_milli(milli_to_units_ceil(escape_target))
    assert on_hand >= leg
    assert on_hand >= ship.brain._departure_fuel_requirement(b)
    assert ship.brain._fuel_safe_destination(b, a, on_hand - leg)


def test_scarcity_gate_ignores_a_one_credit_galaxy_minimum():
    """A 1-credit probe ask elsewhere does not open the sell gate.

    The gate anchors on the median believable valuation, not the galaxy
    minimum ask. Anchored on the minimum, one ship's 1-credit remainder ask
    made every local bid look scarcity-priced and the whole fleet sold its
    tanks at the launch window's low prices.
    """
    specs = [(f"P{i}", 100 * i, 0) for i in range(4)]
    sim, fuel, _, planets = _make_world(specs)
    home = planets[0]
    # Fuel is worth 40 across the galaxy: believable asks with real depth.
    for planet in planets[1:3]:
        supplier = _make_ship(sim, planet, hold_fuel=200, name=f"S{planet.name}")
        planet.market.place_sell_order(supplier, fuel, 100, 40)
    # One single-unit ask at 1 credit pins the galaxy minimum.
    probe = _make_ship(sim, planets[3], hold_fuel=5, name="Probe")
    planets[3].market.place_sell_order(probe, fuel, 1, 1)
    # An ordinary local bid, far below what fuel is worth.
    buyer = _make_ship(sim, home, money=2000, name="Buyer")
    home.market.place_buy_order(buyer, fuel, 20, 10)

    ship = _make_ship(sim, home, fuel_milli=BASE_FUEL_CAPACITY, name="Trader")
    brain = ship.brain
    brain._nav.refresh_market_facts()

    assert brain._nav.cheapest_fuel_ask() == 1
    assert brain._fuel_value_reference() == 40

    brain.decide_trade_actions()
    assert not [o for o in home.market.sell_orders[fuel] if o.actor is ship]


def test_fuel_remainder_never_rests_below_replacement_cost():
    """Surplus fuel on a market that has never traded it rests at the reference.

    Without the floor the remainder rested at ``max(1, best_bid or avg)``,
    which is 1 credit on a planet with no fuel book at all.
    """
    specs = [(f"P{i}", 100 * i, 0) for i in range(3)]
    sim, fuel, _, planets = _make_world(specs)
    home = planets[0]
    for planet in planets[1:]:
        supplier = _make_ship(sim, planet, hold_fuel=200, name=f"S{planet.name}")
        planet.market.place_sell_order(supplier, fuel, 100, 40)

    ship = _make_ship(
        sim, home, fuel_milli=BASE_FUEL_CAPACITY, hold_fuel=10, name="Trader"
    )
    brain = ship.brain
    brain._nav.refresh_market_facts()
    assert brain._fuel_value_reference() == 40

    actions = brain._place_flow_sell_orders(home.market, fuel, 10)

    assert actions
    sells = [o for o in home.market.sell_orders[fuel] if o.actor is ship]
    assert sum(o.quantity for o in sells) == 10
    assert all(o.price >= 40 for o in sells)


def test_fuel_remainder_floor_falls_back_before_any_fuel_trades():
    """With no believable valuation anywhere the floor is the fallback price."""
    sim, fuel, _, (a, _) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(
        sim, a, fuel_milli=BASE_FUEL_CAPACITY, hold_fuel=10, name="Trader"
    )
    brain = ship.brain
    brain._nav.refresh_market_facts()
    assert brain._fuel_value_reference() is None

    brain._place_flow_sell_orders(a.market, fuel, 10)

    sells = [o for o in a.market.sell_orders[fuel] if o.actor is ship]
    assert [o.price for o in sells] == [FUEL_BID_FALLBACK_FLOOR]
