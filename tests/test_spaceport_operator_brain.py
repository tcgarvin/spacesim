"""Tests for SpaceportOperatorBrain and the navigator fuel-price move.

The operator is the standing fuel counterparty on a planet: it lifts cheap
local asks, rests delivery-viable bids where there is no ask, and resells at
its own cost basis plus a spread, gated by the condition of its spaceport.
These tests pin each of those rules, the self-trade guard, the setup wiring,
and that the delivery-bid price moved to the navigator without changing.
"""

import math

from spacesim.core.actor import Actor, ActorType
from spacesim.core.brains import dealer
from spacesim.core.brains.spaceport_operator import (
    CONDITION_ASK_FLOOR,
    INVENTORY_SKEW_CAP,
    SpaceportOperatorBrain,
)
from spacesim.core.commands import PlaceBuyOrderCommand, PlaceSellOrderCommand
from spacesim.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim.core.drives.facility_upkeep_drive import FacilityUpkeepDrive
from spacesim.core.facility import FacilityDefinition, FailureType, UpkeepSpec
from spacesim.core.galaxy import StarLaneNetwork
from spacesim.core.market import Market
from spacesim.core.navigation import get_navigator
from spacesim.core.planet import Planet
from spacesim.core.ship import FUEL_MILLI, Ship, fuel_capacity_for, milli_to_units_ceil
from spacesim.core.simulation import Simulation

_COMMODITIES = [
    ("nova_fuel", "NovaFuel"),
    ("spaceport", "Spaceport"),
    ("simple_building_materials", "Simple Building Materials"),
    ("common_metal", "Common Metal"),
]

_FACILITY = FacilityDefinition(
    id="spaceport",
    upkeep=UpkeepSpec(
        event_probability=0.01,
        failures=(
            FailureType(
                name="structural",
                weight=0.6,
                material_id="simple_building_materials",
                quantity=3,
            ),
            FailureType(
                name="mechanical",
                weight=0.25,
                material_id="common_metal",
                quantity=1,
            ),
        ),
    ),
)


def _make_world(planet_specs=(("A", 0, 0), ("B", 200, 0))):
    """Build a registry, planets joined by lanes, and a mock simulation."""
    registry = CommodityRegistry()
    for commodity_id, name in _COMMODITIES:
        registry.add_commodity(
            CommodityDefinition(
                id=commodity_id,
                name=name,
                transportable=commodity_id != "spaceport",
                description=name,
            )
        )
    planets = [Planet(name, Market(), x, y) for name, x, y in planet_specs]
    for planet in planets:
        planet.market.commodity_registry = registry
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


def _make_operator(sim, planet, money=5000, fuel_units=0):
    """An operator actor on ``planet``, with a spaceport and an upkeep drive."""
    registry = sim.commodity_registry
    drive = FacilityUpkeepDrive(registry, _FACILITY)
    actor = Actor(
        name="Operator",
        sim=sim,
        actor_type=ActorType.SERVICE,
        drives=[drive],
        brain=SpaceportOperatorBrain(),
        planet=planet,
        initial_money=money,
    )
    actor.inventory.add_commodity(registry.get_commodity("spaceport"), 1)
    if fuel_units:
        actor.inventory.add_commodity(registry.get_commodity("nova_fuel"), fuel_units)
    planet.add_actor(actor)
    return actor, drive


def _seller(sim, planet, commodity, quantity, price, money=0):
    """A bare actor resting an ask, so the operator has something to lift."""
    actor = Actor(
        name=f"Seller-{commodity.id}",
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[],
        brain=SpaceportOperatorBrain(),
        planet=planet,
        initial_money=money,
    )
    actor.inventory.add_commodity(commodity, quantity)
    planet.add_actor(actor)
    planet.market.place_sell_order(actor, commodity, quantity, price)
    return actor


def _dealer_seller(sim, planet, commodity, quantity, price):
    """A SERVICE actor resting an ask, i.e. another dealer's quote."""
    actor = Actor(
        name=f"Dealer-{planet.name}",
        sim=sim,
        actor_type=ActorType.SERVICE,
        drives=[],
        brain=SpaceportOperatorBrain(),
        planet=planet,
        initial_money=0,
    )
    actor.inventory.add_commodity(commodity, quantity)
    planet.add_actor(actor)
    planet.market.place_sell_order(actor, commodity, quantity, price)
    return actor


