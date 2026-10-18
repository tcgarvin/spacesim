"""Tests for fuel price discipline and the refuelling reposition (R3, R4).

- Every fuel bid a ship posts is capped at the galaxy fuel bid ceiling, twice
  the reference. Sellers fill a resting bid at the bid price, so an
  uncapped bid buys its own escalation.
- A ship that can fly to a fuel station posts no bid and buys nothing at a
  spiked ask; it flies there instead and refuels at the reference. Only a
  trapped ship still buys, and it buys the rationed minimum.
- Plan funding covers the outbound leg plus the arrival reserve the
  destination demands, which at a non-station destination can exceed the
  round trip.
"""

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.galaxy import StarLaneNetwork
from spacesim2.core.market import Market
from spacesim2.core.navigation import FUEL_STATION_MIN_DEPTH, get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.ship import (
    MAINTENANCE_FUEL_MILLI,
    Ship,
    milli_to_units_ceil,
)

STATION_DEPTH = max(FUEL_STATION_MIN_DEPTH, 50)


def _make_world(planet_specs):
    """Build a fuel and food registry with planets and a mock simulation."""
    registry = CommodityRegistry()
    registry.add_commodity(
        CommodityDefinition(
            id="nova_fuel",
            name="NovaFuel",
            transportable=True,
            description="High-density energy source for starship travel.",
        )
    )
    registry.add_commodity(
        CommodityDefinition(
            id="food", name="Food", transportable=True, description="Basic sustenance."
        )
    )
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
    return sim, registry, planets


def _make_ship(sim, planet, fuel_milli=0, money=1000, name="TestShip", hold_fuel=0):
    """Dock a ship at ``planet`` with ``fuel_milli`` thousandths of a unit."""
    ship = Ship(name, sim, planet, initial_money=money, fuel_efficiency=1.0)
    planet.add_ship(ship)
    ship.fuel = fuel_milli
    if hold_fuel:
        ship.cargo.add_commodity(
            sim.commodity_registry.get_commodity("nova_fuel"), hold_fuel
        )
    ship.check_maintenance = lambda: False  # deterministic departures
    return ship


def _rest_fuel_ask(sim, planet, quantity, price, name):
    """Park a supplier at ``planet`` resting ``quantity`` fuel at ``price``."""
    supplier = _make_ship(sim, planet, hold_fuel=quantity + 10, name=name)
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    planet.market.place_sell_order(supplier, fuel, quantity, price)
    return supplier


def _spiked_world():
    """A(0) and B(100) are stations at 10; C(200) has deep fuel at 100.

    The galaxy reference is 10, so the bid ceiling is 20 and C's ask of 100
    is far outside the bunkering premium.
    """
    sim, registry, (a, b, c) = _make_world([("A", 0, 0), ("B", 100, 0), ("C", 200, 0)])
    _rest_fuel_ask(sim, a, STATION_DEPTH, 10, "SupplierA")
    _rest_fuel_ask(sim, b, STATION_DEPTH, 10, "SupplierB")
    _rest_fuel_ask(sim, c, STATION_DEPTH, 100, "SupplierC")
    return sim, registry, a, b, c


# ---------------------------------------------------------------------------
# R3: the bid ceiling
# ---------------------------------------------------------------------------


