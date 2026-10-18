"""Tests for the ship brain's side of contracts.

Riders on a trip the ship was making anyway, trips flown for contract
payments alone, the early consignment load that funds a broke ship's fuel,
the pin a loaded payload puts on the destination, and the reposition toward
a planet whose only export is a queue of jobs.
"""

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.contracts import (
    GOVERNMENT,
    ConsignmentPayload,
    Contract,
    ContractStatus,
    PassengerPayload,
    load_contract,
)
from spacesim2.core.galaxy import StarLaneNetwork
from spacesim2.core.planet import Planet
from spacesim2.core.ship import (
    REFUEL_STOP_MAX_TURNS,
    REPOSITION_CONTRACT_PATIENCE,
    ContractPlan,
    Ship,
    TradePlan,
)
from tests.test_ship_fuel import _make_ship, _make_world


def _world(planet_specs, fuel_price=5, fuel_depth=200, chain=False):
    """A galaxy with a deep fuel ask on every planet and contract counters.

    ``chain`` links the planets in a line instead of the default complete
    graph, so the route from the first to the last passes through every
    planet in between.
    """
    sim, fuel, food, planets = _make_world(planet_specs)
    if chain:
        network = StarLaneNetwork()
        for previous, planet in zip(planets, planets[1:]):
            network.add_lane(previous, planet)
        sim.star_lanes = network
    sim.actors = []
    sim.contracts_delivered = 0
    sim.contracts_stranded = 0
    sim.government_payouts = 0
    sim.migration_departures = 0
    sim.passage_wait_turns = []
    sim.migration_log = []
    for index, planet in enumerate(planets):
        supplier = _make_ship(
            sim, planet, hold_fuel=fuel_depth, name=f"Supplier{index}"
        )
        planet.market.place_sell_order(supplier, fuel, fuel_depth, fuel_price)
    return sim, fuel, food, planets


def _consignment(
    sim,
    origin: Planet,
    destination: Planet,
    advance: int = 200,
    on_delivery: int = 0,
    units: int = 20,
) -> Contract:
    """Post a government freight job and return it."""
    contract = Contract(
        poster=GOVERNMENT,
        origin=origin,
        destination=destination,
        payload=ConsignmentPayload(units=units),
        advance=advance,
        on_delivery=on_delivery,
        posted_turn=sim.current_turn,
        expires_turn=sim.current_turn + 50,
    )
    origin.contracts.post(contract)
    return contract


class _StayPutBrain(ActorBrain):
    """An actor brain that decides nothing."""

    def decide_economic_action(self, actor: Actor):
        return None

    def decide_market_actions(self, actor: Actor):
        return []


def _passenger(
    sim, origin: Planet, destination: Planet, advance: int = 200
) -> Contract:
    """Post a passage contract for a new actor living at ``origin``."""
    actor = Actor(
        name="Traveller",
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[],
        brain=_StayPutBrain(),
        initial_money=1000,
    )
    sim.actors.append(actor)
    origin.add_actor(actor)
    contract = Contract(
        poster=actor,
        origin=origin,
        destination=destination,
        payload=PassengerPayload(actor=actor),
        advance=advance,
        on_delivery=0,
        posted_turn=sim.current_turn,
        expires_turn=sim.current_turn + 50,
    )
    origin.contracts.post(contract)
    return contract


def _loaded_plan(ship: Ship, origin: Planet, destination: Planet, food, quantity=10):
    """Put a haul aboard and mark its plan loaded, ready to depart."""
    ship.cargo.add_commodity(food, quantity)
    plan = TradePlan(
        origin=origin,
        destination=destination,
        commodity=food,
        quantity=quantity,
        bid_price_per_unit=10,
        purchase_price_per_unit=10,
        expected_sell_price_per_unit=30,
        distance=ship.route_distance(origin, destination),
        fuel_needed_one_way=ship.fuel_required(
            ship.route_distance(origin, destination)
        ),
        fuel_price_at_origin=5,
        fuel_units_from_tank=0,
        fuel_price_from_tank=5,
    )
    ship.brain._current_plan = plan
    ship.brain._plan_loaded = True
    return plan


# ---------------------------------------------------------------------------
# Riders
# ---------------------------------------------------------------------------


