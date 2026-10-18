"""Shipyard, hull sections and hulls: data wiring and industrialist valuation.

``ship_hull`` is the only good that is tradable without being transportable,
so these tests also pin that distinction.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from spacesim.core.actor import Actor, ActorType
from spacesim.core.brains.industrialist import ENTRY_MARGIN, IndustrialistBrain
from spacesim.core.commands import PlaceSellOrderCommand, ProcessCommand
from spacesim.core.commodity import CommodityRegistry
from spacesim.core.drives.food_drive import FoodDrive, food_pantry_units
from spacesim.core.market import Market
from spacesim.core.process import ProcessDefinition, ProcessRegistry
from spacesim.core.simulation import Simulation

DATA_DIR = Path(__file__).parent.parent / "data"

# Reference asks seeded on the book, near the 100-planet trade prices for the
# goods a hull draws on. Everything the hull chain needs is quoted, so the
# make-or-buy imputation prices the recipe off the market instead of
# recursing to raw ore.
INPUT_ASKS = {
    "simple_building_materials": 12,
    "common_metal": 31,
    "simple_tools": 25,
    "heavy_machinery": 138,
    "electronics": 194,
    "computers": 502,
    "precision_parts": 65,
    "advanced_building_materials": 64,
    "ship_components": 420,
    "ship_hull_section": 780,
}

HULL_BID = 8000


def _registries() -> tuple[CommodityRegistry, ProcessRegistry]:
    commodity_registry = CommodityRegistry()
    commodity_registry.load_from_file(DATA_DIR / "commodities.yaml")
    process_registry = ProcessRegistry(commodity_registry)
    process_registry.load_from_file(DATA_DIR / "processes.yaml")
    return commodity_registry, process_registry


def _world() -> tuple[Actor, IndustrialistBrain, Market, CommodityRegistry]:
    """An industrialist on a planet whose market quotes every hull input."""
    commodity_registry, process_registry = _registries()
    sim = Mock()
    sim.current_turn = 0
    sim.commodity_registry = commodity_registry
    sim.process_registry = process_registry
    sim.data_logger = None

    market = Market()
    planet = Mock()
    planet.market = market

    brain = IndustrialistBrain()
    actor = Actor(
        name="HullBuilder",
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[FoodDrive(commodity_registry)],
        brain=brain,
        planet=planet,
        initial_money=10_000,
    )

    supplier = Actor(
        name="Supplier",
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[],
        brain=Mock(),
        planet=planet,
        initial_money=0,
    )
    for commodity_id, price in INPUT_ASKS.items():
        commodity = commodity_registry[commodity_id]
        supplier.inventory.add_commodity(commodity, 20)
        market.place_sell_order(supplier, commodity, 20, price)

    return actor, brain, market, commodity_registry


def _process(actor: Actor, process_id: str) -> ProcessDefinition:
    process = actor.sim.process_registry.get_process(process_id)
    assert process is not None
    return process


def _rest_hull_bid(
    market: Market, registry: CommodityRegistry, sim: "Simulation"
) -> Actor:
    """A buyer resting a bid for one hull, standing in for the organization."""
    planet = Mock()
    planet.market = market
    buyer = Actor(
        name="Organization",
        sim=sim,
        actor_type=ActorType.REGULAR,
        drives=[],
        brain=Mock(),
        planet=planet,
        initial_money=HULL_BID,
    )
    market.place_buy_order(buyer, registry["ship_hull"], 1, HULL_BID)
    return buyer


class TestHullTradability:
    def test_hull_is_tradable_but_not_transportable(self) -> None:
        registry, _ = _registries()
        hull = registry["ship_hull"]
        assert hull.tradable
        assert not hull.transportable

    def test_facilities_are_neither_tradable_nor_transportable(self) -> None:
        registry, _ = _registries()
        for facility_id in ("shipyard", "advanced_factory", "precision_forge"):
            facility = registry[facility_id]
            assert not facility.transportable
            assert not facility.tradable

    def test_every_transportable_good_is_tradable(self) -> None:
        registry, _ = _registries()
        for commodity in registry.all_commodities():
            assert commodity.tradable or not commodity.transportable

    def test_industrialist_offers_a_hull_but_not_a_facility(self) -> None:
        actor, brain, market, registry = _world()
        hull = registry["ship_hull"]
        shipyard = registry["shipyard"]
        actor.inventory.add_commodity(hull, 1)
        actor.inventory.add_commodity(shipyard, 1)
        _rest_hull_bid(market, registry, actor.sim)

        commands = brain._sell_command(actor, market, hull) + brain._sell_command(
            actor, market, shipyard
        )
        assert all(isinstance(command, PlaceSellOrderCommand) for command in commands)
        offered = {
            command.commodity_type.id
            for command in commands
            if isinstance(command, PlaceSellOrderCommand)
        }
        assert offered == {"ship_hull"}


class TestHullRecipeValuation:
    def test_a_hull_bid_makes_assembly_profitable(self) -> None:
        actor, brain, market, registry = _world()
        process = _process(actor, "make_ship_hull")
        assert brain._calculate_recipe_score(actor, market, process) == 0.0

        _rest_hull_bid(market, registry, actor.sim)

        cost = brain._impute_recipe_cost(actor, market, process, 0, frozenset(), {})
        value = brain._recipe_output_value(actor, market, process, {})
        score = brain._calculate_recipe_score(actor, market, process)
        assert value >= cost * ENTRY_MARGIN
        assert score > 0
        # The 5k-9k band the organization's payback assumes.
        assert 5_000 <= cost <= 9_000

    def test_hull_assembly_ranks_among_the_candidate_recipes(self) -> None:
        actor, brain, market, registry = _world()
        _rest_hull_bid(market, registry, actor.sim)

        ranked = {
            process.id: brain._calculate_recipe_score(actor, market, process)
            for process in actor.sim.process_registry.all_processes()
        }
        assert ranked["make_ship_hull"] > 0

    def test_a_section_maker_needs_no_hull_bid(self) -> None:
        """Sections carry their own demand, from the hull recipe's netback."""
        actor, brain, market, registry = _world()
        _rest_hull_bid(market, registry, actor.sim)
        process = _process(actor, "make_ship_hull_section")
        assert brain._calculate_recipe_score(actor, market, process) > 0


