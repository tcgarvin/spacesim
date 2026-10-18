"""Integration tests for tool and facility economy."""

from unittest.mock import patch

from spacesim2.core.commands import ProcessCommand
from spacesim2.core.simulation import Simulation


class TestBootstrapPath:
    """Actors can bootstrap from nothing to metal tools."""

    def test_harvest_wood_works_without_tools(self):
        """harvest_wood, the bootstrap entry point, needs no tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        can_execute = actor.can_execute_process("harvest_wood")
        assert can_execute, "Should be able to harvest wood without tools"

    def test_make_simple_tools_wood_works_without_tools(self):
        """make_simple_tools_wood needs no tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        wood = sim.commodity_registry.get_commodity("wood")
        actor.inventory.add_commodity(wood, 10)

        can_execute = actor.can_execute_process("make_simple_tools_wood")
        assert can_execute, (
            "Should be able to make simple tools from wood without existing tools"
        )

    def test_build_smelting_facility_requires_tools(self):
        """build_smelting_facility needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        building_materials = sim.commodity_registry.get_commodity(
            "simple_building_materials"
        )
        actor.inventory.add_commodity(building_materials, 10)

        can_execute = actor.can_execute_process("build_smelting_facility")
        assert not can_execute, (
            "Should not be able to build smelting facility without tools"
        )

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("build_smelting_facility")
        assert can_execute, "Should be able to build smelting facility with tools"

    def test_build_metalworking_facility_requires_tools(self):
        """build_metalworking_facility needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        building_materials = sim.commodity_registry.get_commodity(
            "simple_building_materials"
        )
        actor.inventory.add_commodity(building_materials, 10)

        can_execute = actor.can_execute_process("build_metalworking_facility")
        assert not can_execute, (
            "Should not be able to build metalworking facility without tools"
        )

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("build_metalworking_facility")
        assert can_execute, "Should be able to build metalworking facility with tools"

    def test_make_simple_tools_without_tools(self):
        """make_simple_tools needs a metalworking facility but no tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        common_metal = sim.commodity_registry.get_commodity("common_metal")
        metalworking_facility = sim.commodity_registry.get_commodity(
            "metalworking_facility"
        )

        actor.inventory.add_commodity(common_metal, 10)
        actor.inventory.add_commodity(metalworking_facility, 1)

        can_execute = actor.can_execute_process("make_simple_tools")
        assert can_execute, "Should be able to make simple tools without existing tools"

    def test_refine_common_metal_without_tools(self):
        """refine_common_metal needs a smelting facility but no tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        common_metal_ore = sim.commodity_registry.get_commodity("common_metal_ore")
        smelting_facility = sim.commodity_registry.get_commodity("smelting_facility")

        actor.inventory.add_commodity(common_metal_ore, 10)
        actor.inventory.add_commodity(smelting_facility, 1)

        can_execute = actor.can_execute_process("refine_common_metal")
        assert can_execute, "Should be able to refine common metal without tools"

    def test_full_bootstrap_path(self):
        """The complete bootstrap path from nothing to metal tools runs.

        Path: harvest_wood -> make_simple_tools_wood
        -> make_building_materials_wood -> build_smelting_facility
        -> mine_common_metal_ore -> refine_common_metal
        -> build_metalworking_facility -> make_simple_tools
        """
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        # Skill checks always pass and the actor's land is perfect.
        from spacesim2.core.land import Land

        actor.land = Land.default()
        with (
            patch("spacesim2.core.skill.SkillCheck.success_check", return_value=True),
            patch("spacesim2.core.commands.random.random", return_value=0.5),
        ):
            # 4 wood for tools, the rest for building materials.
            for _ in range(20):
                ProcessCommand("harvest_wood").execute(actor)

            wood = sim.commodity_registry.get_commodity("wood")
            assert actor.inventory.get_quantity(wood) >= 4, "Should have harvested wood"

            ProcessCommand("make_simple_tools_wood").execute(actor)
            simple_tools = sim.commodity_registry.get_commodity("simple_tools")
            assert actor.inventory.get_quantity(simple_tools) >= 1, (
                "Should have wood tools"
            )

            for _ in range(10):
                ProcessCommand("make_building_materials_wood").execute(actor)

            building_materials = sim.commodity_registry.get_commodity(
                "simple_building_materials"
            )
            assert actor.inventory.get_quantity(building_materials) >= 5, (
                "Should have building materials"
            )

            ProcessCommand("build_smelting_facility").execute(actor)
            smelting_facility = sim.commodity_registry.get_commodity(
                "smelting_facility"
            )
            assert actor.inventory.get_quantity(smelting_facility) >= 1, (
                "Should have built smelting facility"
            )

            for _ in range(20):
                ProcessCommand("mine_common_metal_ore").execute(actor)

            common_metal_ore = sim.commodity_registry.get_commodity("common_metal_ore")
            assert actor.inventory.get_quantity(common_metal_ore) >= 3, (
                "Should have mined ore"
            )

            while actor.inventory.get_quantity(common_metal_ore) >= 3:
                ProcessCommand("refine_common_metal").execute(actor)

            common_metal = sim.commodity_registry.get_commodity("common_metal")
            assert actor.inventory.get_quantity(common_metal) >= 2, (
                "Should have refined metal"
            )

            for _ in range(10):
                ProcessCommand("make_building_materials_wood").execute(actor)

            ProcessCommand("build_metalworking_facility").execute(actor)
            metalworking_facility = sim.commodity_registry.get_commodity(
                "metalworking_facility"
            )
            assert actor.inventory.get_quantity(metalworking_facility) >= 1, (
                "Should have built metalworking facility"
            )

            ProcessCommand("make_simple_tools").execute(actor)
            assert actor.inventory.get_quantity(simple_tools) >= 1, (
                "Should have bootstrapped to metal tools!"
            )


