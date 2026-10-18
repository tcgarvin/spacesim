"""Tests for land: per-actor extraction coefficients drawn per planet."""

import math
import statistics
from unittest.mock import patch

import pytest

from spacesim.core.actor import Actor, ActorType
from spacesim.core.actor_brain import ActorBrain
from spacesim.core.commands import ProcessCommand
from spacesim.core.land import (
    LANDS_PER_PLANET,
    Land,
    NoFreeLandError,
    draw_land,
    generate_lands,
    sample_coefficient,
)
from spacesim.core.market import Market
from spacesim.core.planet import Planet
from spacesim.core.planet_attributes import RESOURCE_ATTRIBUTES, PlanetAttributes
from spacesim.core.simulation import DEFAULT_LANDS_PER_PLANET, Simulation


class TestLand:
    def test_missing_resource_is_fully_available(self):
        assert Land({"wood": 0.3}).get_availability("biomass") == 1.0
        assert Land.default().get_availability("wood") == 1.0

    def test_out_of_range_coefficient_raises(self):
        with pytest.raises(ValueError):
            Land({"wood": 1.5})

    def test_at_mean_copies_every_planet_attribute(self):
        attributes = PlanetAttributes(biomass=0.4, wood=0.7)
        land = Land.at_mean(attributes)
        assert land.get_availability("biomass") == 0.4
        assert land.get_availability("wood") == 0.7
        assert set(land.to_dict()) == set(RESOURCE_ATTRIBUTES)


class TestSampleCoefficient:
    def test_infinite_concentration_returns_the_mean(self):
        assert sample_coefficient(0.37, math.inf) == 0.37

    def test_degenerate_means_return_themselves(self):
        assert sample_coefficient(0.0, 3.0) == 0.0
        assert sample_coefficient(1.0, 3.0) == 1.0

    def test_draws_stay_in_range_and_average_the_mean(self):
        draws = [sample_coefficient(0.3, 4.0) for _ in range(4000)]
        assert all(0.0 <= d <= 1.0 for d in draws)
        assert statistics.fmean(draws) == pytest.approx(0.3, abs=0.03)

    def test_low_concentration_spreads_wider_than_high(self):
        wide = statistics.pstdev([sample_coefficient(0.5, 0.5) for _ in range(2000)])
        tight = statistics.pstdev([sample_coefficient(0.5, 50.0) for _ in range(2000)])
        assert wide > 3 * tight

    def test_draws_below_the_barren_floor_snap_to_zero(self):
        """A denormal draw would overflow the replacement-cost division."""
        with patch("spacesim.core.land.random.betavariate", return_value=1e-310):
            assert sample_coefficient(0.3, 0.5) == 0.0
        with patch("spacesim.core.land.random.betavariate", return_value=0.02):
            assert sample_coefficient(0.3, 0.5) == 0.02

    def test_invalid_arguments_raise(self):
        with pytest.raises(ValueError):
            sample_coefficient(1.2, 3.0)
        with pytest.raises(ValueError):
            sample_coefficient(0.5, 0.0)


class TestGenerateLands:
    def test_default_attributes_give_penalty_free_lands(self):
        lands = generate_lands(PlanetAttributes(), 5)
        assert len(lands) == 5
        assert all(
            land.get_availability(r) == 1.0
            for land in lands
            for r in RESOURCE_ATTRIBUTES
        )

    def test_unset_concentration_pins_every_land_to_the_mean(self):
        attributes = PlanetAttributes(biomass=0.6)
        assert all(
            land.get_availability("biomass") == 0.6
            for land in generate_lands(attributes, 20)
        )

    def test_concentration_spreads_draws_around_the_mean(self):
        attributes = PlanetAttributes(biomass=0.6, land_concentration={"biomass": 2.0})
        draws = [draw_land(attributes).get_availability("biomass") for _ in range(2000)]
        assert statistics.fmean(draws) == pytest.approx(0.6, abs=0.03)
        assert statistics.pstdev(draws) > 0.1

    def test_negative_count_raises(self):
        with pytest.raises(ValueError):
            generate_lands(PlanetAttributes(), -1)


class TestPlanetAttributesConcentration:
    def test_generate_random_rolls_a_concentration_per_resource(self):
        attributes = PlanetAttributes.generate_random()
        assert set(attributes.land_concentration) == set(RESOURCE_ATTRIBUTES)
        assert all(c > 0.0 for c in attributes.land_concentration.values())

    def test_unknown_resource_or_bad_concentration_raises(self):
        with pytest.raises(ValueError):
            PlanetAttributes(land_concentration={"unobtainium": 2.0})
        with pytest.raises(ValueError):
            PlanetAttributes(land_concentration={"wood": 0.0})

    def test_to_dict_excludes_concentration(self):
        attributes = PlanetAttributes(land_concentration={"wood": 2.0})
        assert "land_concentration" not in attributes.to_dict()


