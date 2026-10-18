"""Integration test: full tech tree from raw materials to T3 advanced goods.

A single actor on a planet with perfect resource availability produces every
commodity in the chain by running processes in order. Skill checks always
succeed and tools never degrade, so the test is deterministic.
"""

from unittest.mock import patch

from spacesim2.core.actor import Actor
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.simulation import Simulation


def _run(process_id: str, actor: Actor, times: int = 1) -> None:
    """Execute a process `times` times, asserting each run succeeds."""
    for i in range(times):
        result = ProcessCommand(process_id).execute(actor)
        assert result, f"Process '{process_id}' failed on attempt {i + 1}"


def _qty(commodity_id: str, actor: Actor, sim: Simulation) -> int:
    """Inventory quantity of a commodity by id."""
    commodity = sim.commodity_registry.get_commodity(commodity_id)
    assert commodity is not None, f"Commodity '{commodity_id}' not found in registry"
    return actor.inventory.get_quantity(commodity)


def _has(commodity_id: str, actor: Actor, sim: Simulation) -> bool:
    return _qty(commodity_id, actor, sim) >= 1


class TestFullTechTree:
    """The complete tech tree can be traversed from scratch."""

    def _setup(self):
        """One actor holding land with every resource coefficient at 1.0."""
        from spacesim2.core.land import Land

        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )
        actor = sim.actors[0]
        actor.land = Land.default()

        return sim, actor

    def test_full_tech_tree_path(self):
        """Walk the full production chain from raw gathering to T3 goods.

        Tiers:
          T0: raw gathering, no tools required
          T1: first industry: tools, facilities, basic goods
          T2: specialization: 4 new facilities, intermediates, consumer goods
          T3: advanced factory, computers, luxury goods
        """
        sim, actor = self._setup()

        # Skill checks always succeed with no multiplier, and tools never break.
        # multiplier_check must be patched too: an unpredicted 2x input
        # multiplier would exhaust resources mid-phase. random.random = 0.5
        # stays above the 0.01 tool-break threshold and resource-success rolls.
        with (
            patch("spacesim2.core.skill.SkillCheck.success_check", return_value=True),
            patch(
                "spacesim2.core.skill.SkillCheck.multiplier_check", return_value=False
            ),
            patch("spacesim2.core.commands.random.random", return_value=0.5),
        ):
            self._phase_t0_raw_gathering(sim, actor)
            self._phase_t1_basic_industry(sim, actor)
            self._phase_t2_facilities(sim, actor)
            self._phase_t2_production(sim, actor)
            self._phase_t2_consumer_goods(sim, actor)
            self._phase_t3_advanced(sim, actor)

    # -----------------------------------------------------------------------
    # T0: Raw gathering, no tools required
    # -----------------------------------------------------------------------

    def _phase_t0_raw_gathering(self, sim, actor):
        """Gather the T0 raw materials that need no tools."""
        _run("gather_biomass", actor, 30)
        assert _has("biomass", actor, sim)

        _run("harvest_wood", actor, 30)
        assert _has("wood", actor, sim)

        _run("gather_fiber", actor, 20)
        assert _has("fiber", actor, sim)

        # silica and rare_earth_ore need tools, so they are mined in T1.

    # -----------------------------------------------------------------------
    # T1: Basic industry
    # -----------------------------------------------------------------------

    def _phase_t1_basic_industry(self, sim, actor):
        """Produce the T1 goods: tools, facilities, chemicals, glass, fuel."""
        # Wood tools need no tools or facilities.
        _run("make_simple_tools_wood", actor, 3)
        assert _has("simple_tools", actor, sim), "Should have crafted wood tools"

        # With tools, mine the remaining raw ores.
        _run("mine_common_metal_ore", actor, 30)
        assert _has("common_metal_ore", actor, sim)

        _run("mine_nova_fuel_ore", actor, 20)
        assert _has("nova_fuel_ore", actor, sim)

        _run("mine_silica", actor, 20)
        assert _has("silica", actor, sim)

        _run("mine_rare_earth", actor, 20)
        assert _has("rare_earth_ore", actor, sim)

        _run("make_building_materials_wood", actor, 20)
        assert _has("simple_building_materials", actor, sim)

        _run("build_smelting_facility", actor, 1)
        assert _has("smelting_facility", actor, sim), (
            "Should have built smelting facility"
        )

        # Enough metal for facilities, tools, and later T2 work.
        _run("mine_common_metal_ore", actor, 60)
        _run("refine_common_metal", actor, 15)
        assert _has("common_metal", actor, sim)

        _run("make_building_materials_metal", actor, 5)

        _run("build_metalworking_facility", actor, 1)
        assert _has("metalworking_facility", actor, sim), (
            "Should have built metalworking facility"
        )

        # Metal tools need the metalworking facility.
        _run("make_simple_tools", actor, 3)
        assert _has("simple_tools", actor, sim)

        _run("refine_nova_fuel", actor, 5)
        assert _has("nova_fuel", actor, sim)

        _run("make_food", actor, 5)
        assert _has("food", actor, sim)

        _run("make_clothing", actor, 3)
        assert _has("clothing", actor, sim)

        _run("make_chemicals", actor, 10)
        assert _has("chemicals", actor, sim)

        _run("make_glass", actor, 5)
        assert _has("glass", actor, sim)

        _run("make_ship_supplies", actor, 3)
        assert _has("ship_supplies", actor, sim)

    # -----------------------------------------------------------------------
    # T2: Facility construction
    # -----------------------------------------------------------------------

    def _phase_t2_facilities(self, sim, actor):
        """Build all six T2 facilities.

        Total inputs across the 6 builds:
          building_materials: 30, 5 each
          common_metal: 9, as 2 + 3 + 2 + 2, plus 5 each for the chemical
            plant's and the farm's heavy_machinery input, 19 total
          glass: 4, as 2 + 2, needing 12 silica
          chemicals: 2, for chemistry_lab

        mine_common_metal_ore 120 -> refine_common_metal 30 nets 60 common_metal
        (90 ore consumed), covering the 30 for building materials plus the 19
        for facilities and machinery with margin.
        """
        _run("mine_common_metal_ore", actor, 120)
        _run("refine_common_metal", actor, 30)
        _run("make_building_materials_metal", actor, 30)

        # make_glass uses 3 silica each.
        _run("mine_silica", actor, 60)
        _run("make_glass", actor, 10)

        _run("gather_biomass", actor, 20)
        _run("make_chemicals", actor, 10)

        _run("build_textile_mill", actor, 1)
        assert _has("textile_mill", actor, sim), "Should have built textile mill"

        _run("build_chemistry_lab", actor, 1)
        assert _has("chemistry_lab", actor, sim), "Should have built chemistry lab"

        _run("build_precision_forge", actor, 1)
        assert _has("precision_forge", actor, sim), "Should have built precision forge"

        _run("build_electronics_workshop", actor, 1)
        assert _has("electronics_workshop", actor, sim), (
            "Should have built electronics workshop"
        )

        _run("make_heavy_machinery", actor, 2)
        assert _qty("heavy_machinery", actor, sim) >= 2, (
            "Should have made heavy machinery for the plant and the farm"
        )

        _run("build_chemical_plant", actor, 1)
        assert _has("chemical_plant", actor, sim), "Should have built chemical plant"

        _run("build_farm", actor, 1)
        assert _has("farm", actor, sim), "Should have built farm"

        _run("make_chemicals", actor, 1)
        farm_biomass_before = _qty("biomass", actor, sim)
        _run("farm_biomass", actor, 1)
        assert _qty("biomass", actor, sim) - farm_biomass_before == 64, (
            "farm_biomass should yield 64 biomass at full attribute"
        )

    # -----------------------------------------------------------------------
    # T2: Intermediate production
    # -----------------------------------------------------------------------

    def _phase_t2_production(self, sim, actor):
        """Produce the T2 intermediate goods."""
        _run("gather_fiber", actor, 20)
        _run("gather_biomass", actor, 10)
        _run("mine_common_metal_ore", actor, 20)
        _run("refine_common_metal", actor, 5)
        _run("mine_silica", actor, 10)
        _run("make_glass", actor, 5)
        _run("mine_rare_earth", actor, 20)
        _run("mine_nova_fuel_ore", actor, 10)
        _run("make_chemicals", actor, 10)

        _run("make_textiles", actor, 3)
        assert _has("textiles", actor, sim)

        _run("refine_chemicals", actor, 3)
        assert _has("refined_chemicals", actor, sim)

        # precision_parts take 3 metal per run. Electronics need 2x3, precision
        # tools 2x2, and ship parts 2x2, so 14 parts, which is 7 runs.
        _run("mine_common_metal_ore", actor, 40)
        _run("refine_common_metal", actor, 10)
        _run("make_precision_parts", actor, 10)
        assert _has("precision_parts", actor, sim)

        _run("refine_rare_earth", actor, 3)
        assert _has("rare_earth", actor, sim)

        _run("make_polymers", actor, 5)
        assert _has("polymers", actor, sim)

        _run("make_electronics", actor, 3)
        assert _has("electronics", actor, sim)

        _run("make_precision_tools", actor, 2)
        assert _has("precision_tools", actor, sim)

        _run("make_ship_parts", actor, 2)
        assert _has("ship_parts", actor, sim)

    # -----------------------------------------------------------------------
    # T2: Consumer goods
    # -----------------------------------------------------------------------

    def _phase_t2_consumer_goods(self, sim, actor):
        """Produce the T2 consumer goods."""
        _run("make_food", actor, 5)
        _run("gather_biomass", actor, 5)
        _run("refine_chemicals", actor, 3)
        _run("make_textiles", actor, 3)
        _run("make_polymers", actor, 3)
        _run("mine_silica", actor, 10)
        _run("make_glass", actor, 3)
        _run("make_building_materials_metal", actor, 3)

        # process_food takes 40 biomass + 1 chemicals -> 60 processed_food.
        _run("gather_biomass", actor, 6)
        _run("make_chemicals", actor, 1)
        food_before = _qty("food", actor, sim)
        _run("process_food", actor, 1)
        assert _qty("processed_food", actor, sim) >= 60
        assert _qty("food", actor, sim) == food_before, (
            "process_food must not consume plain food"
        )

        _run("make_quality_clothing", actor, 2)
        assert _has("quality_clothing", actor, sim)

        _run("make_medicine", actor, 2)
        assert _has("medicine", actor, sim)

        _run("make_prefab_housing", actor, 2)
        assert _has("prefab_housing", actor, sim)

    # -----------------------------------------------------------------------
    # T3: Advanced goods
    # -----------------------------------------------------------------------

    def _phase_t3_advanced(self, sim, actor):
        """Build the advanced factory and produce the T3 goods."""
        # T2 inputs across the T3 builds:
        #   build_advanced_factory: building_materials 5, precision_parts 3,
        #     electronics 2
        #   make_computers: electronics 2, precision_parts 1, polymers 1
        #   make_luxury_goods: rare_earth 1, textiles 1, glass 1
        #   make_advanced_medicine x2: medicine 1, electronics 1,
        #     refined_chemicals 2
        #   make_ship_components x2: precision_parts 2, electronics 1, polymers 1
        #   make_advanced_building_materials x2: metal 2, polymers 1, glass 1
        # Totals: electronics 7, precision_parts 8, polymers 4, rare_earth 8
        # including 7 for electronics. Everything is made with a margin so no
        # input runs out mid-phase.
        _run("mine_common_metal_ore", actor, 200)
        _run("refine_common_metal", actor, 50)  # 100 metal
        _run("make_building_materials_metal", actor, 15)
        _run("mine_silica", actor, 100)
        _run("make_glass", actor, 20)
        _run("mine_rare_earth", actor, 100)
        _run("refine_rare_earth", actor, 20)
        _run("mine_nova_fuel_ore", actor, 60)
        _run("gather_biomass", actor, 30)
        _run("make_chemicals", actor, 20)
        _run("refine_chemicals", actor, 10)
        _run("make_polymers", actor, 10)
        _run("make_precision_parts", actor, 15)
        _run("make_electronics", actor, 10)

        _run("build_advanced_factory", actor, 1)
        assert _has("advanced_factory", actor, sim), (
            "Should have built advanced factory"
        )

        _run("make_advanced_building_materials", actor, 2)
        assert _has("advanced_building_materials", actor, sim)

        _run("make_computers", actor, 2)
        assert _has("computers", actor, sim)

        _run("make_textiles", actor, 3)  # textiles for luxury goods
        _run("make_luxury_goods", actor, 2)
        assert _has("luxury_goods", actor, sim)

        _run("make_medicine", actor, 3)  # medicine for advanced medicine
        _run("refine_chemicals", actor, 3)
        _run("make_electronics", actor, 3)
        _run("make_advanced_medicine", actor, 2)
        assert _has("advanced_medicine", actor, sim)

        _run("make_ship_components", actor, 2)
        assert _has("ship_components", actor, sim)


