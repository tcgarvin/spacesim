"""A doubled process run consumes doubled inputs or does not double.

The skill multiplier used to remove ``quantity * 2`` inputs with an
unchecked ``remove_commodity``, which removes nothing when short, while the
output loop still added the doubled quantity. Half of all cooks with 4-7
biomass produced 4 food from nothing.
"""

from unittest.mock import patch

import pytest

from spacesim2.core.actor import ActorType
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.simulation import Simulation


@pytest.fixture
def cook():
    sim = Simulation()
    sim.setup_simple(
        num_planets=1, num_regular_actors=2, num_market_makers=0, num_ships=0
    )
    actor = next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)
    reg = sim.commodity_registry
    food = reg.get_commodity("food")
    biomass = reg.get_commodity("biomass")
    assert food is not None and biomass is not None
    for c in (food, biomass):
        qty = actor.inventory.get_available_quantity(c)
        if qty:
            actor.inventory.remove_commodity(c, qty)
    actor.planet.attributes.biomass = 1.0
    return actor, food, biomass


def _run_doubled(actor) -> bool:
    with (
        patch("spacesim2.core.skill.SkillCheck.success_check", return_value=True),
        patch("spacesim2.core.skill.SkillCheck.multiplier_check", return_value=True),
    ):
        return ProcessCommand("make_food").execute(actor)


def test_doubled_run_with_doubled_inputs_consumes_and_produces_double(cook):
    actor, food, biomass = cook
    actor.inventory.add_commodity(biomass, 8)
    assert _run_doubled(actor)
    assert actor.inventory.get_available_quantity(biomass) == 0
    assert actor.inventory.get_available_quantity(food) == 8


def test_doubled_run_without_doubled_inputs_falls_back_to_single_scale(cook):
    actor, food, biomass = cook
    actor.inventory.add_commodity(biomass, 5)
    assert _run_doubled(actor)
    assert actor.inventory.get_available_quantity(biomass) == 1
    assert actor.inventory.get_available_quantity(food) == 4
