"""The staple/premium food flip.

``processed_food`` is the staple ``FoodDrive`` eats and bids for;
hand-cooked ``food`` is the premium good. These tests cover the parts that
span drive, brain and market: the combined pantry the cook-or-gather gates
read, the numeraire, and the willingness-to-pay cap.
"""

import math

import pytest

from spacesim2.core.actor import ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.drives.food_drive import FoodDrive, food_pantry_units
from spacesim2.core.simulation import Simulation


@pytest.fixture(scope="module")
def sim() -> Simulation:
    world = Simulation()
    world.setup_simple(
        num_planets=1, num_regular_actors=4, num_market_makers=1, num_ships=0
    )
    return world


def _regular(sim: Simulation):
    return next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)


def _clear(actor, commodity) -> None:
    qty = actor.inventory.get_available_quantity(commodity)
    if qty:
        assert actor.inventory.remove_commodity(commodity, qty)


def _empty_pantry(actor, sim: Simulation):
    reg = sim.commodity_registry
    staple = reg.get_commodity("processed_food")
    premium = reg.get_commodity("food")
    biomass = reg.get_commodity("biomass")
    assert staple is not None and premium is not None and biomass is not None
    for commodity in (staple, premium, biomass):
        _clear(actor, commodity)
    return staple, premium, biomass


class TestPantryCountsBothGoods:
    def test_pantry_units_sums_staple_and_premium(self, sim):
        actor = _regular(sim)
        staple, premium, _ = _empty_pantry(actor, sim)
        actor.inventory.add_commodity(staple, 3)
        actor.inventory.add_commodity(premium, 2)
        assert food_pantry_units(actor) == 5
        _empty_pantry(actor, sim)

    def test_no_food_drive_reads_as_empty(self, sim):
        actor = _regular(sim)
        drives = actor.drives
        actor.drives = [d for d in drives if not isinstance(d, FoodDrive)]
        try:
            assert food_pantry_units(actor) == 0
        finally:
            actor.drives = drives


@pytest.mark.parametrize("brain_cls", [ColonistBrain, IndustrialistBrain])
class TestCookGate:
    """The gates read the pantry, not the ``food`` commodity alone."""

    def test_staple_stock_stops_the_actor_cooking(self, sim, brain_cls):
        actor = _regular(sim)
        actor.brain = brain_cls()
        staple, _, biomass = _empty_pantry(actor, sim)
        actor.inventory.add_commodity(staple, 6)
        actor.inventory.add_commodity(biomass, 8)
        action = actor.brain.decide_economic_action(actor)
        assert not (
            isinstance(action, ProcessCommand)
            and action.process_id in ("make_food", "gather_biomass")
        )
        _empty_pantry(actor, sim)

    def test_empty_pantry_still_cooks_by_hand(self, sim, brain_cls):
        actor = _regular(sim)
        actor.brain = brain_cls()
        _, _, biomass = _empty_pantry(actor, sim)
        actor.inventory.add_commodity(biomass, 8)
        action = actor.brain.decide_economic_action(actor)
        assert isinstance(action, ProcessCommand)
        assert action.process_id == "make_food"
        _empty_pantry(actor, sim)


class TestNumeraire:
    def test_numeraire_is_the_cheaper_food(self, sim):
        """Lambda anchors on whichever food the actor can actually get.

        A colonist with no chemical plant cannot make the staple, so its
        effective staple price is whatever the book asks; hand-cooked food
        it can always make. The numeraire is the smaller of the two.
        """
        actor = _regular(sim)
        market = actor.planet.market
        drive = next(d for d in actor.drives if isinstance(d, FoodDrive))
        staple, premium = drive.materials()
        expected = min(
            actor.brain._effective_food_price(actor, market, staple),
            actor.brain._effective_food_price(actor, market, premium),
        )
        assert actor.brain._numeraire_price(actor, market) == pytest.approx(expected)


class TestWillingnessToPayCap:
    def test_staple_bid_is_capped_by_hand_cooked_food(self, sim):
        """With no plant, the cap on a staple bid is the cost of cooking.

        ``_replacement_cost`` is None for the staple, so capping on the
        target good alone would leave the bid unbounded. The cap is the
        cheapest self-supply across the drive's materials.
        """
        actor = _regular(sim)
        market = actor.planet.market
        drive = next(d for d in actor.drives if isinstance(d, FoodDrive))
        staple, premium = drive.materials()

        assert actor.brain._replacement_cost(actor, market, staple) is None
        premium_cost = actor.brain._replacement_cost(actor, market, premium)
        assert premium_cost is not None

        # A tiny lambda makes welfare-based WTP enormous, so the returned
        # figure is the cap and nothing else.
        wtp = actor.brain._drive_willingness_to_pay(
            actor, market, drive, staple, lam=1e-9
        )
        assert wtp == math.ceil(premium_cost * (1.0 + drive.metrics.debt))