def test_riders_bound_elsewhere_are_left_on_the_board():
    """A departing ship takes the contracts to where it is going, and no others."""
    sim, _fuel, food, (a, b, c) = _world([("A", 0, 0), ("B", 50, 0), ("C", 0, 50)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    _loaded_plan(ship, a, b, food)

    wanted = _consignment(sim, a, b)
    unwanted = _consignment(sim, a, c)

    assert ship.brain.decide_travel() is b
    assert ship.contracts == [wanted]
    assert wanted.status is ContractStatus.ACCEPTED
    assert unwanted.status is ContractStatus.OPEN


def test_a_rider_loads_at_departure_and_is_delivered_on_arrival():
    """The payload boards when the journey starts and pays out on arrival."""
    sim, _fuel, food, (a, b) = _world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    _loaded_plan(ship, a, b, food)
    contract = _consignment(sim, a, b, advance=200, on_delivery=50)

    money_before = ship.money
    destination = ship.brain.decide_travel()
    assert destination is b
    assert ship.start_journey(destination)
    assert contract.status is ContractStatus.LOADED
    assert ship.money == money_before + 200

    while ship.destination is not None:
        ship.update_journey()

    assert contract.status is ContractStatus.DELIVERED
    assert ship.money == money_before + 250
    assert ship.contracts == []
    assert sim.contracts_delivered == 1


def test_a_rider_to_a_planet_on_the_route_is_accepted():
    """A contract to a planet the trip flies past is taken on like a terminus one."""
    sim, _fuel, food, (a, b, c) = _world(
        [("A", 0, 0), ("B", 50, 0), ("C", 100, 0)], chain=True
    )
    ship = _make_ship(sim, a, fuel_units=60, money=2000, name="Trader")
    _loaded_plan(ship, a, c, food)
    en_route = _consignment(sim, a, b)

    assert ship.brain.decide_travel() is c
    assert ship.contracts == [en_route]
    assert en_route.status is ContractStatus.ACCEPTED


def test_a_rider_is_delivered_when_its_planet_is_passed():
    """The payload gets off as the ship flies past, without the ship docking."""
    sim, _fuel, food, (a, b, c) = _world(
        [("A", 0, 0), ("B", 50, 0), ("C", 100, 0)], chain=True
    )
    ship = _make_ship(sim, a, money=2000, name="Trader")
    ship.fuel = ship.fuel_capacity  # a full tank wants no en-route refuel stop
    _loaded_plan(ship, a, c, food)
    contract = _consignment(sim, a, b, advance=200, on_delivery=50)

    assert ship.brain.decide_travel() is c
    money_before = ship.money
    assert ship.start_journey(c)
    assert contract.status is ContractStatus.LOADED

    while contract.status is ContractStatus.LOADED:
        assert ship.destination is c
        ship.update_journey()

    assert contract.status is ContractStatus.DELIVERED
    assert ship.money == money_before + 250
    # Delivered in flight: the ship never docked at B and is still going to C.
    assert ship.planet is a
    assert ship.destination is c
    assert sim.contracts_delivered == 1


def test_a_payload_off_the_route_is_not_delivered():
    """Passing B delivers nothing bound for D, which this route never reaches."""
    sim, _fuel, food, (a, b, c, d) = _world(
        [("A", 0, 0), ("B", 50, 0), ("C", 100, 0), ("D", 150, 0)], chain=True
    )
    ship = _make_ship(sim, a, money=2000, name="Trader")
    ship.fuel = ship.fuel_capacity
    _loaded_plan(ship, a, c, food)

    off_route = _consignment(sim, a, d)
    assert a.contracts.accept(off_route, ship)
    assert load_contract(ship, off_route)

    assert ship.start_journey(c)
    while ship.destination is not None:
        ship.update_journey()

    assert ship.planet is c
    assert off_route.status is ContractStatus.LOADED
    assert sim.contracts_delivered == 0


# ---------------------------------------------------------------------------
# Contract-only trips
# ---------------------------------------------------------------------------


def test_a_paying_job_is_taken_when_there_is_nothing_to_haul():
    """With no trade plan, a job that beats its fuel becomes the trip."""
    sim, _fuel, _food, (a, b) = _world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    contract = _consignment(sim, a, b, advance=300)

    ship.brain.decide_trade_actions()

    plan = ship.brain._current_plan
    assert isinstance(plan, ContractPlan)
    assert plan.destination is b
    assert plan.contracts == (contract,)
    assert contract.status is ContractStatus.ACCEPTED
    assert ship.brain.decide_travel() is b


def test_a_job_that_does_not_cover_its_fuel_is_refused():
    """Payments below the leg's fuel and maintenance are not worth flying."""
    sim, _fuel, _food, (a, b) = _world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    contract = _consignment(sim, a, b, advance=1)

    ship.brain.decide_trade_actions()

    assert ship.brain._current_plan is None
    assert ship.contracts == []
    assert contract.status is ContractStatus.OPEN


def test_a_broke_ship_funds_its_fuel_with_the_advance():
    """A dry, penniless ship loads the consignment early and buys fuel with it."""
    sim, fuel, _food, (a, b) = _world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=0, money=0, name="Broke")
    contract = _consignment(sim, a, b, advance=400)

    ship.brain.decide_trade_actions()

    # The advance is in hand before the fuel bid goes in.
    assert contract.status is ContractStatus.LOADED
    assert ship.money + ship.reserved_money == 400
    assert any(order.actor is ship for order in a.market.buy_orders[fuel])

    for _ in range(6):
        a.market.match_orders()
        sim.current_turn += 1
        ship.take_turn()

    assert ship.departure_turns
    assert ship.planet is b
    assert contract.status is ContractStatus.DELIVERED


def _haul_world(trade_bid: int):
    """A world where A can export food to far-off C at ``trade_bid`` a unit."""
    sim, fuel, food, planets = _world([("A", 0, 0), ("B", 20, 0), ("C", 0, 200)])
    a, _b, c = planets
    seller = _make_ship(sim, a, name="SellerA")
    seller.cargo.add_commodity(food, 60)
    a.market.place_sell_order(seller, food, 60, 10)
    buyer = _make_ship(sim, c, money=100000, name="BuyerC")
    c.market.place_buy_order(buyer, food, 60, trade_bid)
    return sim, fuel, food, planets


def test_a_well_paid_job_beats_a_longer_haul():
    """The trip worth more per turn occupied wins, not the bigger total."""
    sim, _fuel, _food, (a, b, c) = _haul_world(trade_bid=30)
    ship = _make_ship(sim, a, fuel_units=60, money=4000, name="Trader")
    _consignment(sim, a, b, advance=600)
    ship.brain._nav.refresh_market_facts()

    haul = ship.brain._find_best_trade_plan()
    job = ship.brain._best_contract_trip()
    assert haul is not None and job is not None
    # The haul earns more in total; the job earns more per turn it occupies.
    assert job.expected_profit < haul.expected_profit
    assert ship.brain._trip_turn_value(
        job.expected_profit, b
    ) > ship.brain._trip_turn_value(haul.expected_profit, c)

    ship.brain.decide_trade_actions()

    plan = ship.brain._current_plan
    assert isinstance(plan, ContractPlan)
    assert plan.destination is b


def test_a_thin_job_loses_to_the_haul():
    """A job that pays its fuel and little else does not displace a trade plan."""
    sim, _fuel, _food, (a, b, c) = _haul_world(trade_bid=30)
    ship = _make_ship(sim, a, fuel_units=60, money=4000, name="Trader")
    job = _consignment(sim, a, b, advance=30)
    ship.brain._nav.refresh_market_facts()

    ship.brain.decide_trade_actions()

    plan = ship.brain._current_plan
    assert isinstance(plan, TradePlan)
    assert plan.destination is c
    assert job.status is ContractStatus.OPEN


def _two_haul_world():
    """A world where A can export food to B or to C on near-equal terms.

    C pays a credit a unit more, so C wins the plan search on cargo alone.
    """
    sim, fuel, food, planets = _world([("A", 0, 0), ("B", 60, 0), ("C", 0, 60)])
    a, b, c = planets
    seller = _make_ship(sim, a, name="SellerA")
    seller.cargo.add_commodity(food, 120)
    a.market.place_sell_order(seller, food, 120, 10)
    buyer_b = _make_ship(sim, b, money=100000, name="BuyerB")
    b.market.place_buy_order(buyer_b, food, 60, 30)
    buyer_c = _make_ship(sim, c, money=100000, name="BuyerC")
    c.market.place_buy_order(buyer_c, food, 60, 31)
    return sim, fuel, food, planets


def test_riders_decide_between_two_similar_hauls():
    """The haul whose route also clears the board beats the slightly richer one."""
    sim, _fuel, _food, (a, b, c) = _two_haul_world()
    ship = _make_ship(sim, a, fuel_units=60, money=8000, name="Trader")
    ship.brain._nav.refresh_market_facts()

    # On cargo alone C pays more, so C is the plan.
    without_riders = ship.brain._find_best_trade_plan()
    assert without_riders is not None
    assert without_riders.destination is c

    # A well-paid job bound for B rides along on the B haul and nothing rides
    # on the C haul, which is enough to reverse the ranking.
    _consignment(sim, a, b, advance=900)
    ship.brain._plan_search_memo = None
    with_riders = ship.brain._find_best_trade_plan()
    assert with_riders is not None
    assert with_riders.destination is b
    # The riders decided it; they did not make a bad haul look good.
    assert with_riders.is_profitable()


def test_rider_value_stays_out_of_the_profitability_test():
    """A haul that fails the margin test is not rescued by the riders it could carry."""
    sim, _fuel, food, (a, b) = _world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=60, money=8000, name="Trader")
    plan = _loaded_plan(ship, a, b, food, quantity=10)
    # A wafer-thin spread: profitable but under MIN_MARGIN.
    plan.expected_sell_price_per_unit = 11
    assert not plan.is_profitable()

    _consignment(sim, a, b, advance=5000)
    assert ship.brain._route_rider_value(a, b, ship.free_hold()) == 5000
    # The riders are worth far more than the haul, and change neither test.
    assert not plan.is_profitable()
    assert not ship.brain._plan_acceptable(plan)