def _stock_target(sim, planet):
    """The operator's fuel stock target: recent flow, floored at one tank.

    ``fuel_capacity_for`` is millifuel; the stock floor is market units, so a
    tank is about six units and the no-history neutral target dominates it.
    """
    navigator = get_navigator(sim)
    one_tank = milli_to_units_ceil(
        fuel_capacity_for(navigator.mean_pair_distance(), 1.0)
    )
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    return dealer.flow_stock_target(planet.market, fuel, floor=one_tank)


def _quote(sim, actor):
    """Refresh galaxy fuel facts, then ask the brain for this turn's orders."""
    get_navigator(sim).refresh_market_facts()
    return actor.brain.decide_market_actions(actor)


def _orders(commands, command_class, commodity_id):
    return [
        c
        for c in commands
        if isinstance(c, command_class) and c.commodity_type.id == commodity_id
    ]


# ---------------------------------------------------------------------------
# Fuel bid
# ---------------------------------------------------------------------------


def test_lifts_a_cheap_local_ask_up_to_the_stock_target():
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    # An expensive offworld source sets import parity well above the local ask.
    _seller(sim, other, fuel, 500, 60)
    _seller(sim, planet, fuel, 500, 20)
    actor, _ = _make_operator(sim, planet, money=5000)

    commands = _quote(sim, actor)
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    assert len(bids) == 1
    target = _stock_target(sim, planet)
    budget = int(5000 * SpaceportOperatorBrain.BUY_CAPITAL_FRACTION)
    assert bids[0].price == 20  # the ask is lifted at its own price
    assert bids[0].quantity == min(target, budget // 20)


def test_rests_a_delivery_priced_bid_when_the_local_ask_is_too_dear():
    """A local ask above import parity is passed over, but a bid still rests."""
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    # Importing from `other` is cheaper than the local spike, so the operator
    # bids at the delivered price rather than lifting the spike or sitting out.
    _seller(sim, other, fuel, 500, 10)
    _seller(sim, planet, fuel, 100, 500)
    actor, _ = _make_operator(sim, planet, money=5000)

    commands = _quote(sim, actor)
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    navigator = get_navigator(sim)
    target = _stock_target(sim, planet)
    delivered = navigator.fuel_delivery_bid_price(planet, target)
    assert delivered < 500
    assert len(bids) == 1
    # The delivered price is an arbitrage ceiling: the empty-stock skew would
    # raise the bid above it, and is clipped.
    assert bids[0].price == delivered
    assert delivered < dealer.skew_midpoint(delivered, 0, target, INVENTORY_SKEW_CAP)


def test_lifts_a_local_ask_at_or_below_the_delivered_price():
    """Buying at home beats paying for a delivery, so the ask is lifted."""
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, other, fuel, 500, 30)
    actor, _ = _make_operator(sim, planet, money=5000)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()
    target = _stock_target(sim, planet)
    delivered = navigator.fuel_delivery_bid_price(planet, target)

    _seller(sim, planet, fuel, 500, delivered)
    commands = _quote(sim, actor)
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    assert len(bids) == 1
    assert bids[0].price == delivered


def test_rests_a_delivery_priced_bid_when_there_is_no_ask():
    sim, registry, (planet, _) = _make_world()
    actor, _ = _make_operator(sim, planet, money=5000)
    navigator = get_navigator(sim)
    target = _stock_target(sim, planet)

    commands = _quote(sim, actor)
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    assert len(bids) == 1
    # Never above the delivered price, whatever the inventory skew wants.
    delivered = navigator.fuel_delivery_bid_price(planet, target)
    assert bids[0].price == delivered
    assert bids[0].quantity == target


def test_bid_is_never_skewed_above_the_delivered_price():
    """Low stock may not bid over what importing the fuel would cost.

    The skew used to be free to add up to 50%. Operators bid each other's
    prices up through it: every dealer's ask fed the next dealer's anchor.
    """
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, other, fuel, 500, 30)
    actor, _ = _make_operator(sim, planet, money=100000)
    navigator = get_navigator(sim)
    target = _stock_target(sim, planet)

    commands = _quote(sim, actor)
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    delivered = navigator.fuel_delivery_bid_price(planet, target)
    assert len(bids) == 1
    assert bids[0].price == delivered
    assert dealer.skew_midpoint(delivered, 0, target, INVENTORY_SKEW_CAP) > delivered


