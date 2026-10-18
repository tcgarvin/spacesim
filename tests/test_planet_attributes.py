"""Tests for planet attributes."""

from unittest.mock import patch

import pytest

from spacesim2.core.land import Land
from spacesim2.core.planet_attributes import (
    BIOMASS_DRAW_LOW,
    PlanetAttributes,
    _bimodal_sample,
)
from spacesim2.core.process import ResourceAttribute


class TestPlanetAttributes:
    """PlanetAttributes dataclass."""

    def test_default_attributes_are_all_one(self):
        """Default attributes are all 1.0, meaning no penalty."""
        attrs = PlanetAttributes()
        assert attrs.biomass == 1.0
        assert attrs.fiber == 1.0
        assert attrs.wood == 1.0
        assert attrs.common_metal_ore == 1.0
        assert attrs.nova_fuel_ore == 1.0
        assert attrs.simple_building_materials == 1.0

    def test_custom_attributes(self):
        """Constructor stores custom values."""
        attrs = PlanetAttributes(
            biomass=0.5,
            fiber=0.3,
            wood=0.8,
            common_metal_ore=0.0,
            nova_fuel_ore=1.0,
            simple_building_materials=0.6,
        )
        assert attrs.biomass == 0.5
        assert attrs.fiber == 0.3
        assert attrs.wood == 0.8
        assert attrs.common_metal_ore == 0.0
        assert attrs.nova_fuel_ore == 1.0
        assert attrs.simple_building_materials == 0.6

    def test_validation_rejects_values_above_one(self):
        """Attributes above 1.0 raise ValueError."""
        with pytest.raises(ValueError, match="biomass must be between"):
            PlanetAttributes(biomass=1.5)

    def test_validation_rejects_negative_values(self):
        """Negative attributes raise ValueError."""
        with pytest.raises(ValueError, match="wood must be between"):
            PlanetAttributes(wood=-0.1)

    def test_generate_random_produces_valid_attributes(self):
        """generate_random keeps every attribute in [0, 1]."""
        for _ in range(100):
            attrs = PlanetAttributes.generate_random()
            assert 0.0 <= attrs.biomass <= 1.0
            assert 0.0 <= attrs.fiber <= 1.0
            assert 0.0 <= attrs.wood <= 1.0
            assert 0.0 <= attrs.common_metal_ore <= 1.0
            assert 0.0 <= attrs.nova_fuel_ore <= 1.0
            assert 0.0 <= attrs.simple_building_materials <= 1.0

    def test_generate_random_respects_minimum_values(self):
        """generate_random keeps building materials >= 0.3.

        Biomass has no floor above zero: the draw clamps its negative tail
        to exactly 0.0 so some planets cannot grow food.
        """
        for _ in range(100):
            attrs = PlanetAttributes.generate_random()
            assert attrs.biomass >= 0.0
            assert attrs.simple_building_materials >= 0.3

    def test_get_availability_for_tracked_commodity(self):
        """get_availability returns the named attribute."""
        attrs = PlanetAttributes(biomass=0.5, nova_fuel_ore=0.8)
        assert attrs.get_availability("biomass") == 0.5
        assert attrs.get_availability("nova_fuel_ore") == 0.8

    def test_get_availability_for_untracked_commodity_returns_one(self):
        """Untracked commodities such as processed food return 1.0."""
        attrs = PlanetAttributes()
        assert attrs.get_availability("food") == 1.0
        assert attrs.get_availability("nonexistent") == 1.0

    def test_default_returns_all_ones(self):
        """PlanetAttributes.default() returns all 1.0."""
        attrs = PlanetAttributes.default()
        assert attrs.biomass == 1.0
        assert attrs.fiber == 1.0
        assert attrs.wood == 1.0
        assert attrs.common_metal_ore == 1.0
        assert attrs.nova_fuel_ore == 1.0
        assert attrs.simple_building_materials == 1.0

    def test_to_dict(self):
        """to_dict() includes every attribute, with defaults filled in."""
        attrs = PlanetAttributes(biomass=0.5, wood=0.7)
        d = attrs.to_dict()
        assert d["biomass"] == 0.5
        assert d["wood"] == 0.7
        assert d["fiber"] == 1.0
        assert len(d) == 8  # all 8 resource attributes