# ---------------------------------------------------------------------------
# Pickup stops
# ---------------------------------------------------------------------------


def _chain_trip(advance: int, sell_price: int = 30):
    """A ship flying A to C down a chain, with a job waiting at B bound for C."""
    sim, _fuel, food, (a, b, c) = _world(
        [("A", 0, 0), ("B", 50, 0), ("C", 100, 0)], chain=True
    )
    ship = _make_ship(sim, a, money=4000, name="Trader")
    ship.fuel = ship.fuel_capacity  # a full tank wants no en-route refuel stop
    plan = _loaded_plan(ship, a, c, food)
    plan.expected_sell_price_per_unit = sell_price
    contract = _consignment(sim, b, c, advance=advance)
    return sim, ship, (a, b, c), contract, food


def _fly_until_docked(sim, ship, limit=12):
    """Run turns until the ship is docked somewhere, or ``limit`` runs out."""
    for _ in range(limit):
        sim.current_turn += 1
        for planet in sim.planets:
            planet.market.match_orders()
        ship.take_turn()
        if ship.status.value == "docked":
            return


def test_a_ship_stops_to_pick_up_a_job_bound_for_its_own_destination():
    """A job at a planet on the route is worth a turn, and rides the rest of the way."""
    sim, ship, (a, b, c), contract, _food = _chain_trip(advance=900)
    assert ship.brain.decide_travel() is c
    assert ship.start_journey(c)

    _fly_until_docked(sim, ship)

    assert ship.planet is b
    assert ship.stop_resume is c
    assert ship.stop_for_pickup
    assert ship.pickup_stops == 1
    assert contract.status is ContractStatus.ACCEPTED
    assert contract.carrier is ship

    # The resume loads it, and the terminus delivers it.
    sim.current_turn += 1
    ship.take_turn()
    assert ship.destination is c
    assert contract.status is ContractStatus.LOADED

    while ship.destination is not None:
        ship.update_journey()

    assert ship.planet is c
    assert contract.status is ContractStatus.DELIVERED
    assert sim.contracts_delivered == 1