# ---------------------------------------------------------------------------
# Fuel ask
# ---------------------------------------------------------------------------


def test_ask_prices_off_restock_cost_and_scales_quantity_with_condition():
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    # Stock sits under the target, so no over-target skew walks the ask down.
    actor, drive = _make_operator(sim, planet, money=5000, fuel_units=20)
    # Bought during a spike: the basis is 60, but restocking today is cheap.
    actor.brain._fuel_units = 20
    actor.brain._fuel_total_cost = 1200.0
    _seller(sim, other, fuel, 500, 10)

    drive.metrics.health = 0.5
    commands = _quote(sim, actor)
    asks = _orders(commands, PlaceSellOrderCommand, "nova_fuel")

    navigator = get_navigator(sim)
    target = _stock_target(sim, planet)
    restock = float(navigator.fuel_delivery_bid_price(planet, target))
    spread = actor.brain.spread
    assert len(asks) == 1
    assert asks[0].price == max(
        math.ceil(restock) + 1, math.ceil(restock * (1.0 + spread))
    )
    # The spike-priced stock is sold at a loss rather than frozen above market.
    assert asks[0].price < 60
    assert asks[0].quantity == 10  # floor(20 * 0.5)


def test_ask_follows_a_cheaper_local_producer_ask_down():
    """Restock cost is the cheaper of a local ask and an import, not history."""
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    actor, _ = _make_operator(sim, planet, money=5000, fuel_units=20)
    actor.brain._fuel_units = 20
    actor.brain._fuel_total_cost = 1600.0  # basis 80
    _seller(sim, other, fuel, 500, 40)
    _seller(sim, planet, fuel, 500, 8)

    commands = _quote(sim, actor)
    asks = _orders(commands, PlaceSellOrderCommand, "nova_fuel")
    bids = _orders(commands, PlaceBuyOrderCommand, "nova_fuel")

    spread = actor.brain.spread
    assert len(asks) == 1
    assert asks[0].price == max(9, math.ceil(8 * (1.0 + spread)))
    # The self-trade guard is not needed here, but the invariant it protects
    # holds anyway: what we bid stays under what we ask.
    assert bids and bids[0].price < asks[0].price


def test_no_ask_below_the_condition_floor():
    sim, registry, (planet, _) = _make_world()
    actor, drive = _make_operator(sim, planet, money=200, fuel_units=30)
    actor.brain._fuel_units = 30
    actor.brain._fuel_total_cost = 600.0

    drive.metrics.health = CONDITION_ASK_FLOOR - 0.01
    commands = _quote(sim, actor)

    assert _orders(commands, PlaceSellOrderCommand, "nova_fuel") == []


# ---------------------------------------------------------------------------
# Upkeep and the self-trade guard
# ---------------------------------------------------------------------------


def test_upkeep_is_bought_before_fuel_and_targets_the_last_unmet_material():
    sim, registry, (planet, _) = _make_world()
    metal = registry.get_commodity("common_metal")
    materials = registry.get_commodity("simple_building_materials")
    actor, drive = _make_operator(sim, planet, money=300)
    # The last event the port could not cover called for common_metal.
    drive.last_unmet = _FACILITY.upkeep.failures[1]

    commands = _quote(sim, actor)
    buys = [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]
    ids = [c.commodity_type.id for c in buys]

    assert ids.index(materials.id) < ids.index("nova_fuel")
    assert ids.index(metal.id) < ids.index("nova_fuel")
    building_buy = _orders(commands, PlaceBuyOrderCommand, materials.id)[0]
    assert building_buy.quantity == drive.target_units()
    assert _orders(commands, PlaceBuyOrderCommand, metal.id)[0].quantity == 1