class TestBimodalSample:
    """_bimodal_sample helper."""

    def test_bimodal_sample_in_range(self):
        """Samples fall in one of the two ranges."""
        for _ in range(100):
            value = _bimodal_sample(0.0, 0.3, 0.7, 1.0)
            assert (0.0 <= value <= 0.3) or (0.7 <= value <= 1.0)


class TestResourceAttribute:
    """ResourceAttribute dataclass."""

    def test_valid_success_effect(self):
        """The success effect is accepted."""
        ra = ResourceAttribute(commodity="nova_fuel_ore", effect="success")
        assert ra.commodity == "nova_fuel_ore"
        assert ra.effect == "success"

    def test_valid_output_effect(self):
        """The output effect is accepted."""
        ra = ResourceAttribute(commodity="biomass", effect="output")
        assert ra.commodity == "biomass"
        assert ra.effect == "output"

    def test_invalid_effect_raises_error(self):
        """An unknown effect raises ValueError."""
        with pytest.raises(ValueError, match="effect must be one of"):
            ResourceAttribute(commodity="biomass", effect="invalid")


class TestSimulationIntegration:
    """Planet attributes in a running simulation."""

    def test_simulation_with_planet_attributes(self):
        """setup_simple gives every planet attributes."""
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=3,
            num_regular_actors=4,
            num_market_makers=1,
        )

        for planet in sim.planets:
            assert planet.attributes is not None
            assert isinstance(planet.attributes, PlanetAttributes)

    def test_different_planets_have_different_attributes(self):
        """Planets get independently generated attributes."""
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=5,
            num_regular_actors=4,
            num_market_makers=1,
        )

        # Five random planets are almost never all identical.
        attributes_sets = [
            (p.attributes.biomass, p.attributes.nova_fuel_ore) for p in sim.planets
        ]
        assert len(set(attributes_sets)) > 1


class TestProcessCommandIntegration:
    """ProcessCommand with planet attributes."""

    def test_process_with_output_effect_reduces_yield(self):
        """The output effect reduces yield on a low-availability planet."""
        from spacesim2.core.commands import ProcessCommand
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=2,
            num_market_makers=1,
        )

        actor = sim.actors[0]
        actor.land = Land({"biomass": 0.25})

        # High agriculture skill so the process succeeds.
        actor.improve_skill("agriculture", 10.0)

        biomass = sim.commodity_registry["biomass"]
        initial_biomass = actor.inventory.get_quantity(biomass)

        cmd = ProcessCommand("gather_biomass")
        result = cmd.execute(actor)

        assert result is True, f"Process failed with action: {actor.last_action}"

        new_biomass = actor.inventory.get_quantity(biomass)
        gained = new_biomass - initial_biomass
        # Base output 8 * 0.25 = 2, or 4 if the skill multiplier doubled it.
        assert gained in (2, 4), f"Expected 2 or 4 biomass, got {gained}"

    def test_process_with_success_effect_can_fail(self):
        """The success effect fails the process on a zero-availability planet."""
        import random

        from spacesim2.core.commands import ProcessCommand
        from spacesim2.core.simulation import Simulation

        random.seed(42)

        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=2,
            num_market_makers=1,
        )

        actor = sim.actors[0]
        actor.land = Land({"nova_fuel_ore": 0.0})

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)

        cmd = ProcessCommand("mine_nova_fuel_ore")
        result = cmd.execute(actor)

        assert result is False
        assert "insufficient planetary resources" in actor.last_action

    def test_process_without_resource_attribute_unaffected(self):
        """A process without resource_attribute ignores planet attributes."""
        from spacesim2.core.commands import ProcessCommand
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=2,
            num_market_makers=1,
        )

        actor = sim.actors[0]
        actor.land = Land({"biomass": 0.1})
        # The culinary skill check fails with probability 1 - rating below 1.0.
        actor.improve_skill("culinary", 10.0)

        biomass = sim.commodity_registry["biomass"]
        actor.inventory.add_commodity(biomass, 10)

        food = sim.commodity_registry["food"]
        initial_food = actor.inventory.get_quantity(food)

        cmd = ProcessCommand("make_food")
        result = cmd.execute(actor)

        assert result is True
        # make_food outputs 4 food; the skill multiplier may double it.
        gained = actor.inventory.get_quantity(food) - initial_food
        assert gained >= 4


