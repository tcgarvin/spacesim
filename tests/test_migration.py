"""Tests for the core mechanics of actor migration.

Brain reasoning (whether and where to move) is tested elsewhere; these
cover what core does: the land pools, the passage contracts the migration
phase keeps on the boards, and the invariants the rest of the sim relies on.
"""

import math
from typing import TYPE_CHECKING, Optional, cast

import pytest

from spacesim.core.actor import Actor, ActorType
from spacesim.core.actor_brain import ActorBrain
from spacesim.core.brains import SpaceportOperatorBrain
from spacesim.core.contracts import ContractStatus, PassengerPayload
from spacesim.core.government import GOVERNMENT_JOB_MARGIN
from spacesim.core.land import Land, NoFreeLandError
from spacesim.core.market import Market
from spacesim.core.migration import (
    NO_MIGRATION,
    PASSAGE_CONTRACT_TTL,
    PASSAGE_FARE_MARGIN,
    PASSAGE_FARE_MINIMUM,
    MigrationRequest,
    passage_fare,
    refresh_planet_stats,
    run_migration_phase,
)
from spacesim.core.navigation import FUEL_BID_FALLBACK_FLOOR
from spacesim.core.planet import Planet
from spacesim.core.ship import Ship
from spacesim.core.simulation import Simulation

if TYPE_CHECKING:
    from spacesim.core.navigation import Navigator


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


def _operator(sim: Simulation, name: str) -> Actor:
    return Actor(
        name=name,
        sim=sim,
        actor_type=ActorType.SERVICE,
        drives=[],
        brain=SpaceportOperatorBrain(),
        initial_money=0,
    )


def _two_planet_sim(lands: int = 10, operators: int = 2) -> Simulation:
    """A galaxy of two lane-joined planets with no automatic setup."""
    sim = Simulation()
    for name in ("Origin", "Destination"):
        market = Market()
        market.commodity_registry = sim.commodity_registry
        planet = Planet(name, market, x=0.0, y=0.0, num_lands=lands)
        sim.planets.append(planet)
    sim.planets[1].x = 30.0
    sim.star_lanes.add_lane(sim.planets[0], sim.planets[1])

    for index, planet in enumerate(sim.planets):
        for i in range(operators):
            operator = _operator(sim, f"{planet.name}Operator-{i}")
            sim.actors.append(operator)
            planet.add_actor(operator)
        for i in range(2):
            actor = _regular(sim, f"{planet.name}Regular-{index}{i}")
            sim.actors.append(actor)
            planet.add_actor(actor)
    return sim


class _FareNavigator:
    """One fixed lane distance and fuel reference, for pricing a fare alone."""

    def __init__(self, distance: float, reference: Optional[float] = 40.0) -> None:
        self._distance = distance
        self._reference = reference

    def distance(self, a: Planet, b: Planet) -> float:
        return self._distance

    def fuel_value_reference(self) -> Optional[float]:
        return self._reference


def _fare(distance: float, reference: Optional[float] = 40.0) -> int:
    sim = _two_planet_sim()
    navigator = cast("Navigator", _FareNavigator(distance, reference))
    return passage_fare(navigator, sim.planets[0], sim.planets[1])


class TestPassageFare:
    def test_fare_is_the_legs_fuel_plus_the_margin(self) -> None:
        # 1000 lane units burns 5 fuel at 1 per 200; at a 40-credit reference
        # that is 200 of fuel, and PASSAGE_FARE_MARGIN adds half.
        assert _fare(1000.0) == 300

    def test_the_margin_is_double_a_government_jobs(self) -> None:
        assert PASSAGE_FARE_MARGIN == 2 * GOVERNMENT_JOB_MARGIN

    def test_a_cheap_short_leg_pays_the_minimum(self) -> None:
        # A quarter of a fuel unit at 10 credits, plus half, is 4: under the floor.
        assert _fare(50.0, reference=10.0) == PASSAGE_FARE_MINIMUM

    def test_no_fuel_reference_falls_back_to_the_bid_floor(self) -> None:
        # Before anything trades: 5 fuel at FUEL_BID_FALLBACK_FLOOR, plus half.
        expected = math.ceil(5 * FUEL_BID_FALLBACK_FLOOR * 1.5)
        assert _fare(1000.0, reference=None) == expected


