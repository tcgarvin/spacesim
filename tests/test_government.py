"""Tests for government freight: what gets posted, and what a flown job pays.

Whether a ship takes a job is brain logic and is tested elsewhere; these
accept and fly one by hand.
"""

import math

from spacesim.core.contracts import GOVERNMENT, ContractStatus
from spacesim.core.government import (
    GOVERNMENT_JOB_MARGIN,
    GOVERNMENT_JOB_MAX_HOPS,
    GOVERNMENT_JOB_TTL,
    GOVERNMENT_JOB_UNITS_MAX,
    GOVERNMENT_JOB_UNITS_MIN,
    GOVERNMENT_JOBS_PER_PLANET,
    refresh_government_jobs,
)
from spacesim.core.market import Market
from spacesim.core.navigation import FUEL_BID_FALLBACK_FLOOR, get_navigator
from spacesim.core.planet import Planet
from spacesim.core.ship import FUEL_MILLI, Ship
from spacesim.core.simulation import Simulation


def _chain_sim(planet_count: int) -> Simulation:
    """A galaxy of planets on one lane chain, 30 units apart, no actors."""
    sim = Simulation()
    for index in range(planet_count):
        market = Market()
        market.commodity_registry = sim.commodity_registry
        planet = Planet(f"P{index}", market, x=30.0 * index, y=0.0, num_lands=10)
        sim.planets.append(planet)
        if index > 0:
            sim.star_lanes.add_lane(sim.planets[index - 1], planet)
    return sim


def _government_jobs(planet: Planet):
    return [
        contract
        for contract in planet.contracts.open_contracts()
        if contract.poster is GOVERNMENT
    ]


def _expected_advance(sim: Simulation, origin: Planet, destination: Planet) -> int:
    navigator = get_navigator(sim)
    reference = navigator.fuel_value_reference()
    if reference is None:
        reference = float(FUEL_BID_FALLBACK_FLOOR)
    # calculate_fuel_needed is millifuel; the reference prices a market unit.
    fuel = Ship.calculate_fuel_needed(navigator.distance(origin, destination))
    return math.ceil(fuel * reference / FUEL_MILLI * (1 + GOVERNMENT_JOB_MARGIN))


class TestRefresh:
    def test_every_planet_carries_its_quota_after_a_turn(self) -> None:
        sim = _chain_sim(5)
        sim.run_turn()
        for planet in sim.planets:
            assert len(_government_jobs(planet)) == GOVERNMENT_JOBS_PER_PLANET

    def test_job_destination_is_another_planet_within_range(self) -> None:
        sim = _chain_sim(6)
        navigator = get_navigator(sim)
        for _ in range(20):
            sim.run_turn()
            for planet in sim.planets:
                for job in _government_jobs(planet):
                    assert job.destination is not planet
                    hops = len(navigator.route(planet, job.destination)) - 1
                    assert 1 <= hops <= GOVERNMENT_JOB_MAX_HOPS

    def test_job_payload_and_terms(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        job = _government_jobs(sim.planets[0])[0]
        assert (
            GOVERNMENT_JOB_UNITS_MIN
            <= job.payload.hold_units
            <= GOVERNMENT_JOB_UNITS_MAX
        )
        assert job.on_delivery == 0
        assert job.expires_turn == job.posted_turn + GOVERNMENT_JOB_TTL
        assert job.status is ContractStatus.OPEN

    def test_advance_is_the_leg_fuel_plus_the_margin(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        for planet in sim.planets:
            for job in _government_jobs(planet):
                assert job.advance == _expected_advance(sim, planet, job.destination)
                assert job.advance > 0

    def test_counts_every_post(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        assert sim.contracts_posted == 4 * GOVERNMENT_JOBS_PER_PLANET
        refresh_government_jobs(sim)
        assert sim.contracts_posted == 4 * GOVERNMENT_JOBS_PER_PLANET

    def test_a_lone_planet_posts_nothing(self) -> None:
        sim = _chain_sim(1)
        sim.run_turn()
        assert _government_jobs(sim.planets[0]) == []
        assert sim.contracts_posted == 0

    def test_an_expired_job_is_re_rolled(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        first = _government_jobs(sim.planets[0])[0]

        for _ in range(GOVERNMENT_JOB_TTL + 1):
            sim.run_turn()

        assert first.status is ContractStatus.EXPIRED
        replacements = _government_jobs(sim.planets[0])
        assert len(replacements) == GOVERNMENT_JOBS_PER_PLANET
        assert replacements[0] is not first
        assert sim.contracts_expired >= 1


class TestFlyingAJob:
    def test_advance_is_paid_at_departure_and_the_lot_is_delivered(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        origin = sim.planets[0]
        job = _government_jobs(origin)[0]

        ship = Ship("Hauler", sim, origin, initial_money=0)
        sim.ships.append(ship)
        origin.add_ship(ship)
        ship.fuel = ship.fuel_capacity
        # Exactly enough hold for this lot. With no room for riders the brain
        # has no reason to break the journey, so it is flown by hand as the
        # module docstring says.
        ship.cargo_capacity = job.payload.hold_units

        assert origin.contracts.accept(job, ship)
        assert job.status is ContractStatus.ACCEPTED
        assert ship.money == 0
        assert sim.government_payouts == 0

        assert ship.start_journey(job.destination, resuming=True)
        assert job.status is ContractStatus.LOADED
        assert ship.money == job.advance
        assert sim.government_payouts == job.advance

        while ship.destination is not None:
            ship.update_journey()

        assert job.status is ContractStatus.DELIVERED
        assert job not in ship.contracts
        assert ship.money == job.advance  # on_delivery is 0
        assert sim.government_payouts == job.advance
        assert sim.contracts_delivered == 1

    def test_a_taken_job_is_replaced_the_next_turn(self) -> None:
        sim = _chain_sim(4)
        sim.run_turn()
        origin = sim.planets[0]
        job = _government_jobs(origin)[0]

        ship = Ship("Hauler", sim, origin, initial_money=0)
        sim.ships.append(ship)
        origin.add_ship(ship)
        ship.fuel = ship.fuel_capacity
        assert origin.contracts.accept(job, ship)
        assert _government_jobs(origin) == []
        # The brain releases a contract that is merely accepted at the top of
        # its docked turn, so the job has to be loaded to count as taken.
        assert ship.start_journey(job.destination)
        assert job.status is ContractStatus.LOADED

        sim.run_turn()
        replacements = _government_jobs(origin)
        assert len(replacements) == GOVERNMENT_JOBS_PER_PLANET
        assert replacements[0] is not job