def test_self_trade_guard_drops_a_bid_at_or_above_our_own_ask(monkeypatch):
    """The guard still fires, even though restock pricing rarely needs it.

    Restock-cost asks sit a spread above the price the bid is capped at, so
    the two sides no longer cross on their own. The guard remains because a
    crossed quote would churn inventory for a guaranteed loss.
    """
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, other, fuel, 500, 40)
    actor, _ = _make_operator(sim, planet, money=5000, fuel_units=10)

    cheap_ask = PlaceSellOrderCommand(fuel, 10, 2)
    monkeypatch.setattr(
        SpaceportOperatorBrain, "_fuel_ask", lambda *args, **kwargs: cheap_ask
    )
    commands = _quote(sim, actor)

    assert _orders(commands, PlaceSellOrderCommand, "nova_fuel") == [cheap_ask]
    assert _orders(commands, PlaceBuyOrderCommand, "nova_fuel") == []


def test_no_spaceport_means_no_quotes():
    sim, registry, (planet, _) = _make_world()
    actor, _ = _make_operator(sim, planet, money=5000)
    actor.inventory.remove_commodity(registry.get_commodity("spaceport"), 1)

    commands = _quote(sim, actor)

    assert commands == []
    assert "no spaceport" in actor.last_action


# ---------------------------------------------------------------------------
# Setup and smoke
# ---------------------------------------------------------------------------


def test_setup_creates_two_operators_per_planet_with_a_spaceport():
    sim = Simulation()
    sim.setup_simple(
        num_planets=2, num_regular_actors=4, num_market_makers=1, num_ships=1
    )
    spaceport = sim.commodity_registry.get_commodity("spaceport")

    operators = [a for a in sim.actors if isinstance(a.brain, SpaceportOperatorBrain)]
    assert len(operators) == 4
    for planet in sim.planets:
        on_planet = [a for a in operators if a.planet is planet]
        assert len(on_planet) == 2

    for operator in operators:
        assert operator.actor_type == ActorType.SERVICE
        assert operator.money == 50, "operators get no capital injection"
        assert operator.inventory.get_quantity(spaceport) == 1
        names = [d.metrics.get_name() for d in operator.drives]
        assert names == ["facility_upkeep"]


def test_sixty_turn_smoke_leaves_an_operator_in_the_fuel_market():
    sim = Simulation()
    sim.setup_simple(
        num_planets=3, num_regular_actors=10, num_market_makers=1, num_ships=2
    )
    for _ in range(60):
        sim.run_turn()

    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    operators = [a for a in sim.actors if isinstance(a.brain, SpaceportOperatorBrain)]
    assert operators

    engaged = 0
    for operator in operators:
        assert operator.planet is not None
        orders = operator.planet.market.get_actor_orders(operator)
        has_order = any(
            order.commodity_type.id == "nova_fuel"
            for order in orders["buy"] + orders["sell"]
        )
        if operator.inventory.get_quantity(fuel) > 0 or has_order:
            engaged += 1
    assert engaged >= 1


# ---------------------------------------------------------------------------
# The navigator move
# ---------------------------------------------------------------------------


def _legacy_fuel_bid_price(navigator, planet, quantity):
    """The pre-move TraderBrain formula, inlined so the move can be checked."""
    from spacesim.core.navigation import (
        DELIVERER_WORST_FUEL_EFFICIENCY,
        FUEL_BID_MARGIN,
    )

    best = None
    for source, ask in navigator.fuel_ask_planets():
        if source is planet:
            continue
        leg_fuel_units = (
            math.ceil(
                Ship.calculate_fuel_needed(navigator.distance(source, planet))
                / DELIVERER_WORST_FUEL_EFFICIENCY
            )
            / FUEL_MILLI
        )
        delivered = ask + (2 * leg_fuel_units * ask) / max(quantity, 1)
        if best is None or delivered < best:
            best = delivered
    if best is None:
        return navigator.local_fuel_reference_price(planet)
    return max(1, math.ceil(best * (1.0 + FUEL_BID_MARGIN)))


def test_navigator_delivery_price_matches_the_old_trader_formula():
    """The delivered-cost formula is unchanged below the fuel bid ceiling.

    Small orders amortize the round trip over few units, so their delivered
    price runs far above the ceiling and is capped there; the formula itself
    still governs everything under it.
    """
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, other, fuel, 100, 24)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    ceiling = navigator.fuel_bid_ceiling()
    for quantity in (1, 7, 50, 400):
        assert navigator.fuel_delivery_bid_price(planet, quantity) == min(
            _legacy_fuel_bid_price(navigator, planet, quantity), ceiling
        )