class TestPlanetLandPool:
    def test_release_and_claim_round_trip(self) -> None:
        planet = Planet("P", Market(), num_lands=3)
        land = planet.claim_land()
        assert len(planet.free_lands) == 2
        planet.release_land(land)
        assert len(planet.free_lands) == 3
        assert any(other is land for other in planet.free_lands)

    def test_remove_actor_returns_land_to_pool(self) -> None:
        sim = Simulation()
        planet = Planet("P", Market(), num_lands=3)
        actor = _regular(sim, "A")
        planet.add_actor(actor)
        claimed = actor.land
        assert len(planet.free_lands) == 2

        planet.remove_actor(actor)
        assert actor not in planet.actors
        assert len(planet.free_lands) == 3
        assert any(land is claimed for land in planet.free_lands)
        assert actor.land.coefficients == Land.default().coefficients

    def test_service_actor_removal_touches_no_land(self) -> None:
        sim = Simulation()
        planet = Planet("P", Market(), num_lands=3)
        operator = _operator(sim, "Op")
        planet.add_actor(operator)
        assert len(planet.free_lands) == 3
        planet.remove_actor(operator)
        assert len(planet.free_lands) == 3

    def test_remove_actor_on_wrong_planet_raises(self) -> None:
        sim = Simulation()
        home = Planet("Home", Market(), num_lands=3)
        elsewhere = Planet("Elsewhere", Market(), num_lands=3)
        actor = _regular(sim, "A")
        home.add_actor(actor)
        with pytest.raises(ValueError):
            elsewhere.remove_actor(actor)

    def test_add_actor_with_land_does_not_draw_a_second(self) -> None:
        sim = Simulation()
        planet = Planet("P", Market(), num_lands=3)
        reserved = planet.claim_land()
        actor = _regular(sim, "A")
        planet.add_actor_with_land(actor, reserved)
        assert actor.land is reserved
        assert len(planet.free_lands) == 2
        assert actor.planet is planet

    def test_exhausted_pool_raises(self) -> None:
        planet = Planet("P", Market(), num_lands=1)
        planet.claim_land()
        with pytest.raises(NoFreeLandError):
            planet.claim_land()


class TestPlanetStats:
    def test_stats_over_regular_residents(self) -> None:
        sim = Simulation()
        sim.setup_simple(
            num_planets=2,
            num_regular_actors=4,
            num_market_makers=1,
            num_spaceport_operators=1,
            num_ships=0,
            lands_per_planet=20,
        )
        stats = refresh_planet_stats(sim)

        assert stats is sim.planet_stats
        assert set(stats) == set(sim.planets)
        for planet in sim.planets:
            entry = stats[planet]
            assert entry.population == 4  # service actors excluded
            assert entry.free_land_count == 20 - 4
            assert entry.free_land_mean  # non-empty pool
            assert all(0.0 <= v <= 1.0 for v in entry.free_land_mean.values())
            assert entry.median_money == 50.0
            assert 0.0 <= entry.median_need_debt <= 1.0
            assert 0.0 <= entry.median_prosperity <= 1.0

    def test_empty_pool_gives_empty_means(self) -> None:
        sim = _two_planet_sim(lands=2)
        stats = refresh_planet_stats(sim)
        entry = stats[sim.planets[0]]
        assert entry.free_land_count == 0
        assert entry.free_land_mean == {}
        assert entry.population == 2


def _ship(sim: Simulation, planet: Planet, fuel: int = -1) -> Ship:
    """A hauler with a full tank unless ``fuel`` (millifuel) says otherwise."""
    ship = Ship("Hauler", sim, planet, initial_money=0)
    sim.ships.append(ship)
    planet.add_ship(ship)
    ship.fuel = ship.fuel_capacity if fuel < 0 else fuel
    return ship


def _leaver(sim: Simulation) -> Actor:
    """A regular resident of the origin planet."""
    return next(a for a in sim.planets[0].actors if a.claims_land)


