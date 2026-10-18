"""Displacement bids: an actor that hand-makes a need good also bids for it.

See ``ActorBrain._displacement_bid_commands``. A need gate that spends a
labor turn cooking food leaves no market demand behind, so nothing is
shipped in and the actor cooks again next turn. The bid is sized to the run
the purchase would displace and priced with the actor's own opportunity cost
of a labor turn.
"""

import pytest

from spacesim.core.actor import ActorType
from spacesim.core.actor_brain import GOVERNMENT_WAGE, BrainCache
from spacesim.core.commodity import Inventory
from spacesim.core.drives.food_drive import PANTRY_TARGET, FoodDrive
from spacesim.core.land import Land
from spacesim.core.simulation import Simulation

# One run of make_food, per data/processes.yaml.
MAKE_FOOD_OUTPUT = 4


@pytest.fixture
def sim():
    simulation = Simulation()
    simulation.setup_simple(
        num_planets=1, num_regular_actors=4, num_market_makers=0, num_ships=0
    )
    return simulation


def _colonists(sim):
    regular = [a for a in sim.actors if a.actor_type == ActorType.REGULAR]
    return regular[0], regular[1]


def _food_drive(actor) -> FoodDrive:
    return next(d for d in actor.drives if isinstance(d, FoodDrive))


def _stocked_buyer(sim, money: int = 5000):
    """A colonist with a full pantry, plenty of money, and a clean market.

    The land is pinned to the penalty-free default. A random draw can give
    the planet zero biomass, and an actor who cannot gather biomass has no
    gathering run to displace.
    """
    buyer, seller = _colonists(sim)
    buyer.land = Land.default()
    seller.land = Land.default()
    market = buyer.planet.market
    market.buy_orders.clear()
    market.sell_orders.clear()
    buyer.inventory = Inventory()
    buyer.inventory.add_commodity(_food_drive(buyer).quality_commodity, PANTRY_TARGET)
    buyer.money = money
    return buyer, seller


def _bids_by_id(commands):
    return {c.commodity_type.id: c for c in commands}


class TestDisplacementBid:
    def test_cooking_actor_bids_for_food_with_a_full_pantry(self, sim):
        """A hand-cooked run this turn is bid for even with no unmet need."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market

        without = _bids_by_id(buyer.brain._drive_buy_commands(buyer, market))
        assert "food" not in without

        buyer.brain._begin_self_supply_record()
        buyer.brain._record_self_supply(buyer, "make_food")
        with_record = _bids_by_id(buyer.brain._drive_buy_commands(buyer, market))

        assert "food" in with_record
        assert with_record["food"].quantity >= MAKE_FOOD_OUTPUT

    def test_every_material_of_the_drive_gets_a_bid(self, sim):
        """Cooking food also bids for the staple a ship could bring instead."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market

        buyer.brain._begin_self_supply_record()
        buyer.brain._record_self_supply(buyer, "make_food")
        commands = _bids_by_id(buyer.brain._drive_buy_commands(buyer, market))

        assert commands["food"].quantity > 0
        assert commands["processed_food"].quantity > 0

    def test_gathering_actor_bids_for_the_intermediate(self, sim):
        """gather_biomass draws a bid for biomass, an input to the food recipe."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market

        buyer.brain._begin_self_supply_record()
        buyer.brain._record_self_supply(buyer, "gather_biomass")
        commands = _bids_by_id(buyer.brain._drive_buy_commands(buyer, market))

        assert commands["biomass"].quantity > 0
        assert commands["biomass"].price > 0

    def test_food_gate_records_the_run_it_makes(self, sim):
        """The record is written by the need gate, not only by tests."""
        buyer, _seller = _stocked_buyer(sim)
        buyer.inventory = Inventory()  # empty pantry: the food gate fires

        command = buyer.brain.decide_economic_action(buyer)

        assert command.process_id in ("make_food", "gather_biomass")
        recorded = {c.id: q for c, q in buyer.brain._self_supplied.items()}
        assert recorded

    def test_no_self_supply_posts_no_extra_bid(self, sim):
        """An actor that made nothing by hand bids exactly as before."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market

        buyer.brain._begin_self_supply_record()
        commands = buyer.brain._drive_buy_commands(buyer, market)

        assert "food" not in _bids_by_id(commands)

    def test_bid_respects_the_money_budget(self, sim):
        """Every bid an actor posts in a turn together fits its money."""
        buyer, _seller = _stocked_buyer(sim, money=12)
        market = buyer.planet.market

        buyer.brain._begin_self_supply_record()
        buyer.brain._record_self_supply(buyer, "make_food")
        commands = buyer.brain._drive_buy_commands(buyer, market)

        assert sum(c.quantity * c.price for c in commands) <= buyer.money


class TestOpportunityCostLaborValue:
    def test_higher_labor_value_raises_the_ceiling(self, sim):
        """A costlier labor turn raises what the actor pays for the good."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market
        drive = _food_drive(buyer)
        food = drive.quality_commodity
        cache = buyer.brain._turn_cache(buyer)
        lam = buyer.brain._value_of_money(buyer, market, cache)

        at_wage = buyer.brain._drive_willingness_to_pay(
            buyer, market, drive, food, lam, cache, GOVERNMENT_WAGE
        )
        at_opportunity = buyer.brain._drive_willingness_to_pay(
            buyer, market, drive, food, lam, cache, GOVERNMENT_WAGE * 5
        )

        assert at_opportunity > at_wage

    def test_no_alternative_work_prices_labor_at_the_wage(self, sim, monkeypatch):
        """With no profitable process the opportunity cost is the wage."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market
        monkeypatch.setattr(
            buyer.brain,
            "_best_process_and_raw_profit",
            lambda actor, market, cache=None: (None, 0.0),
        )

        cache = buyer.brain._turn_cache(buyer)
        assert buyer.brain.labor_opportunity_cost(buyer, cache) == GOVERNMENT_WAGE

        food = _food_drive(buyer).quality_commodity
        wage_cost = buyer.brain._replacement_cost(buyer, market, food, cache)
        opportunity_cost = buyer.brain._replacement_cost(
            buyer,
            market,
            food,
            cache,
            labor_value=buyer.brain.labor_opportunity_cost(buyer, cache),
        )
        assert wage_cost == opportunity_cost

    def test_replacement_cost_is_not_served_from_the_wage_memo(self, sim):
        """A different labor value gets its own memo entry, not the wage's."""
        buyer, _seller = _stocked_buyer(sim)
        market = buyer.planet.market
        food = _food_drive(buyer).quality_commodity
        cache = BrainCache().refresh(buyer)

        at_wage = buyer.brain._replacement_cost(buyer, market, food, cache)
        at_double = buyer.brain._replacement_cost(
            buyer, market, food, cache, labor_value=GOVERNMENT_WAGE * 2
        )

        assert at_wage is not None and at_double is not None
        assert at_double > at_wage
        assert cache.replacement_cost[(food.id, GOVERNMENT_WAGE)] == at_wage
        assert cache.replacement_cost[(food.id, GOVERNMENT_WAGE * 2)] == at_double
