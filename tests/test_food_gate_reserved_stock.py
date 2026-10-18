"""A hungry actor's own biomass sell order must not block it from eating.

Gathered biomass used to be listed for sale the same turn. The reserved
units counted toward the need gate's biomass check but not toward the
recipe's executability, so the actor neither gathered nor cooked.
"""

import pytest

from spacesim2.core.actor import ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import PlaceSellOrderCommand, ProcessCommand
from spacesim2.core.land import Land
from spacesim2.core.simulation import Simulation


def _clear(actor, commodity) -> None:
    qty = actor.inventory.get_available_quantity(commodity)
    if qty:
        assert actor.inventory.remove_commodity(commodity, qty)


@pytest.fixture
def world():
    sim = Simulation()
    sim.setup_simple(
        num_planets=1, num_regular_actors=4, num_market_makers=0, num_ships=0
    )
    actor = next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)
    # Pin the land: a barren draw makes hand food unviable and the brains
    # then skip the gate under test entirely. See brains/hand_food.py.
    actor.land = Land({"biomass": 1.0})
    reg = sim.commodity_registry
    food = reg.get_commodity("food")
    biomass = reg.get_commodity("biomass")
    assert food is not None and biomass is not None
    _clear(actor, food)
    _clear(actor, biomass)
    return sim, actor, food, biomass


@pytest.mark.parametrize("brain_cls", [ColonistBrain, IndustrialistBrain])
def test_reserved_biomass_does_not_satisfy_the_gather_gate(world, brain_cls):
    sim, actor, food, biomass = world
    actor.brain = brain_cls()
    actor.inventory.add_commodity(biomass, 4)
    assert actor.inventory.reserve_commodity(biomass, 2)
    assert actor.inventory.get_quantity(biomass) == 4
    assert not actor.can_execute_process("make_food")
    action = actor.brain.decide_economic_action(actor)
    assert isinstance(action, ProcessCommand)
    assert action.process_id == "gather_biomass"


@pytest.mark.parametrize("brain_cls", [ColonistBrain, IndustrialistBrain])
def test_one_food_batch_of_biomass_is_not_sold(world, brain_cls):
    sim, actor, food, biomass = world
    actor.brain = brain_cls()
    actor.inventory.add_commodity(biomass, 4)
    commands = actor.brain.decide_market_actions(actor)
    listed = [
        c
        for c in commands
        if isinstance(c, PlaceSellOrderCommand) and c.commodity_type is biomass
    ]
    assert listed == []
    actor.inventory.add_commodity(biomass, 3)
    commands = actor.brain.decide_market_actions(actor)
    listed = [
        c
        for c in commands
        if isinstance(c, PlaceSellOrderCommand) and c.commodity_type is biomass
    ]
    assert sum(c.quantity for c in listed) == 3