class TestProcessedFoodRecipe:
    """process_food is an industrial biomass recipe, not a food consumer."""

    def _registry(self):
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )
        return sim

    def test_process_food_consumes_biomass_not_food(self):
        sim = self._registry()
        process = sim.process_registry.get_process("process_food")
        assert process is not None
        inputs = {c.id: q for c, q in process.inputs.items()}
        outputs = {c.id: q for c, q in process.outputs.items()}
        assert inputs == {"biomass": 40, "chemicals": 1}
        assert outputs == {"processed_food": 60}
        assert [f.id for f in process.facilities_required] == ["chemical_plant"]
        assert process.tools_required == []

    def test_no_process_consumes_food_to_make_processed_food(self):
        sim = self._registry()
        for process in sim.process_registry.all_processes():
            output_ids = {c.id for c in process.outputs}
            if "processed_food" not in output_ids:
                continue
            input_ids = {c.id for c in process.inputs}
            assert "food" not in input_ids, (
                f"{process.id} still consumes food to make processed food"
            )

    def test_chemical_plant_is_buildable_from_the_metal_bootstrap(self):
        sim = self._registry()
        build = sim.process_registry.get_process("build_chemical_plant")
        assert build is not None
        inputs = {c.id: q for c, q in build.inputs.items()}
        assert inputs == {
            "simple_building_materials": 5,
            "common_metal": 2,
            "heavy_machinery": 1,
        }
        assert [t.id for t in build.tools_required] == ["simple_tools"]
        assert build.facilities_required == []

    def test_process_food_declares_heavy_machinery_upkeep(self):
        sim = self._registry()
        process = sim.process_registry.get_process("process_food")
        assert process is not None
        upkeep = {c.id: p for c, p in process.upkeep.items()}
        assert upkeep == {"heavy_machinery": 0.01}

    def test_heavy_machinery_is_reachable_from_the_metal_bootstrap(self):
        """make_heavy_machinery needs only smelting + metalworking, no chemical plant."""
        sim = self._registry()
        make = sim.process_registry.get_process("make_heavy_machinery")
        assert make is not None
        inputs = {c.id: q for c, q in make.inputs.items()}
        assert inputs == {"common_metal": 5}
        assert [t.id for t in make.tools_required] == ["simple_tools"]
        assert [f.id for f in make.facilities_required] == ["metalworking_facility"]

    def test_every_required_facility_has_a_build_process(self):
        """Brains reach a facility only through _get_build_process_for_facility."""
        from spacesim2.core.actor_brain import ActorBrain

        sim = self._registry()
        brain = ActorBrain()
        for process in sim.process_registry.all_processes():
            for facility in process.facilities_required:
                build_id = brain._get_build_process_for_facility(facility)
                assert build_id is not None, (
                    f"{facility.id} (required by {process.id}) has no build process"
                )
                assert sim.process_registry.get_process(build_id) is not None


