"""Tests for the contract primitive: the board, the money, and the payloads.

Whether to post or accept a contract is brain logic and is tested
elsewhere; these cover what core does once one exists.
"""

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.contracts import (
    GOVERNMENT,
    MIGRANT_CARGO_UNITS,
    ConsignmentPayload,
    Contract,
    ContractStatus,
    PassengerPayload,
    strand_contract,
)
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.ship import Ship
from spacesim2.core.simulation import Simulation


class StayPutBrain(ActorBrain):
    """A brain that does nothing, with a record of its relocation hook."""

    def __init__(self) -> None:
        self.relocated_count = 0

    def decide_economic_action(self, actor: Actor):
        return None

    def decide_market_actions(self, actor: Actor):
        return []

    def on_relocated(self, actor: Actor) -> None:
        self.relocated_count += 1


def _regular(sim: Simulation, name: str, money: int = 1000) -> Actor:
    return Actor(
        name=name,
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[],
        brain=StayPutBrain(),
        initial_money=money,
    )


def _two_planet_sim(lands: int = 10) -> Simulation:
    """A galaxy of two lane-joined planets with no automatic setup."""
    sim = Simulation()
    for name in ("Origin", "Destination"):
        market = Market()
        market.commodity_registry = sim.commodity_registry
        planet = Planet(name, market, x=0.0, y=0.0, num_lands=lands)
        sim.planets.append(planet)
    sim.planets[1].x = 30.0
    sim.star_lanes.add_lane(sim.planets[0], sim.planets[1])
    return sim


def _ship(sim: Simulation, planet: Planet, fuel: int = -1) -> Ship:
    """A hauler with a full tank unless ``fuel`` (millifuel) says otherwise."""
    ship = Ship("Hauler", sim, planet, initial_money=0)
    sim.ships.append(ship)
    planet.add_ship(ship)
    ship.fuel = ship.fuel_capacity if fuel < 0 else fuel
    return ship


def _passenger_contract(
    sim: Simulation, actor: Actor, advance: int = 40, on_delivery: int = 10
) -> Contract:
    """A passage contract posted by the passenger itself, ready to accept."""
    origin, destination = sim.planets[0], sim.planets[1]
    contract = Contract(
        poster=actor,
        origin=origin,
        destination=destination,
        payload=PassengerPayload(actor=actor),
        advance=advance,
        on_delivery=on_delivery,
        posted_turn=sim.current_turn,
        expires_turn=sim.current_turn + 20,
    )
    origin.contracts.post(contract)
    return contract


def _government_contract(
    sim: Simulation, advance: int = 30, on_delivery: int = 5
) -> Contract:
    origin, destination = sim.planets[0], sim.planets[1]
    contract = Contract(
        poster=GOVERNMENT,
        origin=origin,
        destination=destination,
        payload=ConsignmentPayload(units=20),
        advance=advance,
        on_delivery=on_delivery,
        posted_turn=sim.current_turn,
        expires_turn=sim.current_turn + 20,
    )
    origin.contracts.post(contract)
    return contract


def _fly(ship: Ship, destination: Planet) -> None:
    """Drive a ship to ``destination`` without involving its brain."""
    assert ship.start_journey(destination, resuming=True)
    while ship.destination is not None:
        ship.update_journey()