class TestToolRequirements:
    """Tool requirements are enforced for the right processes."""

    def test_mine_common_metal_ore_requires_tools(self):
        """mine_common_metal_ore needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        can_execute = actor.can_execute_process("mine_common_metal_ore")
        assert not can_execute, (
            "Should not be able to mine common metal ore without tools"
        )

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("mine_common_metal_ore")
        assert can_execute, "Should be able to mine common metal ore with tools"

    def test_mine_nova_fuel_ore_requires_tools(self):
        """mine_nova_fuel_ore needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]

        can_execute = actor.can_execute_process("mine_nova_fuel_ore")
        assert not can_execute, "Should not be able to mine nova fuel ore without tools"

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("mine_nova_fuel_ore")
        assert can_execute, "Should be able to mine nova fuel ore with tools"

    def test_make_clothing_requires_tools(self):
        """make_clothing needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]
        fiber = sim.commodity_registry.get_commodity("fiber")
        actor.inventory.add_commodity(fiber, 10)

        can_execute = actor.can_execute_process("make_clothing")
        assert not can_execute, "Should not be able to make clothing without tools"

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("make_clothing")
        assert can_execute, "Should be able to make clothing with tools"

    def test_refine_nova_fuel_requires_tools(self):
        """refine_nova_fuel needs tools."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]
        nova_fuel_ore = sim.commodity_registry.get_commodity("nova_fuel_ore")
        actor.inventory.add_commodity(nova_fuel_ore, 10)

        can_execute = actor.can_execute_process("refine_nova_fuel")
        assert not can_execute, "Should not be able to refine nova fuel without tools"

        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        actor.inventory.add_commodity(simple_tools, 1)
        can_execute = actor.can_execute_process("refine_nova_fuel")
        assert can_execute, "Should be able to refine nova fuel with tools"


class TestFacilityRequirements:
    """Facility requirements are enforced."""

    def test_refine_common_metal_requires_smelting_facility(self):
        """refine_common_metal needs a smelting facility."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]
        common_metal_ore = sim.commodity_registry.get_commodity("common_metal_ore")
        actor.inventory.add_commodity(common_metal_ore, 10)

        can_execute = actor.can_execute_process("refine_common_metal")
        assert not can_execute, "Should not be able to refine without smelting facility"

        smelting_facility = sim.commodity_registry.get_commodity("smelting_facility")
        actor.inventory.add_commodity(smelting_facility, 1)
        can_execute = actor.can_execute_process("refine_common_metal")
        assert can_execute, "Should be able to refine with smelting facility"

    def test_make_simple_tools_requires_metalworking_facility(self):
        """make_simple_tools needs a metalworking facility."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )

        actor = sim.actors[0]
        common_metal = sim.commodity_registry.get_commodity("common_metal")
        actor.inventory.add_commodity(common_metal, 10)

        can_execute = actor.can_execute_process("make_simple_tools")
        assert not can_execute, (
            "Should not be able to make tools without metalworking facility"
        )

        metalworking_facility = sim.commodity_registry.get_commodity(
            "metalworking_facility"
        )
        actor.inventory.add_commodity(metalworking_facility, 1)
        can_execute = actor.can_execute_process("make_simple_tools")
        assert can_execute, "Should be able to make tools with metalworking facility"
