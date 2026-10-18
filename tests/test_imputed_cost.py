"""Imputed make-cost respects local planet resource availability.

The make branch of ``_imputed_unit_cost`` must price extraction at its
expected yield on this planet, not the recipe's nominal yield. Otherwise
agents on resource-poor planets believe extraction inputs are cheap and
downstream industry such as fuel refining is sited on ore-poor planets.
"""

import math
from unittest.mock import Mock

import pytest

from spacesim2.core.actor import Actor
from spacesim2.core.actor_brain import GOVERNMENT_WAGE, ActorBrain
from spacesim2.core.commodity import CommodityDefinition, Inventory
from spacesim2.core.land import Land
from spacesim2.core.planet_attributes import PlanetAttributes
from spacesim2.core.process import ProcessDefinition, ResourceAttribute


def _wire_producer_index(sim_mock):
    """Derive the mock's get_processes_producing from all_processes.return_value.

    Evaluated lazily, so tests may set the list after the fixture runs.
    Mirrors the id-keyed producer index in ProcessRegistry.
    """
    registry = sim_mock.process_registry
    registry.get_processes_producing.side_effect = lambda commodity: [
        p
        for p in registry.all_processes.return_value
        if any(out.id == commodity.id for out in p.outputs)
    ]


def _commodity(cid: str) -> Mock:
    c = Mock(spec=CommodityDefinition)
    c.id = cid
    c.transportable = True
    return c


def _actor(attributes: PlanetAttributes) -> Mock:
    """Actor holding a land at the planet mean of ``attributes``."""
    actor = Mock(spec=Actor)
    actor.sim = Mock()
    _wire_producer_index(actor.sim)
    actor.planet = Mock()
    actor.planet.attributes = attributes
    actor.land = Land.at_mean(attributes)
    actor.inventory = Mock(spec=Inventory)
    actor.inventory.has_quantity.return_value = False
    return actor


def _mining_process(ore: Mock, effect: str, out_qty: int = 1) -> Mock:
    """Zero-input extraction, so the imputed recipe cost is one turn of labor."""
    process = Mock(spec=ProcessDefinition)
    process.id = "mine_nova_fuel_ore"
    process.inputs = {}
    process.outputs = {ore: out_qty}
    process.tools_required = []
    process.facilities_required = []
    process.upkeep = {}
    process.capital = {}
    process.resource_attribute = ResourceAttribute(
        commodity="nova_fuel_ore", effect=effect
    )
    return process


def _dead_market() -> Mock:
    """No asks and no trade history, which forces the make branch."""
    market = Mock()
    market.get_bid_ask_spread.return_value = (None, None)
    market.has_price_signal.return_value = False
    market.get_avg_price.return_value = 10
    return market


def _impute(brain: ActorBrain, actor: Mock, ore: Mock) -> float:
    return brain._imputed_unit_cost(actor, _dead_market(), ore, 0, frozenset(), {})


@pytest.fixture
def brain() -> ActorBrain:
    return ActorBrain()


class TestAttributeScaledImputation:
    def test_success_effect_scales_cost_by_inverse_availability(self, brain):
        """At availability 0.1, 90% of mining turns fail, so ore costs 10x."""
        ore = _commodity("nova_fuel_ore")
        actor = _actor(PlanetAttributes(nova_fuel_ore=1.0))
        actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="success")
        ]
        rich_cost = _impute(brain, actor, ore)

        poor_actor = _actor(PlanetAttributes(nova_fuel_ore=0.1))
        poor_actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="success")
        ]
        poor_cost = _impute(brain, poor_actor, ore)

        assert rich_cost == pytest.approx(GOVERNMENT_WAGE)
        assert poor_cost / rich_cost == pytest.approx(10.0)

    def test_output_effect_scales_cost_the_same_way(self, brain):
        """Reduced yield divides expected unit cost by availability, like success."""
        ore = _commodity("nova_fuel_ore")
        actor = _actor(PlanetAttributes(nova_fuel_ore=0.25))
        actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="output", out_qty=2)
        ]

        cost = _impute(brain, actor, ore)

        # labor 10 / (2 out * 0.25 attr) = 20
        assert cost == pytest.approx(GOVERNMENT_WAGE / (2 * 0.25))

    def test_zero_availability_makes_recipe_nonviable(self, brain):
        """Availability 0.0 makes the recipe cost inf rather than dividing by zero."""
        ore = _commodity("nova_fuel_ore")
        actor = _actor(PlanetAttributes(nova_fuel_ore=0.0))
        actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="success")
        ]

        assert math.isinf(_impute(brain, actor, ore))

    def test_zero_availability_still_buys_from_a_live_ask(self, brain):
        """Absent local resources leave the buy branch untouched."""
        ore = _commodity("nova_fuel_ore")
        actor = _actor(PlanetAttributes(nova_fuel_ore=0.0))
        actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="success")
        ]
        market = _dead_market()
        market.get_bid_ask_spread.return_value = (None, 7)

        cost = brain._imputed_unit_cost(actor, market, ore, 0, frozenset(), {})

        assert cost == pytest.approx(7.0)

    def test_default_attributes_leave_cost_unscaled(self, brain):
        """Default all-1.0 attributes apply no scaling."""
        ore = _commodity("nova_fuel_ore")
        actor = _actor(PlanetAttributes())
        actor.sim.process_registry.all_processes.return_value = [
            _mining_process(ore, effect="success")
        ]

        assert _impute(brain, actor, ore) == pytest.approx(GOVERNMENT_WAGE)

    def test_process_without_resource_attribute_is_unscaled(self, brain):
        """Non-extraction recipes are untouched even on a poor planet."""
        good = _commodity("simple_tools")
        actor = _actor(PlanetAttributes(nova_fuel_ore=0.1))
        process = _mining_process(good, effect="success")
        process.resource_attribute = None
        actor.sim.process_registry.all_processes.return_value = [process]

        assert _impute(brain, actor, good) == pytest.approx(GOVERNMENT_WAGE)