class TestBoard:
    def test_post_reserves_the_posters_money(self) -> None:
        sim = _two_planet_sim()
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        sim.planets[0].add_actor(actor)

        contract = _passenger_contract(sim, actor, advance=40, on_delivery=10)
        assert contract.status is ContractStatus.OPEN
        assert contract.total_payment == 50
        assert actor.money == 950
        assert actor.reserved_money == 50
        assert sim.planets[0].contracts.open_contracts() == [contract]
        assert sim.planets[0].contracts.open_to(sim.planets[1]) == [contract]

    def test_government_post_reserves_nothing(self) -> None:
        sim = _two_planet_sim()
        contract = _government_contract(sim)
        assert contract.status is ContractStatus.OPEN
        assert sim.planets[0].contracts.open_contracts() == [contract]

    def test_cancel_releases_the_reserve(self) -> None:
        sim = _two_planet_sim()
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        sim.planets[0].add_actor(actor)
        contract = _passenger_contract(sim, actor)

        assert sim.planets[0].contracts.cancel(contract) is True
        assert contract.status is ContractStatus.CANCELLED
        assert actor.money == 1000
        assert actor.reserved_money == 0
        assert sim.planets[0].contracts.open_contracts() == []

    def test_cancel_refuses_an_accepted_contract(self) -> None:
        sim = _two_planet_sim()
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        sim.planets[0].add_actor(actor)
        contract = _passenger_contract(sim, actor)
        ship = _ship(sim, sim.planets[0])
        sim.planets[0].contracts.accept(contract, ship)

        assert sim.planets[0].contracts.cancel(contract) is False
        assert contract.status is ContractStatus.ACCEPTED

    def test_expire_refunds_and_delists(self) -> None:
        sim = _two_planet_sim()
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        sim.planets[0].add_actor(actor)
        contract = _passenger_contract(sim, actor)

        assert sim.planets[0].contracts.expire(contract.expires_turn - 1) == []
        expired = sim.planets[0].contracts.expire(contract.expires_turn)
        assert expired == [contract]
        assert contract.status is ContractStatus.EXPIRED
        assert actor.money == 1000
        assert actor.reserved_money == 0
        assert sim.planets[0].contracts.open_contracts() == []

    def test_expire_leaves_accepted_contracts_alone(self) -> None:
        sim = _two_planet_sim()
        contract = _government_contract(sim)
        ship = _ship(sim, sim.planets[0])
        sim.planets[0].contracts.accept(contract, ship)

        assert sim.planets[0].contracts.expire(contract.expires_turn + 5) == []
        assert contract.status is ContractStatus.ACCEPTED

    def test_accept_and_release_round_trip(self) -> None:
        sim = _two_planet_sim()
        contract = _government_contract(sim)
        ship = _ship(sim, sim.planets[0])
        board = sim.planets[0].contracts

        assert board.accept(contract, ship) is True
        assert contract.carrier is ship
        assert ship.contracts == [contract]
        assert board.open_contracts() == []
        assert board.accept(contract, ship) is False

        assert board.release(contract) is True
        assert contract.carrier is None
        assert ship.contracts == []
        assert board.open_contracts() == [contract]

    def test_run_turn_expires_open_contracts(self) -> None:
        sim = Simulation()
        sim.setup_simple(
            num_planets=2, num_regular_actors=2, num_market_makers=1, num_ships=1
        )
        contract = Contract(
            poster=GOVERNMENT,
            origin=sim.planets[0],
            destination=sim.planets[1],
            payload=ConsignmentPayload(units=5),
            advance=10,
            on_delivery=0,
            posted_turn=sim.current_turn,
            expires_turn=sim.current_turn + 1,
        )
        sim.planets[0].contracts.post(contract)

        sim.run_turn()
        assert contract.status is ContractStatus.EXPIRED
        assert sim.contracts_expired == 1


class TestFreeHold:
    def test_accepted_contracts_take_hold_space(self) -> None:
        sim = _two_planet_sim()
        ship = _ship(sim, sim.planets[0])
        assert ship.free_hold() == ship.cargo_capacity

        contract = _government_contract(sim)
        sim.planets[0].contracts.accept(contract, ship)
        assert ship.contract_hold_units() == 20
        assert ship.free_hold() == ship.cargo_capacity - 20

        sim.planets[0].contracts.release(contract)
        assert ship.free_hold() == ship.cargo_capacity

    def test_cargo_and_contracts_both_count(self) -> None:
        sim = _two_planet_sim()
        ship = _ship(sim, sim.planets[0])
        food = sim.commodity_registry.get_commodity("food")
        assert food is not None
        ship.cargo.add_commodity(food, 15)
        contract = _government_contract(sim)
        sim.planets[0].contracts.accept(contract, ship)
        assert ship.free_hold() == ship.cargo_capacity - 35


