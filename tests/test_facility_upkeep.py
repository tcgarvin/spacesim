"""Facility upkeep: a per-run probability of consuming one unit of a good.

Covers YAML parsing and validation, the consume/fail/no-hit paths in
ProcessCommand, expected upkeep cost in recipe imputation, and industrialist
procurement of the upkeep good.
"""

from typing import List
from unittest.mock import Mock, patch

import pytest

from spacesim2.core.actor_brain import GOVERNMENT_WAGE, ActorBrain
from spacesim2.core.commands import PlaceBuyOrderCommand, ProcessCommand
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry, Inventory
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.process import ProcessDefinition, ProcessRegistry
from spacesim2.core.simulation import Simulation

from .helpers import get_actor

UPKEEP_PROBABILITY = 0.1


def _commodity(registry: CommodityRegistry, cid: str) -> CommodityDefinition:
    definition = CommodityDefinition(
        id=cid, name=cid.replace("_", " ").title(), transportable=True, description=cid
    )
    registry.add_commodity(definition)
    return definition


def _sim_with_upkeep_process() -> Simulation:
    """Simulation with one process whose runs may consume heavy machinery."""
    sim = Simulation()
    sim.commodity_registry = CommodityRegistry()
    ore = _commodity(sim.commodity_registry, "input_commodity")
    widget = _commodity(sim.commodity_registry, "output_commodity")
    machinery = _commodity(sim.commodity_registry, "heavy_machinery")
    _commodity(sim.commodity_registry, "common_metal")

    sim.process_registry = ProcessRegistry(sim.commodity_registry)
    sim.process_registry._processes["test_process"] = ProcessDefinition(
        id="test_process",
        name="Test Process",
        inputs={ore: 1},
        outputs={widget: 1},
        tools_required=[],
        facilities_required=[],
        labor=1,
        description="A test process with upkeep",
        upkeep={machinery: UPKEEP_PROBABILITY},
    )
    return sim


def _get(sim: Simulation, cid: str) -> CommodityDefinition:
    commodity = sim.commodity_registry.get_commodity(cid)
    assert commodity is not None
    return commodity


class TestUpkeepParsing:
    def test_yaml_upkeep_is_parsed(self, tmp_path) -> None:
        registry = CommodityRegistry()
        _commodity(registry, "widget")
        machinery = _commodity(registry, "heavy_machinery")

        path = tmp_path / "processes.yaml"
        path.write_text(
            "- id: make_widget\n"
            "  name: Make Widget\n"
            "  inputs: {}\n"
            "  outputs:\n"
            "    widget: 1\n"
            "  tools_required: []\n"
            "  facilities_required: []\n"
            "  labor: 1\n"
            "  description: Test\n"
            "  upkeep:\n"
            "    heavy_machinery: 0.01\n"
        )

        process_registry = ProcessRegistry(registry)
        process_registry.load_from_file(path)

        process = process_registry.get_process("make_widget")
        assert process is not None
        assert process.upkeep == {machinery: 0.01}

    def test_upkeep_defaults_to_empty(self, tmp_path) -> None:
        registry = CommodityRegistry()
        _commodity(registry, "widget")

        path = tmp_path / "processes.yaml"
        path.write_text(
            "- id: make_widget\n"
            "  name: Make Widget\n"
            "  inputs: {}\n"
            "  outputs:\n"
            "    widget: 1\n"
            "  tools_required: []\n"
            "  facilities_required: []\n"
            "  labor: 1\n"
            "  description: Test\n"
        )

        process_registry = ProcessRegistry(registry)
        process_registry.load_from_file(path)

        process = process_registry.get_process("make_widget")
        assert process is not None
        assert process.upkeep == {}

    def test_unknown_upkeep_commodity_is_skipped(self, tmp_path) -> None:
        registry = CommodityRegistry()
        _commodity(registry, "widget")

        path = tmp_path / "processes.yaml"
        path.write_text(
            "- id: make_widget\n"
            "  name: Make Widget\n"
            "  inputs: {}\n"
            "  outputs:\n"
            "    widget: 1\n"
            "  tools_required: []\n"
            "  facilities_required: []\n"
            "  labor: 1\n"
            "  description: Test\n"
            "  upkeep:\n"
            "    no_such_commodity: 0.01\n"
        )

        process_registry = ProcessRegistry(registry)
        process_registry.load_from_file(path)

        process = process_registry.get_process("make_widget")
        assert process is not None
        assert process.upkeep == {}

    @pytest.mark.parametrize("probability", [0.0, -0.1, 1.5])
    def test_probability_outside_the_unit_interval_is_rejected(
        self, probability: float
    ) -> None:
        registry = CommodityRegistry()
        widget = _commodity(registry, "widget")
        machinery = _commodity(registry, "heavy_machinery")

        with pytest.raises(ValueError, match="upkeep probability"):
            ProcessDefinition(
                id="make_widget",
                name="Make Widget",
                inputs={},
                outputs={widget: 1},
                tools_required=[],
                facilities_required=[],
                labor=1,
                description="Test",
                upkeep={machinery: probability},
            )

    def test_upkeep_is_not_a_precondition_to_run(self) -> None:
        """can_execute_process ignores upkeep, which is probabilistic."""
        sim = _sim_with_upkeep_process()
        actor = get_actor("Test Actor", sim, planet=Planet("Test Planet", Market()))
        actor.inventory.add_commodity(_get(sim, "input_commodity"), 1)

        assert actor.can_execute_process("test_process") is True