def test_standing_bid_is_capped_at_the_ceiling():
    """A trapped ship's rescue bid never rises above twice the reference."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    fuel = registry.get_commodity("nova_fuel")
    _rest_fuel_ask(sim, a, STATION_DEPTH, 10, "SupplierA")
    ship = _make_ship(sim, b, fuel_milli=0, money=1000, name="Trapped")
    nav = get_navigator(sim)
    nav.refresh_market_facts()
    ceiling = nav.fuel_bid_ceiling()

    assert ceiling == 20
    assert not ship.brain._can_reach_station()
    # Priced for the units this ship actually wants, the uncapped delivery
    # price is above the ceiling, so the cap binds.
    wanted = milli_to_units_ceil(ship.brain._fuel_survival_target())
    assert nav.fuel_delivery_bid_price(b, wanted) == ceiling

    order_id = ship.brain._post_standing_fuel_bid()

    assert order_id is not None
    order = b.market.orders_by_id[order_id]
    assert order.commodity_type is fuel
    assert order.price == ceiling


def test_no_standing_bid_when_a_station_is_reachable():
    """With fuel for the hop, the ship flies rather than bids."""
    sim, _registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    _rest_fuel_ask(sim, a, STATION_DEPTH, 10, "SupplierA")
    # Comfortably more than the 500 millifuel hop to the station at A.
    ship = _make_ship(sim, b, fuel_milli=800, money=1000, name="Mobile")
    get_navigator(sim).refresh_market_facts()

    assert ship.brain._can_reach_station()
    assert ship.brain._post_standing_fuel_bid() is None
    assert b.market.get_actor_orders(ship)["buy"] == []


def test_topup_buys_nothing_at_a_spike_when_a_station_is_reachable():
    """The reposition, not the book, is how this ship gets fuel."""
    sim, _registry, _a, b, c = _spiked_world()
    ship = _make_ship(sim, c, fuel_milli=600, money=5000, name="Mobile")
    get_navigator(sim).refresh_market_facts()

    assert b in ship.brain._reachable_stations()
    assert ship.brain._opportunistic_fuel_topup() is None
    assert c.market.get_actor_orders(ship)["buy"] == []


def test_trapped_topup_buys_the_rationed_minimum_at_the_ceiling():
    """No station in range: buy the escape units only, never above the cap."""
    sim, registry, _a, b, c = _spiked_world()
    fuel = registry.get_commodity("nova_fuel")
    ship = _make_ship(sim, c, fuel_milli=0, money=5000, name="Trapped")
    nav = get_navigator(sim)
    nav.refresh_market_facts()

    assert ship.brain._reachable_stations() == []
    order_id = ship.brain._opportunistic_fuel_topup()

    assert order_id is not None
    order = c.market.orders_by_id[order_id]
    assert order.commodity_type is fuel
    assert order.price == nav.fuel_bid_ceiling()
    # The escape target is the leg to the nearest station, which demands no
    # arrival reserve of its own.
    assert order.quantity == milli_to_units_ceil(
        ship.fuel_required(ship.route_distance(c, b))
    )


def test_local_reference_price_is_capped_at_the_ceiling():
    """Scarcity escalation stops at the ceiling instead of ratcheting."""
    sim, registry, _a, _b, c = _spiked_world()
    fuel = registry.get_commodity("nova_fuel")
    c.market.price_history[fuel].append(400)
    nav = get_navigator(sim)
    nav.refresh_market_facts()

    assert nav.local_fuel_reference_price(c) == nav.fuel_bid_ceiling()


# ---------------------------------------------------------------------------
# R4: the refuelling reposition
# ---------------------------------------------------------------------------


def test_empty_ship_repositions_to_the_nearest_station():
    """With no cargo to value, the cheapest leg wins."""
    sim, _registry, a, b, c = _spiked_world()
    # Enough for the 500 millifuel hop to B, short of the 1000 to A.
    ship = _make_ship(sim, c, fuel_milli=600, money=5000, name="Empty")
    get_navigator(sim).refresh_market_facts()

    assert set(ship.brain._reachable_stations()) >= {b}
    assert a not in ship.brain._reachable_stations()
    assert ship.brain._refuel_reposition_target() is b


def test_loaded_ship_repositions_to_the_station_that_pays_most():
    """Cargo value at the station, less the leg's fuel, picks the target."""
    sim, registry, a, b, c = _spiked_world()
    food = registry.get_commodity("food")
    buyer = _make_ship(sim, a, money=100000, name="Buyer")
    a.market.place_buy_order(buyer, food, 10, 100)
    thin = _make_ship(sim, b, money=100, name="ThinBuyer")
    b.market.place_buy_order(thin, food, 1, 1)

    # Enough for either station, so cargo value picks the target.
    ship = _make_ship(sim, c, fuel_milli=1200, money=5000, name="Loaded")
    ship.cargo.add_commodity(food, 10)
    get_navigator(sim).refresh_market_facts()

    assert set(ship.brain._reachable_stations()) == {a, b}
    assert ship.brain._refuel_reposition_target() is a