def test_setup_guarantees_a_fuel_rich_planet():
    """Setup re-rolls one planet into the abundant fuel band when none lands there.

    An all-poor bimodal roll would strand every ship.
    """
    from spacesim2.core.simulation import Simulation

    for _ in range(30):
        sim = Simulation()
        sim.setup_simple(
            num_planets=2,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )
        assert any(
            p.attributes is not None and p.attributes.nova_fuel_ore >= 0.7
            for p in sim.planets
        )


class TestBiomassDrawClamping:
    """The biomass draw's negative tail clamps to exactly 0.0."""

    def _uniform_returning(self, biomass_draw):
        """random.uniform stand-in that fixes only the biomass draw."""

        def fake_uniform(low, high):
            if low == BIOMASS_DRAW_LOW:
                return biomass_draw
            return (low + high) / 2.0

        return fake_uniform

    def test_negative_draw_clamps_to_zero(self, monkeypatch):
        monkeypatch.setattr(
            "spacesim2.core.planet_attributes.random.uniform",
            self._uniform_returning(-0.15),
        )
        assert PlanetAttributes.generate_random().biomass == 0.0

    def test_positive_draw_passes_through(self, monkeypatch):
        monkeypatch.setattr(
            "spacesim2.core.planet_attributes.random.uniform",
            self._uniform_returning(0.42),
        )
        assert PlanetAttributes.generate_random().biomass == pytest.approx(0.42)

    def test_zero_share_matches_the_lower_bound(self):
        """The share of barren planets is -low / (1 - low)."""
        draws = 4000
        zeros = sum(
            1 for _ in range(draws) if PlanetAttributes.generate_random().biomass == 0.0
        )
        expected = -BIOMASS_DRAW_LOW / (1.0 - BIOMASS_DRAW_LOW)
        assert zeros / draws == pytest.approx(expected, abs=0.03)

    def test_zero_biomass_is_a_valid_attribute(self):
        attrs = PlanetAttributes(biomass=0.0)
        assert attrs.get_availability("biomass") == 0.0
        assert attrs.to_dict()["biomass"] == 0.0


def test_a_barren_planet_runs_without_raising():
    """A planet with no biomass at all survives a run end to end."""
    from spacesim2.core.simulation import Simulation

    real_generate = PlanetAttributes.generate_random
    calls = {"n": 0}

    def barren_first():
        attrs = real_generate()
        if calls["n"] == 0:
            attrs.biomass = 0.0
        calls["n"] += 1
        return attrs

    with patch.object(PlanetAttributes, "generate_random", side_effect=barren_first):
        sim = Simulation()
        sim.setup_simple(
            num_planets=2,
            num_regular_actors=10,
            num_market_makers=1,
            num_spaceport_operators=1,
            num_ships=1,
            lands_per_planet=20,
        )

    barren = sim.planets[0]
    assert barren.attributes.biomass == 0.0
    assert all(
        actor.land.get_availability("biomass") == 0.0
        for actor in barren.actors
        if actor.land.coefficients
    )

    for _ in range(30):
        sim.run_turn()