class TestUpkeepExecution:
    def test_hit_consumes_one_unit(self) -> None:
        sim = _sim_with_upkeep_process()
        actor = get_actor("Test Actor", sim, planet=Planet("Test Planet", Market()))
        actor.inventory.add_commodity(_get(sim, "input_commodity"), 5)
        actor.inventory.add_commodity(_get(sim, "heavy_machinery"), 2)

        with patch("spacesim2.core.commands.random.random", return_value=0.0):
            assert ProcessCommand("test_process").execute(actor) is True

        assert actor.inventory.get_quantity(_get(sim, "heavy_machinery")) == 1
        assert actor.inventory.get_quantity(_get(sim, "output_commodity")) == 1
        assert "upkeep" in actor.last_action

    def test_miss_consumes_nothing(self) -> None:
        sim = _sim_with_upkeep_process()
        actor = get_actor("Test Actor", sim, planet=Planet("Test Planet", Market()))
        actor.inventory.add_commodity(_get(sim, "input_commodity"), 5)
        actor.inventory.add_commodity(_get(sim, "heavy_machinery"), 2)

        with patch("spacesim2.core.commands.random.random", return_value=0.9):
            assert ProcessCommand("test_process").execute(actor) is True

        assert actor.inventory.get_quantity(_get(sim, "heavy_machinery")) == 2
        assert actor.inventory.get_quantity(_get(sim, "output_commodity")) == 1
        assert "upkeep" not in actor.last_action

    def test_hit_without_the_good_fails_with_no_side_effects(self) -> None:
        sim = _sim_with_upkeep_process()
        actor = get_actor("Test Actor", sim, planet=Planet("Test Planet", Market()))
        actor.inventory.add_commodity(_get(sim, "input_commodity"), 5)

        with patch("spacesim2.core.commands.random.random", return_value=0.0):
            assert ProcessCommand("test_process").execute(actor) is False

        assert actor.inventory.get_quantity(_get(sim, "input_commodity")) == 5
        assert actor.inventory.get_quantity(_get(sim, "output_commodity")) == 0

    def test_skill_multiplier_never_doubles_the_upkeep_draw(self) -> None:
        sim = _sim_with_upkeep_process()
        process = sim.process_registry.get_process("test_process")
        assert process is not None
        process.relevant_skills = ["manufacturing"]

        actor = get_actor(
            "Test Actor",
            sim,
            planet=Planet("Test Planet", Market()),
            initial_skills={"manufacturing": 5.0},
        )
        actor.inventory.add_commodity(_get(sim, "input_commodity"), 5)
        actor.inventory.add_commodity(_get(sim, "heavy_machinery"), 2)

        # 0.0 hits the upkeep draw, passes the skill check, and triggers the
        # output multiplier.
        with patch("spacesim2.core.commands.random.random", return_value=0.0):
            assert ProcessCommand("test_process").execute(actor) is True

        assert actor.inventory.get_quantity(_get(sim, "heavy_machinery")) == 1


