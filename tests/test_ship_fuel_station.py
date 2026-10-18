"""Tests for fuel stations and the maintenance fuel buffer.

- A fuel station is depth a ship can refill a tank from at a price near the
  galaxy reference. A one-unit trickle ask and a deep book at a panic price
  are both purchasable and neither is a station.
- The arrival reserve at a non-station destination carries the fuel a
  maintenance roll could burn, unless the ship holds a repair kit.
- A ship buys a kit proactively when it costs less than the fuel it saves,
  and never treats that kit as trade cargo.
"""

from spacesim.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim.core.galaxy import StarLaneNetwork
from spacesim.core.market import Market
from spacesim.core.navigation import FUEL_STATION_MIN_DEPTH
from spacesim.core.planet import Planet
from spacesim.core.ship import MAINTENANCE_FUEL_MILLI, Ship

MAINTENANCE_GOODS = (
    ("ship_components", "Ship Components"),
    ("ship_parts", "Ship Parts"),
    ("ship_supplies", "Ship Supplies"),
)


def _make_world(planet_specs):
    """Build a fuel, food and maintenance-goods registry with planets and a mock sim."""
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
    for commodity_id, name in MAINTENANCE_GOODS:
        registry.add_commodity(
            CommodityDefinition(
                id=commodity_id,
                name=name,
                transportable=True,
                description="Maintenance tier.",
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
    ship = Ship(name, sim, planet, initial_money=money)
    planet.add_ship(ship)
    ship.fuel = fuel_milli
    if hold_fuel:
        ship.cargo.add_commodity(
            sim.commodity_registry.get_commodity("nova_fuel"), hold_fuel
        )
    ship.check_maintenance = lambda: False  # deterministic departures
    return ship


# ---------------------------------------------------------------------------
# Station classification
# ---------------------------------------------------------------------------


def test_trickle_ask_is_not_a_station():
    """One unit resting is purchasable but cannot refill a tank."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    fuel = registry.get_commodity("nova_fuel")
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    nav = _make_ship(sim, b, name="Observer").brain._nav

    a.market.place_sell_order(supplier, fuel, 1, 10)
    nav.refresh_market_facts()

    assert nav.fuel_purchasable_at(a)
    assert not nav.fuel_station_at(a)
    assert nav.fuel_station_planets() == []
    assert nav.nearest_fuel_station_distance(b) is None


def test_station_depth_at_a_sane_price_is_a_station():
    """Enough depth to refill a tank, priced near the reference, qualifies."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    fuel = registry.get_commodity("nova_fuel")
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    nav = _make_ship(sim, b, name="Observer").brain._nav

    a.market.place_sell_order(supplier, fuel, FUEL_STATION_MIN_DEPTH, 10)
    nav.refresh_market_facts()

    assert nav.fuel_station_at(a)
    assert nav.fuel_station_planets() == [a]
    assert nav.nearest_fuel_station_distance(b) == 100.0
    assert nav.nearest_fuel_station_distance(a) is None


def test_depth_at_a_spiked_price_is_not_a_station():
    """A deep book at three times the galaxy reference is not somewhere to refuel."""
    sim, registry, (a, b, c) = _make_world([("A", 0, 0), ("B", 100, 0), ("C", 200, 0)])
    fuel = registry.get_commodity("nova_fuel")
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    supplier_c = _make_ship(sim, c, hold_fuel=500, name="SupplierC")
    nav = supplier_a.brain._nav

    # Two planets at 10 set the reference; C asks 30 for the same depth.
    a.market.place_sell_order(supplier_a, fuel, 50, 10)
    b.market.place_sell_order(supplier_b, fuel, 50, 10)
    c.market.place_sell_order(supplier_c, fuel, 50, 30)
    nav.refresh_market_facts()

    assert nav.fuel_value_reference() == 10.0
    assert nav.fuel_purchasable_at(c)
    assert not nav.fuel_station_at(c)
    assert nav.fuel_station_planets() == [a, b]


# ---------------------------------------------------------------------------
# Arrival reserve and the maintenance buffer
# ---------------------------------------------------------------------------


def test_arrival_requirement_carries_the_maintenance_buffer_without_a_kit():
    """A non-station destination costs the escape leg plus a possible repair."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    fuel = registry.get_commodity("nova_fuel")
    components = registry.get_commodity("ship_components")
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 50, 10)
    ship = _make_ship(sim, a, name="Trader")
    ship.brain._nav.refresh_market_facts()

    escape_leg = ship.fuel_required(ship.route_distance(b, a))
    assert (
        ship.brain._arrival_fuel_requirement(b, a)
        == escape_leg + MAINTENANCE_FUEL_MILLI
    )

    ship.cargo.add_commodity(components, 1)
    assert ship.brain._holds_repair_kit()
    assert ship.brain._maintenance_fuel_buffer() == 0
    assert ship.brain._arrival_fuel_requirement(b, a) == escape_leg


def test_a_partial_tier_is_not_a_repair_kit():
    """Two of the three ship_supplies a repair takes buy no buffer relief."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplies = registry.get_commodity("ship_supplies")
    ship = _make_ship(sim, a, name="Trader")

    ship.cargo.add_commodity(supplies, 2)
    assert not ship.brain._holds_repair_kit()
    assert ship.brain._maintenance_fuel_buffer() == MAINTENANCE_FUEL_MILLI

    ship.cargo.add_commodity(supplies, 1)
    assert ship.brain._holds_repair_kit()
    assert ship.brain._maintenance_fuel_buffer() == 0


# ---------------------------------------------------------------------------
# Proactive kit purchase
# ---------------------------------------------------------------------------


def _fuel_reference_world():
    """A two-planet world whose galaxy fuel reference is 10."""
    sim, registry, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    fuel = registry.get_commodity("nova_fuel")
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 50, 10)
    return sim, registry, a, b, supplier


def test_kit_bought_when_it_costs_less_than_the_fuel_it_saves():
    """A cheap complete tier is lifted at its ask, one tier per turn."""
    sim, registry, a, _b, supplier = _fuel_reference_world()
    supplies = registry.get_commodity("ship_supplies")
    supplier.cargo.add_commodity(supplies, 10)
    # Three units at 1 is 3, under 0.5 fuel at the reference of 10.
    a.market.place_sell_order(supplier, supplies, 10, 1)
    ship = _make_ship(sim, a, money=5000, name="Trader")
    ship.brain._nav.refresh_market_facts()

    actions = ship.brain._buy_repair_kit()

    assert len(actions) == 1
    orders = a.market.get_actor_orders(ship)["buy"]
    assert [(o.commodity_type.id, o.quantity, o.price) for o in orders] == [
        ("ship_supplies", 3, 1)
    ]


def test_kit_skipped_when_it_costs_more_than_the_fuel_it_saves():
    """Above the fuel it would save, the kit is not worth the money."""
    sim, registry, a, _b, supplier = _fuel_reference_world()
    supplies = registry.get_commodity("ship_supplies")
    supplier.cargo.add_commodity(supplies, 10)
    # Three units at 2 is 6, above 0.5 fuel at the reference of 10.
    a.market.place_sell_order(supplier, supplies, 10, 2)
    ship = _make_ship(sim, a, money=5000, name="Trader")
    ship.brain._nav.refresh_market_facts()

    assert ship.brain._buy_repair_kit() == []
    assert a.market.get_actor_orders(ship)["buy"] == []


def test_kit_not_bought_twice():
    """A ship already holding a kit does not buy another."""
    sim, registry, a, _b, supplier = _fuel_reference_world()
    supplies = registry.get_commodity("ship_supplies")
    supplier.cargo.add_commodity(supplies, 10)
    a.market.place_sell_order(supplier, supplies, 10, 12)
    ship = _make_ship(sim, a, money=5000, name="Trader")
    ship.cargo.add_commodity(supplies, 3)
    ship.brain._nav.refresh_market_facts()

    assert ship.brain._buy_repair_kit() == []


def test_kit_purchase_respects_the_cash_buffer():
    """The buy keeps the same 10% operating buffer every other buy keeps."""
    sim, registry, a, _b, supplier = _fuel_reference_world()
    supplies = registry.get_commodity("ship_supplies")
    supplier.cargo.add_commodity(supplies, 10)
    a.market.place_sell_order(supplier, supplies, 10, 1)
    # 3 credits of kit against 2 spendable out of 3.
    ship = _make_ship(sim, a, money=3, name="Poor")
    ship.brain._nav.refresh_market_facts()
    assert ship.brain._buy_repair_kit() == []

    rich = _make_ship(sim, a, money=4, name="LessPoor")
    assert rich.brain._buy_repair_kit() != []


# ---------------------------------------------------------------------------
# Kit goods are equipment, not cargo
# ---------------------------------------------------------------------------


def test_kit_units_are_not_sellable_cargo():
    """One kit's worth is held back; anything above it still trades."""
    sim, registry, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplies = registry.get_commodity("ship_supplies")
    ship = _make_ship(sim, a, name="Trader")

    ship.cargo.add_commodity(supplies, 3)
    assert ship.brain._sellable_quantity(supplies) == 0

    ship.cargo.add_commodity(supplies, 2)
    assert ship.brain._sellable_quantity(supplies) == 2


def test_only_the_tier_the_ship_repairs_with_is_reserved():
    """A second tier carried as cargo is sellable; the best one is not."""
    sim, registry, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    components = registry.get_commodity("ship_components")
    supplies = registry.get_commodity("ship_supplies")
    ship = _make_ship(sim, a, name="Trader")

    ship.cargo.add_commodity(components, 1)
    ship.cargo.add_commodity(supplies, 3)

    assert ship.brain._sellable_quantity(components) == 0
    assert ship.brain._sellable_quantity(supplies) == 3