class TestPassageContracts:
    def test_a_request_posts_a_contract_with_the_offer_reserved(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        money_before = actor.money

        sim.current_turn = 1
        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)

        contract = actor.passage_contract
        assert contract is not None
        assert contract.status is ContractStatus.OPEN
        assert isinstance(contract.payload, PassengerPayload)
        assert contract.payload.actor is actor
        assert contract.payload.land is None
        assert (contract.advance, contract.on_delivery) == (60, 0)
        assert contract.destination is destination
        assert contract.expires_turn == 1 + PASSAGE_CONTRACT_TTL
        assert origin.contracts.open_contracts() == [contract]
        assert sim.contracts_posted == 1

        # Reserved like a bid, and nobody has moved.
        assert actor.money == money_before - 60
        assert actor.reserved_money == 60
        assert actor in origin.actors
        assert actor in sim.actors
        assert sim.migration_departures == 0

    def test_a_standing_request_does_not_repost(self) -> None:
        sim = _two_planet_sim(lands=10)
        actor = _leaver(sim)
        actor.migration_request = MigrationRequest(sim.planets[1], fare_offer=60)
        run_migration_phase(sim)
        first = actor.passage_contract

        run_migration_phase(sim)
        assert actor.passage_contract is first
        assert sim.contracts_posted == 1

    def test_a_higher_offer_reprices_the_contract(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        money_before = actor.money
        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)
        first = actor.passage_contract

        actor.migration_request = MigrationRequest(destination, fare_offer=90)
        run_migration_phase(sim)

        assert first is not None and first.status is ContractStatus.CANCELLED
        contract = actor.passage_contract
        assert contract is not None and contract is not first
        assert contract.advance == 90
        # The wait is measured from the first ask, not the last re-price.
        assert contract.posted_turn == first.posted_turn
        assert origin.contracts.open_contracts() == [contract]
        assert actor.money == money_before - 90
        assert actor.reserved_money == 90
        # A re-price replaces the contract; it is not a second posting.
        assert sim.contracts_posted == 1

    def test_a_new_destination_replaces_the_contract(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        third = Planet("Third", Market(), x=0.0, y=40.0, num_lands=10)
        sim.planets.append(third)
        sim.star_lanes.add_lane(origin, third)
        actor = _leaver(sim)

        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)
        first = actor.passage_contract

        actor.migration_request = MigrationRequest(third, fare_offer=60)
        run_migration_phase(sim)

        assert first is not None and first.status is ContractStatus.CANCELLED
        contract = actor.passage_contract
        assert contract is not None and contract.destination is third
        assert origin.contracts.open_contracts() == [contract]

    def test_no_migration_cancels_and_refunds(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        money_before = actor.money
        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)

        actor.migration_request = NO_MIGRATION
        run_migration_phase(sim)

        contract = actor.passage_contract
        assert contract is not None and contract.status is ContractStatus.CANCELLED
        assert origin.contracts.open_contracts() == []
        assert actor.money == money_before
        assert actor.reserved_money == 0

    def test_an_accepted_contract_is_left_alone(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None
        origin.contracts.accept(contract, _ship(sim, origin))

        actor.migration_request = MigrationRequest(destination, fare_offer=200)
        run_migration_phase(sim)
        assert actor.passage_contract is contract
        assert contract.status is ContractStatus.ACCEPTED
        assert contract.advance == 60

        actor.migration_request = NO_MIGRATION
        run_migration_phase(sim)
        assert contract.status is ContractStatus.ACCEPTED

    def test_the_offer_is_capped_at_the_actors_money(self) -> None:
        sim = _two_planet_sim(lands=10)
        actor = _leaver(sim)
        actor.money = 25
        actor.migration_request = MigrationRequest(sim.planets[1], fare_offer=400)
        run_migration_phase(sim)

        contract = actor.passage_contract
        assert contract is not None and contract.advance == 25
        assert actor.money == 0

    def test_a_request_for_the_current_planet_posts_nothing(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin = sim.planets[0]
        actor = _leaver(sim)
        actor.migration_request = MigrationRequest(origin, fare_offer=60)
        run_migration_phase(sim)

        assert actor.passage_contract is None
        assert origin.contracts.open_contracts() == []

    def test_expiry_refunds_and_frees_the_actor_to_repost(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        money_before = actor.money
        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)
        first = actor.passage_contract
        assert first is not None

        sim.current_turn = first.expires_turn
        origin.contracts.expire(sim.current_turn)
        assert first.status is ContractStatus.EXPIRED
        assert actor.money == money_before
        assert actor.reserved_money == 0

        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None and contract is not first
        assert contract.status is ContractStatus.OPEN

    def test_run_turn_counts_an_expired_passage(self) -> None:
        sim = _two_planet_sim(lands=10)
        actor = _leaver(sim)
        actor.migration_request = MigrationRequest(sim.planets[1], fare_offer=60)
        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None
        contract.expires_turn = sim.current_turn + 1
        actor.migration_request = NO_MIGRATION

        sim.run_turn()
        assert contract.status is ContractStatus.EXPIRED
        assert sim.passage_expired == 1


class TestPassengerJourney:
    def _posted(self, sim: Simulation):
        actor = _leaver(sim)
        actor.migration_request = MigrationRequest(sim.planets[1], fare_offer=60)
        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None
        return actor, contract

    def test_boarding_counts_the_departure_and_the_wait(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        sim.current_turn = 5
        actor, contract = self._posted(sim)
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)

        sim.current_turn = 12
        assert ship.start_journey(destination, resuming=True) is True

        assert contract.status is ContractStatus.LOADED
        assert sim.migration_departures == 1
        assert sim.passage_wait_turns == [7]
        assert ship.money == 60
        assert actor.in_transit is True
        assert actor not in sim.actors
        event = sim.migration_log[-1]
        assert (event.turn, event.origin_name, event.destination_name, event.fare) == (
            12,
            "Origin",
            "Destination",
            60,
        )

    def test_delivery_settles_the_migrant_and_counts_the_arrival(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor, contract = self._posted(sim)
        brain = actor.brain
        assert isinstance(brain, StayPutBrain)
        brain._turn_cache(actor).yield_modifier["gather_wood"] = 0.5
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        ship.start_journey(destination, resuming=True)
        assert isinstance(contract.payload, PassengerPayload)
        claimed = contract.payload.land

        while ship.destination is not None:
            ship.update_journey()

        assert contract.status is ContractStatus.DELIVERED
        assert sim.migration_arrivals == 1
        assert actor.in_transit is False
        assert actor.planet is destination
        assert actor in destination.actors
        assert actor in sim.actors
        assert actor.land is claimed
        # A stale yield cache would keep valuing the old planet's land.
        assert brain._cache is None
        assert brain.relocated_count == 1

    def test_boarding_cancels_the_migrants_resting_orders(self) -> None:
        sim = _two_planet_sim(lands=10)
        origin, destination = sim.planets
        actor = _leaver(sim)
        wood = sim.commodity_registry.get_commodity("wood")
        assert wood is not None
        actor.inventory.add_commodity(wood, 5)
        origin.market.place_sell_order(actor, wood, 5, 12)
        assert actor.active_orders

        actor.migration_request = MigrationRequest(destination, fare_offer=60)
        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        ship.start_journey(destination, resuming=True)

        assert not actor.active_orders
        assert actor.inventory.get_quantity(wood) == 0


class TestSimulationIntegration:
    def test_parallel_invariant_holds_with_a_passenger_aboard(self) -> None:
        sim = Simulation()
        sim.setup_simple(
            num_planets=2,
            num_regular_actors=4,
            num_market_makers=1,
            num_spaceport_operators=1,
            num_ships=0,
            lands_per_planet=20,
        )
        origin, destination = sim.planets
        actor = next(a for a in origin.actors if a.claims_land)
        actor.money = 5_000
        actor.migration_request = MigrationRequest(destination, fare_offer=100)
        run_migration_phase(sim)
        contract = actor.passage_contract
        assert contract is not None
        ship = _ship(sim, origin)
        origin.contracts.accept(contract, ship)
        assert ship.start_journey(destination, resuming=True) is True
        assert contract.status is ContractStatus.LOADED

        # The parallel actor phase refuses to shard unless the two lists
        # cover each other exactly.
        assert sum(len(p.actors) for p in sim.planets) == len(sim.actors)

        sim.parallel_workers = 2
        sim.run_turn()
        assert sum(len(p.actors) for p in sim.planets) == len(sim.actors)

    def test_default_brains_run_turns_without_error(self) -> None:
        sim = Simulation()
        sim.setup_simple(
            num_planets=3,
            num_regular_actors=5,
            num_market_makers=1,
            num_spaceport_operators=1,
            num_ships=1,
            lands_per_planet=20,
        )
        for _ in range(5):
            sim.run_turn()

        assert sim.planet_stats
        placed = sum(len(p.actors) for p in sim.planets)
        assert placed == len(sim.actors)
        aboard = sum(
            1
            for ship in sim.ships
            for contract in ship.contracts
            if contract.status is ContractStatus.LOADED
            and isinstance(contract.payload, PassengerPayload)
        )
        assert sim.migration_departures - sim.migration_arrivals == aboard