class TestFarmBiomassRecipe:
    """farm_biomass is a bulk gather at a farm, scaled by the biomass attribute."""

    def _registry(self):
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=1,
            num_market_makers=0,
            num_ships=0,
        )
        return sim

    def test_build_farm_is_buildable_from_the_metal_bootstrap(self):
        sim = self._registry()
        build = sim.process_registry.get_process("build_farm")
        assert build is not None
        inputs = {c.id: q for c, q in build.inputs.items()}
        assert inputs == {"simple_building_materials": 5, "heavy_machinery": 1}
        assert [t.id for t in build.tools_required] == ["simple_tools"]
        assert build.facilities_required == []

    def test_farm_biomass_declares_heavy_machinery_upkeep(self):
        sim = self._registry()
        process = sim.process_registry.get_process("farm_biomass")
        assert process is not None
        upkeep = {c.id: p for c, p in process.upkeep.items()}
        assert upkeep == {"heavy_machinery": 0.01}

    def test_farm_biomass_yields_64_at_full_attribute(self):
        """At biomass attribute 1.0, one run outputs the full base yield."""
        from unittest.mock import patch

        from spacesim2.core.land import Land

        sim = self._registry()
        actor = sim.actors[0]
        actor.land = Land({"biomass": 1.0})

        farm = sim.commodity_registry.get_commodity("farm")
        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        chemicals = sim.commodity_registry.get_commodity("chemicals")
        assert farm is not None and simple_tools is not None and chemicals is not None
        actor.inventory.add_commodity(farm, 1)
        actor.inventory.add_commodity(simple_tools, 1)
        actor.inventory.add_commodity(chemicals, 1)

        biomass = sim.commodity_registry.get_commodity("biomass")
        assert biomass is not None
        before = actor.inventory.get_quantity(biomass)

        with (
            patch("spacesim2.core.skill.SkillCheck.success_check", return_value=True),
            patch(
                "spacesim2.core.skill.SkillCheck.multiplier_check", return_value=False
            ),
        ):
            result = ProcessCommand("farm_biomass").execute(actor)

        assert result is True, f"Process failed with action: {actor.last_action}"
        assert actor.inventory.get_quantity(biomass) - before == 64

    def test_farm_biomass_scales_with_biomass_attribute(self):
        """The output effect scales yield like gather_biomass does."""
        from unittest.mock import patch

        from spacesim2.core.land import Land

        sim = self._registry()
        actor = sim.actors[0]
        actor.land = Land({"biomass": 0.25})

        farm = sim.commodity_registry.get_commodity("farm")
        simple_tools = sim.commodity_registry.get_commodity("simple_tools")
        chemicals = sim.commodity_registry.get_commodity("chemicals")
        assert farm is not None and simple_tools is not None and chemicals is not None
        actor.inventory.add_commodity(farm, 1)
        actor.inventory.add_commodity(simple_tools, 1)
        actor.inventory.add_commodity(chemicals, 1)

        biomass = sim.commodity_registry.get_commodity("biomass")
        assert biomass is not None
        before = actor.inventory.get_quantity(biomass)

        with (
            patch("spacesim2.core.skill.SkillCheck.success_check", return_value=True),
            patch(
                "spacesim2.core.skill.SkillCheck.multiplier_check", return_value=False
            ),
        ):
            result = ProcessCommand("farm_biomass").execute(actor)

        assert result is True, f"Process failed with action: {actor.last_action}"
        # Base output 64 * 0.25 = 16, matching gather_biomass's output scaling.
        assert actor.inventory.get_quantity(biomass) - before == 16