class TestShipyardConstruction:
    def test_the_hull_recipe_builds_a_shipyard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor, brain, market, registry = _world()
        assert (
            brain._get_build_process_for_facility(registry["shipyard"])
            == "build_shipyard"
        )

        _rest_hull_bid(market, registry, actor.sim)
        brain.chosen_recipe_id = "make_ship_hull"
        monkeypatch.setattr(
            IndustrialistBrain, "_should_reevaluate_recipe", lambda self: False
        )
        # Past the food and clothing gates, into the build branch.
        actor.inventory.add_commodity(registry["food"], 5)
        assert food_pantry_units(actor) >= 2
        actor.inventory.add_commodity(registry["clothing"], 1)
        build = _process(actor, "build_shipyard")
        for commodity, quantity in build.inputs.items():
            actor.inventory.add_commodity(commodity, quantity)
        actor.inventory.add_commodity(registry["simple_tools"], 1)

        command = brain.decide_economic_action(actor)
        assert isinstance(command, ProcessCommand)
        assert command.process_id == "build_shipyard"

    def test_the_shipyard_costs_more_than_an_advanced_factory(self) -> None:
        actor, brain, market, _ = _world()
        shipyard = brain._impute_recipe_cost(
            actor, market, _process(actor, "build_shipyard"), 0, frozenset(), {}
        )
        factory = brain._impute_recipe_cost(
            actor, market, _process(actor, "build_advanced_factory"), 0, frozenset(), {}
        )
        assert shipyard > factory