def _actor(sim: Simulation, actor_type: ActorType) -> Actor:
    return Actor(
        name=actor_type.value,
        sim=sim,
        actor_type=actor_type,
        drives=[],
        brain=ActorBrain(),
    )


class TestPlanetPool:
    def test_planet_generates_the_pool_at_construction(self):
        planet = Planet("P", Market())
        assert len(planet.free_lands) == LANDS_PER_PLANET

    def test_claim_removes_from_the_pool(self):
        planet = Planet("P", Market(), num_lands=3)
        claimed = {id(planet.claim_land()) for _ in range(3)}
        assert len(claimed) == 3
        assert planet.free_lands == []

    def test_exhausted_pool_raises(self):
        planet = Planet("P", Market(), num_lands=0)
        with pytest.raises(NoFreeLandError):
            planet.claim_land()

    def test_regular_actor_claims_on_placement_and_service_does_not(self):
        sim = Simulation()
        attributes = PlanetAttributes(wood=0.5)
        planet = Planet("P", Market(), attributes=attributes, num_lands=2)

        regular = _actor(sim, ActorType.REGULAR)
        service = _actor(sim, ActorType.SERVICE)
        planet.add_actor(regular)
        planet.add_actor(service)

        assert regular.land.get_availability("wood") == 0.5
        assert service.land == Land.default()
        assert len(planet.free_lands) == 1

    def test_setup_gives_every_regular_actor_its_own_land(self):
        sim = Simulation()
        sim.setup_simple(num_planets=2, num_regular_actors=6, num_market_makers=1)

        for planet in sim.planets:
            regulars = [a for a in planet.actors if a.claims_land]
            assert len(regulars) == 6
            assert len({id(a.land) for a in regulars}) == 6
            assert len(planet.free_lands) == DEFAULT_LANDS_PER_PLANET - 6
            for actor in regulars:
                for resource in RESOURCE_ATTRIBUTES:
                    assert 0.0 <= actor.land.get_availability(resource) <= 1.0

    def test_more_regular_actors_than_lands_fails_loudly(self):
        sim = Simulation()
        planet = Planet("P", Market(), num_lands=2)
        for _ in range(2):
            planet.add_actor(_actor(sim, ActorType.REGULAR))
        with pytest.raises(NoFreeLandError):
            planet.add_actor(_actor(sim, ActorType.REGULAR))


class TestLandDrivesExtraction:
    def _sim_actor(self) -> tuple[Simulation, Actor]:
        sim = Simulation()
        sim.setup_simple(num_planets=1, num_regular_actors=1, num_market_makers=0)
        actor = sim.actors[0]
        actor.improve_skill("agriculture", 10.0)
        return sim, actor

    def test_execution_reads_the_land_not_the_planet(self):
        """A rich planet mean does not help an actor on poor land."""
        sim, actor = self._sim_actor()
        actor.planet.attributes = PlanetAttributes(biomass=1.0)
        actor.land = Land({"biomass": 0.25})

        biomass = sim.commodity_registry["biomass"]
        before = actor.inventory.get_quantity(biomass)
        assert ProcessCommand("gather_biomass").execute(actor) is True
        assert actor.inventory.get_quantity(biomass) - before in (2, 4)

    def test_valuation_reads_the_land(self):
        sim, actor = self._sim_actor()
        actor.land = Land({"biomass": 0.4})
        process = sim.process_registry.get_process("gather_biomass")
        assert process is not None
        assert actor.brain._expected_yield_modifier(actor, process) == 0.4


class TestZeroMeanLand:
    """A planet mean of exactly 0.0 yields barren land, never an error."""

    def test_sample_coefficient_at_zero_mean(self):
        for concentration in (0.5, 2.0, 50.0):
            assert sample_coefficient(0.0, concentration) == 0.0

    def test_draws_at_zero_mean_are_all_zero(self):
        attributes = PlanetAttributes(
            biomass=0.0,
            land_concentration={resource: 2.0 for resource in RESOURCE_ATTRIBUTES},
        )
        lands = generate_lands(attributes, count=50)
        assert len(lands) == 50
        assert all(land.get_availability("biomass") == 0.0 for land in lands)

    def test_planet_lands_at_zero_mean(self):
        planet = Planet(
            "Barren",
            Market(),
            attributes=PlanetAttributes(
                biomass=0.0,
                land_concentration={resource: 2.0 for resource in RESOURCE_ATTRIBUTES},
            ),
            num_lands=10,
        )
        assert all(
            land.get_availability("biomass") == 0.0 for land in planet.free_lands
        )