def test_a_job_worth_less_than_the_delay_is_flown_past():
    """A rider paying less than the trip's own per-turn value does not stop it."""
    sim, ship, (a, b, c), contract, _food = _chain_trip(advance=20, sell_price=400)
    assert ship.brain.decide_travel() is c
    assert ship.brain._departed_trip_turn_value > 20
    assert ship.start_journey(c)

    while ship.destination is not None:
        ship.update_journey()

    assert ship.planet is c
    assert ship.pickup_stops == 0
    assert contract.status is ContractStatus.OPEN


def test_a_pickup_stop_that_cannot_leave_puts_the_job_back():
    """A stop stuck past REFUEL_STOP_MAX_TURNS is abandoned and the job re-posted."""
    sim, ship, (a, b, c), contract, food = _chain_trip(advance=900)
    assert ship.brain.decide_travel() is c
    assert ship.start_journey(c)

    _fly_until_docked(sim, ship)
    assert ship.planet is b
    assert contract.status is ContractStatus.ACCEPTED

    # Nothing to fly on with, and a hold too full to take the job a second
    # time, so the stop runs out of patience and nothing re-adopts the job.
    ship.fuel = 0
    ship.money = 0
    ship.cargo.add_commodity(food, 75)
    for _ in range(REFUEL_STOP_MAX_TURNS + 1):
        sim.current_turn += 1
        ship.take_turn()

    assert ship.stop_resume is None
    assert not ship.stop_for_pickup
    assert contract.status is ContractStatus.OPEN
    assert contract.carrier is None
    assert ship.contracts == []