def test_navigator_local_reference_price_with_no_ask_anywhere():
    sim, _, (planet, _) = _make_world()
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    assert navigator.fuel_delivery_bid_price(
        planet, 10
    ) == navigator.local_fuel_reference_price(planet)


def test_trader_brain_delegates_fuel_pricing_to_the_navigator():
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, other, fuel, 100, 30)
    ship = Ship("Delegate", sim, planet, initial_money=1000)
    planet.add_ship(ship)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    assert ship.brain._fuel_bid_price(planet, 25) == navigator.fuel_delivery_bid_price(
        planet, 25
    )
    assert ship.brain._local_fuel_reference_price(
        planet
    ) == navigator.local_fuel_reference_price(planet)


def test_delivery_price_ignores_a_shallow_ask_it_cannot_fill():
    """A one-unit probe ask must not anchor the price of a 40-unit order.

    Market makers post tiny discovery asks at a few credits. Anchoring on the
    cheapest ask regardless of depth priced a real delivery as if the whole
    load could be bought at the probe price.
    """
    sim, registry, (planet, near, far) = _make_world(
        (("A", 0, 0), ("Near", 100, 0), ("Far", 900, 0))
    )
    fuel = registry.get_commodity("nova_fuel")
    _seller(sim, far, fuel, 1, 2)
    _seller(sim, near, fuel, 60, 20)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    price = navigator.fuel_delivery_bid_price(planet, 40)

    assert price == _legacy_fuel_bid_price_from(navigator, planet, near, 20, 40)
    assert price > _legacy_fuel_bid_price_from(navigator, planet, far, 2, 40)


def test_delivery_price_ignores_a_dealer_ask():
    """Another dealer's ask is a markup on this same anchor, so it cannot set it.

    Anchoring on it closed a loop: the anchor set the bid, the bid became the
    price a dealer paid, its ask marked that up, and the next dealer anchored
    there. Fuel VWAP ratcheted from 14 to 94 in a hundred turns on it.
    """
    # Distances are short enough that neither delivered price reaches the
    # fuel bid ceiling, so this test still measures which source anchors.
    sim, registry, (planet, near, far) = _make_world(
        (("A", 0, 0), ("Near", 60, 0), ("Far", 160, 0))
    )
    fuel = registry.get_commodity("nova_fuel")
    _dealer_seller(sim, near, fuel, 500, 20)
    _seller(sim, far, fuel, 500, 20)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    price = navigator.fuel_delivery_bid_price(planet, 40)

    assert price == _legacy_fuel_bid_price_from(navigator, planet, far, 20, 40)
    assert price > _legacy_fuel_bid_price_from(navigator, planet, near, 20, 40)


def test_delivery_price_falls_back_to_dealer_asks_when_nobody_produces():
    """With only dealers selling, their asks are the only supply signal there is."""
    sim, registry, (planet, other) = _make_world()
    fuel = registry.get_commodity("nova_fuel")
    _dealer_seller(sim, other, fuel, 500, 20)
    navigator = get_navigator(sim)
    navigator.refresh_market_facts()

    price = navigator.fuel_delivery_bid_price(planet, 40)

    assert price == _legacy_fuel_bid_price_from(navigator, planet, other, 20, 40)
    assert price > navigator.local_fuel_reference_price(planet)


def _legacy_fuel_bid_price_from(navigator, planet, source, ask, quantity):
    """Delivered bid price anchored on one named source, for comparison."""
    from spacesim.core.navigation import (
        DELIVERER_WORST_FUEL_EFFICIENCY,
        FUEL_BID_MARGIN,
    )

    leg_fuel_units = (
        math.ceil(
            Ship.calculate_fuel_needed(navigator.distance(source, planet))
            / DELIVERER_WORST_FUEL_EFFICIENCY
        )
        / FUEL_MILLI
    )
    delivered = ask + (2 * leg_fuel_units * ask) / max(quantity, 1)
    return max(1, math.ceil(delivered * (1.0 + FUEL_BID_MARGIN)))
