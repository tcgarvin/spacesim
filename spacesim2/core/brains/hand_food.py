"""Is feeding yourself by hand worth a labor turn on this actor's land?

Both colonist and industrialist brains gate on a low pantry and answer it by
gathering biomass and cooking. That answer is wrong where the land yields
almost no biomass: ``ProcessCommand.execute`` floors every run at 1 unit of
output, so a barren gatherer still brings home 1 biomass a turn and the gate
keeps re-arming while the pantry never fills. The actor spends its life on
subsistence labor that does not subsist, and buys nothing.

``hand_food_viable`` prices the hand track in labor and lets the brains skip
the gate when it cannot keep up with eating. Those actors fall through to
their other work and pay for imported food out of the wage.
"""

import math
from typing import TYPE_CHECKING, Optional

from spacesim2.core.drives.food_drive import DAILY_CONSUMPTION

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.actor_brain import BrainCache

# The two-step hand track: gather biomass, then cook it.
GATHER_PROCESS_ID = "gather_biomass"
COOK_PROCESS_ID = "make_food"


def hand_food_labor_per_unit(actor: "Actor") -> float:
    """Labor turns the actor spends per unit of hand-made food.

    Reads both recipes from the process registry and scales the gathering
    step by the actor's own land, the same coefficient
    ``ProcessCommand.execute`` applies, taken before that method's 1-unit
    output floor. The floor is what makes a barren land look survivable;
    pricing the run without it is the point.

    The skill multiplier is left out. It scales the inputs and the outputs
    of both steps alike, and it changes over the actor's life, whereas land
    does not: leaving it out keeps the figure land-derived and so cacheable
    for as long as the actor stays on the planet.

    Returns ``math.inf`` when the track cannot produce food at all.
    """
    registry = actor.sim.process_registry
    gather = registry.get_process(GATHER_PROCESS_ID)
    cook = registry.get_process(COOK_PROCESS_ID)
    if gather is None or cook is None:
        return math.inf

    food = actor.sim.commodity_registry.get_commodity("food")
    biomass = actor.sim.commodity_registry.get_commodity("biomass")
    if food is None or biomass is None:
        return math.inf

    food_per_run = cook.outputs.get(food, 0)
    biomass_per_run = cook.inputs.get(biomass, 0)
    biomass_per_gather = gather.outputs.get(biomass, 0)
    if food_per_run <= 0 or biomass_per_gather <= 0:
        return math.inf

    # Both attribute effects cut expected output proportionally, which is
    # what ActorBrain._expected_yield_modifier assumes as well.
    availability = 1.0
    if gather.resource_attribute:
        availability = actor.land.get_availability(gather.resource_attribute.commodity)
    expected_gather = biomass_per_gather * availability
    if expected_gather <= 0.0:
        return math.inf

    gather_turns = (biomass_per_run / expected_gather) * gather.labor
    return (gather_turns + cook.labor) / food_per_run


def hand_food_viable(actor: "Actor", cache: Optional["BrainCache"] = None) -> bool:
    """Can the actor feed itself by hand on its own land?

    Viable means one turn of labor buys at least one turn of eating:
    ``DAILY_CONSUMPTION`` units of food cost no more than one labor turn.
    An actor below that line falls behind however many turns it gathers, so
    its brain skips the pantry gate entirely.

    Memoized in the never-reset land-derived group of ``cache``. A migrant
    draws new land on arrival and the brains drop the whole cache then; see
    ``ColonistBrain.on_relocated``.
    """
    if cache is not None and cache.hand_food_labor is not None:
        labor_per_unit = cache.hand_food_labor
    else:
        labor_per_unit = hand_food_labor_per_unit(actor)
        if cache is not None:
            cache.hand_food_labor = labor_per_unit

    return labor_per_unit * DAILY_CONSUMPTION <= 1.0