class TestPassengerLifecycle:
    def test_departure_loads_and_arrival_delivers(self) -> None:
        sim = _two_planet_sim()
        origin, destination = sim.planets
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        origin.add_actor(actor)
        food = sim.commodity_registry.get_commodity("food")
        assert food is not None
        actor.inventory.add_commodity(food, 3)

        contract = _passenger_contract(sim, actor, advance=40, on_delivery=10)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        free_lands_before = len(destination.free_lands)

        # resuming=True skips the maintenance roll, which is random.
        assert ship.start_journey(destination, resuming=True) is True
        assert contract.status is ContractStatus.LOADED
        assert contract.payload.hold_units == MIGRANT_CARGO_UNITS
        assert isinstance(contract.payload, PassengerPayload)
        claimed = contract.payload.land
        assert claimed is not None
        assert len(destination.free_lands) == free_lands_before - 1
        assert actor.in_transit is True
        assert actor not in origin.actors
        assert actor not in sim.actors
        assert actor.inventory.get_quantity(food) == 0
        assert ship.money == 40
        assert actor.reserved_money == 10

        while ship.destination is not None:
            ship.update_journey()

        assert contract.status is ContractStatus.DELIVERED
        assert ship.contracts == []
        assert ship.money == 50
        assert actor.reserved_money == 0
        assert actor.in_transit is False
        assert actor in destination.actors
        assert actor in sim.actors
        assert actor.land is claimed
        assert sim.contracts_delivered == 1
        assert origin.contracts.contracts == {}

    def test_load_fails_and_releases_when_no_land_is_free(self) -> None:
        sim = _two_planet_sim(lands=1)
        origin, destination = sim.planets
        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        origin.add_actor(actor)
        # Claim the destination's only land so the pool is empty at load.
        destination.claim_land()

        contract = _passenger_contract(sim, actor)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)

        ship.start_journey(destination, resuming=True)
        assert contract.status is ContractStatus.OPEN
        assert contract.carrier is None
        assert ship.contracts == []
        assert actor in origin.actors
        assert actor.in_transit is False
        assert ship.money == 0

    def test_accepted_contract_elsewhere_is_released_at_departure(self) -> None:
        sim = _two_planet_sim()
        origin, destination = sim.planets
        # A contract bound back the other way: the ship is not going there.
        contract = Contract(
            poster=GOVERNMENT,
            origin=origin,
            destination=origin,
            payload=ConsignmentPayload(units=5),
            advance=10,
            on_delivery=0,
            posted_turn=0,
            expires_turn=50,
        )
        origin.contracts.post(contract)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)

        _fly(ship, destination)
        assert contract.status is ContractStatus.OPEN
        assert contract.carrier is None
        assert ship.contracts == []
        assert origin.contracts.open_contracts() == [contract]

    def test_strand_settles_the_passenger_where_the_ship_is(self) -> None:
        sim = _two_planet_sim()
        origin, destination = sim.planets[0], sim.planets[1]
        # A third planet the ship strands at, joined to the origin by a lane.
        market = Market()
        market.commodity_registry = sim.commodity_registry
        waypoint = Planet("Waypoint", market, x=0.0, y=40.0, num_lands=10)
        sim.planets.append(waypoint)
        sim.star_lanes.add_lane(origin, waypoint)

        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        origin.add_actor(actor)
        contract = _passenger_contract(sim, actor, advance=40, on_delivery=10)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        ship.start_journey(destination, resuming=True)
        assert contract.status is ContractStatus.LOADED
        destination_lands = len(destination.free_lands)

        # Put the ship down at the waypoint instead of the destination.
        ship.planet = waypoint
        ship.destination = None
        ship.route = []
        ship.travel_progress = 0.0
        from spacesim2.core.ship import ShipStatus

        ship.status = ShipStatus.DOCKED
        waypoint_lands = len(waypoint.free_lands)

        assert strand_contract(ship, contract, waypoint) is True
        assert contract.status is ContractStatus.CANCELLED
        assert ship.contracts == []
        assert actor in waypoint.actors
        assert actor in sim.actors
        assert actor.in_transit is False
        assert len(waypoint.free_lands) == waypoint_lands - 1
        assert len(destination.free_lands) == destination_lands + 1
        # The advance stays with the carrier; only on_delivery is refunded.
        assert ship.money == 40
        assert actor.money == 960
        assert actor.reserved_money == 0
        assert sim.contracts_stranded == 1

    def test_strand_fails_with_no_free_land(self) -> None:
        sim = _two_planet_sim()
        origin, destination = sim.planets[0], sim.planets[1]
        market = Market()
        market.commodity_registry = sim.commodity_registry
        waypoint = Planet("Waypoint", market, x=0.0, y=40.0, num_lands=0)
        sim.planets.append(waypoint)
        sim.star_lanes.add_lane(origin, waypoint)

        actor = _regular(sim, "Traveller", money=1000)
        sim.actors.append(actor)
        origin.add_actor(actor)
        contract = _passenger_contract(sim, actor)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        ship.start_journey(destination, resuming=True)

        assert strand_contract(ship, contract, waypoint) is False
        assert contract.status is ContractStatus.LOADED
        assert ship.contracts == [contract]
        assert sim.contracts_stranded == 0


class TestConsignmentLifecycle:
    def test_government_freight_creates_the_payment(self) -> None:
        sim = _two_planet_sim()
        origin, destination = sim.planets
        contract = _government_contract(sim, advance=30, on_delivery=5)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)

        _fly(ship, destination)
        assert contract.status is ContractStatus.DELIVERED
        assert ship.money == 35
        assert ship.contracts == []
        assert ship.free_hold() == ship.cargo_capacity
        assert sim.contracts_delivered == 1
        assert origin.contracts.contracts == {}