def test_a_pickup_stop_still_delivers_what_was_bound_for_that_planet():
    """The payload for the stop's own planet gets off before the ship docks there."""
    sim, ship, (a, b, c), waiting, _food = _chain_trip(advance=900)
    # A second job, aboard from A and bound for B, the planet the ship stops at.
    for_b = _consignment(sim, a, b, advance=200, on_delivery=50)
    assert a.contracts.accept(for_b, ship)

    assert ship.brain.decide_travel() is c
    assert ship.start_journey(c)
    assert for_b.status is ContractStatus.LOADED

    _fly_until_docked(sim, ship)

    assert ship.planet is b
    assert ship.stop_for_pickup
    # Delivered as the ship passed B, not carried on through the stop.
    assert for_b.status is ContractStatus.DELIVERED
    assert waiting.status is ContractStatus.ACCEPTED
    assert ship.contracts == [waiting]


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


def test_a_loaded_passenger_pins_the_destination():
    """A payload aboard outranks the cargo the ship would rather chase."""
    sim, _fuel, food, (a, b, c) = _world([("A", 0, 0), ("B", 50, 0), ("C", 0, 50)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    # Cargo aboard that C pays well for: without the passenger the ship flies
    # there instead.
    ship.cargo.add_commodity(food, 10)
    buyer = _make_ship(sim, c, money=20000, name="BuyerC")
    c.market.place_buy_order(buyer, food, 10, 200)

    contract = _passenger(sim, a, b)
    assert a.contracts.accept(contract, ship)
    assert load_contract(ship, contract)

    ship.brain.decide_trade_actions()

    assert ship.brain._committed_fuel_need == ship.brain._departure_fuel_requirement(b)
    assert ship.brain.decide_travel() is b


# ---------------------------------------------------------------------------
# Remote pickup
# ---------------------------------------------------------------------------


def test_reposition_aims_at_a_planet_whose_only_export_is_jobs():
    """An empty ship with nothing to trade flies to where the jobs are."""
    sim, fuel, food, (a, b, c) = _world([("A", 0, 0), ("B", 50, 0), ("C", 0, 50)])
    # A galaxy-wide trade signal that backs no profitable plan: food is for
    # sale at C at a price no bid anywhere beats.
    seller = _make_ship(sim, c, name="SellerC")
    seller.cargo.add_commodity(food, 50)
    c.market.place_sell_order(seller, food, 50, 100)
    buyer = _make_ship(sim, c, money=5000, name="BuyerC")
    c.market.place_buy_order(buyer, food, 50, 5)

    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    assert ship.brain._find_best_trade_plan() is None

    # Nothing at B to export, but a pile of freight bound for C.
    for _ in range(3):
        _consignment(sim, b, c, advance=300)

    assert ship.brain._find_reposition_target(ship.fuel, fuel) is b


def test_a_plan_starved_ship_flies_to_a_queue_in_a_cold_galaxy():
    """With nothing to trade anywhere, a waiting ship still goes where the jobs are."""
    sim, fuel, _food, (a, b, c) = _world([("A", 0, 0), ("B", 50, 0), ("C", 0, 50)])
    ship = _make_ship(sim, a, fuel_units=40, money=2000, name="Trader")
    for _ in range(3):
        _consignment(sim, b, c, advance=300)

    # No commodity has both supply and demand, so no origin backs a plan and
    # the survey is skipped - until the ship has waited long enough.
    ship.brain._turns_without_plan = 0
    assert ship.brain._find_reposition_target(ship.fuel, fuel) is None

    ship.brain._turns_without_plan = REPOSITION_CONTRACT_PATIENCE
    assert ship.brain._find_reposition_target(ship.fuel, fuel) is b
