"""Barren land skips the pantry gate instead of gathering forever.

``ProcessCommand.execute`` floors every run at 1 unit, so an actor on land
with no biomass still gathers 1 unit a turn: 4 gather turns plus a cook turn
make 4 food per 5 turns against 1 eaten per turn. The gate never releases.
``hand_food_viable`` prices the track in labor and the brains skip the gate
when it cannot keep up. See spacesim/core/brains/hand_food.py.
"""

import math

import pytest

from spacesim.core.actor import ActorType
from spacesim.core.actor_brain import BrainCache
from spacesim.core.brains.colonist import ColonistBrain
from spacesim.core.brains.hand_food import hand_food_labor_per_unit, hand_food_viable
from spacesim.core.brains.industrialist import IndustrialistBrain
from spacesim.core.commands import ProcessCommand
from spacesim.core.land import Land
from spacesim.core.simulation import Simulation

FOOD_CHAIN = {"gather_biomass", "make_food"}


@pytest.fixture
def actor():
    sim = Simulation()
    sim.setup_simple(
        num_planets=1, num_regular_actors=4, num_market_makers=0, num_ships=0
    )
    return next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)


def _set_biomass(actor, availability: float) -> None:
    actor.land = Land({"biomass": availability})


class TestViability:
    @pytest.mark.parametrize("availability", [1.0, 0.5, 0.2])
    def test_viable_where_a_labor_turn_buys_a_turn_of_eating(self, actor, availability):
        _set_biomass(actor, availability)
        assert hand_food_viable(actor)

    @pytest.mark.parametrize("availability", [0.0, 0.1])
    def test_not_viable_where_gathering_cannot_keep_up(self, actor, availability):
        _set_biomass(actor, availability)
        assert not hand_food_viable(actor)

    def test_labor_per_unit_matches_the_recipes(self, actor):
        """8 biomass a turn at full yield, 4 biomass cooked into 4 food."""
        _set_biomass(actor, 1.0)
        # 0.5 gather turns + 1 cook turn, over 4 food.
        assert hand_food_labor_per_unit(actor) == pytest.approx(0.375)

        _set_biomass(actor, 0.2)
        # 2.5 gather turns + 1 cook turn, over 4 food.
        assert hand_food_labor_per_unit(actor) == pytest.approx(0.875)

    def test_barren_land_is_infinite_labor(self, actor):
        _set_biomass(actor, 0.0)
        assert hand_food_labor_per_unit(actor) == math.inf


class TestCache:
    def test_result_is_memoized_until_the_cache_is_dropped(self, actor):
        _set_biomass(actor, 1.0)
        cache = BrainCache()
        assert hand_food_viable(actor, cache)
        assert cache.hand_food_labor == pytest.approx(0.375)

        # Land is fixed while the actor stays, so a changed coefficient is
        # not picked up until the cache is dropped.
        _set_biomass(actor, 0.0)
        assert hand_food_viable(actor, cache)

    def test_relocation_drops_the_cached_verdict(self, actor):
        _set_biomass(actor, 1.0)
        brain = ColonistBrain()
        actor.brain = brain
        cache = brain._turn_cache(actor)
        assert hand_food_viable(actor, cache)

        _set_biomass(actor, 0.0)
        brain.on_relocated(actor)
        assert not hand_food_viable(actor, brain._turn_cache(actor))


@pytest.mark.parametrize("brain_cls", [ColonistBrain, IndustrialistBrain])
class TestGate:
    def test_barren_land_skips_the_food_gate(self, actor, brain_cls):
        actor.brain = brain_cls()
        actor.inventory.clear()
        _set_biomass(actor, 0.0)

        action = actor.brain.decide_economic_action(actor)
        assert not (
            isinstance(action, ProcessCommand) and action.process_id in FOOD_CHAIN
        )

    def test_barren_land_still_bids_for_food(self, actor, brain_cls):
        """The drive's own bid is unaffected: the actor buys what it cannot make."""
        actor.brain = brain_cls()
        actor.inventory.clear()
        actor.money = 5000
        _set_biomass(actor, 0.0)
        actor.brain.decide_economic_action(actor)

        market = actor.planet.market
        bids = {
            command.commodity_type.id
            for command in actor.brain._drive_buy_commands(actor, market)
        }
        assert bids & {"food", "processed_food"}

    def test_good_land_still_fires_the_food_gate(self, actor, brain_cls):
        actor.brain = brain_cls()
        actor.inventory.clear()
        _set_biomass(actor, 1.0)

        action = actor.brain.decide_economic_action(actor)
        assert isinstance(action, ProcessCommand)
        assert action.process_id == "gather_biomass"