class TestUpkeepCostImputation:
    def test_imputed_recipe_cost_includes_expected_upkeep(self) -> None:
        registry = CommodityRegistry()
        widget = _commodity(registry, "widget")
        machinery = _commodity(registry, "heavy_machinery")

        without_upkeep = ProcessDefinition(
            id="make_widget",
            name="Make Widget",
            inputs={},
            outputs={widget: 1},
            tools_required=[],
            facilities_required=[],
            labor=1,
            description="Test",
        )
        with_upkeep = ProcessDefinition(
            id="make_widget_upkeep",
            name="Make Widget With Upkeep",
            inputs={},
            outputs={widget: 1},
            tools_required=[],
            facilities_required=[],
            labor=1,
            description="Test",
            upkeep={machinery: UPKEEP_PROBABILITY},
        )

        machinery_ask = 70
        market = Mock()
        market.get_bid_ask_spread.return_value = (None, machinery_ask)

        actor = Mock()
        actor.inventory = Mock(spec=Inventory)
        actor.inventory.has_quantity.return_value = False

        brain = ActorBrain()
        base = brain._impute_recipe_cost(
            actor, market, without_upkeep, 0, frozenset(), {}
        )
        charged = brain._impute_recipe_cost(
            actor, market, with_upkeep, 0, frozenset(), {}
        )

        assert base == float(GOVERNMENT_WAGE)
        assert charged == pytest.approx(base + machinery_ask * UPKEEP_PROBABILITY)


class TestUpkeepProcurement:
    """The industrialist keeps one unit of its recipe's upkeep good on hand."""

    @staticmethod
    def _brain_and_actor(sim: Simulation):
        from spacesim2.core.brains.industrialist import IndustrialistBrain

        brain = IndustrialistBrain()
        brain.chosen_recipe_id = "test_process"
        planet = Planet("Test Planet", Market())
        actor = get_actor(
            "Industrialist", sim, brain=brain, planet=planet, initial_money=500
        )
        return brain, actor, planet.market

    def test_bids_for_a_missing_upkeep_good(self) -> None:
        sim = _sim_with_upkeep_process()
        brain, actor, market = self._brain_and_actor(sim)

        commands: List[object] = list(brain._get_recipe_trading_commands(actor, market))
        machinery = _get(sim, "heavy_machinery")
        bids = [
            c
            for c in commands
            if isinstance(c, PlaceBuyOrderCommand) and c.commodity_type is machinery
        ]

        assert len(bids) == 1
        assert bids[0].quantity == 1
        assert bids[0].price > 0

    def test_does_not_rebuy_an_upkeep_good_already_held(self) -> None:
        sim = _sim_with_upkeep_process()
        brain, actor, market = self._brain_and_actor(sim)
        machinery = _get(sim, "heavy_machinery")
        actor.inventory.add_commodity(machinery, 1)

        commands = brain._get_recipe_trading_commands(actor, market)

        assert not [
            c
            for c in commands
            if isinstance(c, PlaceBuyOrderCommand) and c.commodity_type is machinery
        ]

    def test_keeps_an_upkeep_good_it_also_produces(self) -> None:
        """A recipe whose output is its own upkeep good holds one unit back."""
        sim = _sim_with_upkeep_process()
        process = sim.process_registry.get_process("test_process")
        assert process is not None
        machinery = _get(sim, "heavy_machinery")
        process.outputs = {machinery: 1}

        brain, actor, market = self._brain_and_actor(sim)
        actor.inventory.add_commodity(machinery, 3)

        commands = brain._get_recipe_trading_commands(actor, market)
        sells = [
            c
            for c in commands
            if getattr(c, "commodity_type", None) is machinery
            and not isinstance(c, PlaceBuyOrderCommand)
        ]

        assert len(sells) == 1
        assert sells[0].quantity == 2