def test_no_reposition_from_a_station():
    """A ship already at depth waits or bunkers under the existing rules."""
    sim, _registry, a, _b, _c = _spiked_world()
    ship = _make_ship(sim, a, fuel_milli=100, money=5000, name="AtStation")
    get_navigator(sim).refresh_market_facts()

    assert ship.brain._refuel_reposition_target() is None


def test_no_reposition_when_local_fuel_is_cheap():
    """A cheap ask is worth buying, even where the book is thin."""
    sim, _registry, (a, b, d) = _make_world([("A", 0, 0), ("B", 100, 0), ("D", 200, 0)])
    _rest_fuel_ask(sim, a, STATION_DEPTH, 10, "SupplierA")
    _rest_fuel_ask(sim, b, STATION_DEPTH, 10, "SupplierB")
    _rest_fuel_ask(sim, d, 1, 10, "TrickleD")
    ship = _make_ship(sim, d, fuel_milli=600, money=5000, name="Cheap")
    nav = get_navigator(sim)
    nav.refresh_market_facts()

    assert not nav.fuel_station_at(d)  # one unit resting is no station
    assert ship.brain._can_reach_station()
    assert ship.brain._refuel_reposition_target() is None


def test_reposition_departs_without_listing_the_cargo():
    """The hold rides along; listing it would strand the money in the book."""
    sim, registry, a, b, c = _spiked_world()
    food = registry.get_commodity("food")
    buyer = _make_ship(sim, b, money=100000, name="Buyer")
    b.market.place_buy_order(buyer, food, 10, 50)
    ship = _make_ship(sim, c, fuel_milli=600, money=5000, name="Loaded")
    ship.cargo.add_commodity(food, 10)

    ship.brain.decide_trade_actions()

    assert ship.brain._refuel_reposition is b
    assert c.market.get_actor_orders(ship)["sell"] == []
    assert "Repositioning to B" in ship.last_action
    assert ship.brain.decide_travel() is b


# ---------------------------------------------------------------------------
# Plan funding
# ---------------------------------------------------------------------------


def test_plan_funds_the_leg_plus_the_arrival_reserve():
    """A non-station destination can demand more fuel than the round trip."""
    # D is 80 away, so the leg, its escape leg back, and the maintenance
    # buffer together still fit the tank. The buffer is five market units,
    # which at 200 distance a unit dwarfs every leg in this galaxy.
    sim, _registry, (a, b, d) = _make_world([("A", 0, 0), ("B", 100, 0), ("D", 0, 80)])
    _rest_fuel_ask(sim, a, STATION_DEPTH, 10, "SupplierA")
    _rest_fuel_ask(sim, b, STATION_DEPTH, 10, "SupplierB")
    ship = _make_ship(sim, a, fuel_milli=0, money=100000, name="Planner")
    nav = get_navigator(sim)
    nav.refresh_market_facts()

    leg = ship.fuel_required(ship.route_distance(a, d))
    escape = ship.fuel_required(nav.nearest_fuel_station_distance(d) or 0.0)
    arrival = escape + MAINTENANCE_FUEL_MILLI
    assert leg + arrival > 2 * leg  # the round trip alone would not fund it
    assert leg + arrival <= ship.fuel_capacity

    economics = ship.brain._pair_economics(a, d)

    assert economics is not None
    # The tank is millifuel; the book sells whole units.
    assert economics.fuel_to_buy == milli_to_units_ceil(leg + arrival)
