"""Unit tests for the FoodDrive module."""

from unittest.mock import Mock

import pytest

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import SUBSTITUTE_BID_DISCOUNT
from spacesim2.core.commodity import CommodityRegistry, Inventory
from spacesim2.core.drives.food_drive import (
    DAILY_CONSUMPTION,
    DEBT_DECAY_FACTOR,
    DEBT_MISS_PENALTY,
    PANTRY_MAX,
    PANTRY_TARGET,
    FoodDrive,
    FoodDriveMetrics,
)


class TestFoodDriveMetrics:
    """FoodDriveMetrics."""

    def test_get_name(self):
        """get_name returns food."""
        metrics = FoodDriveMetrics(health=1.0, debt=0.0, buffer=0.0, urgency=1.0)
        assert metrics.get_name() == "food"

    def test_get_score_no_debt(self):
        """Score with no debt is 1.0."""
        metrics = FoodDriveMetrics(health=1.0, debt=0.0, buffer=0.5, urgency=1.0)
        assert metrics.get_score() == 1.0

    def test_get_score_full_debt(self):
        """Score with full debt is 0.0."""
        metrics = FoodDriveMetrics(health=0.0, debt=1.0, buffer=0.0, urgency=1.0)
        assert metrics.get_score() == 0.0

    def test_get_score_partial_debt(self):
        """Score is 1 minus debt."""
        metrics = FoodDriveMetrics(health=0.5, debt=0.3, buffer=0.5, urgency=1.0)
        assert metrics.get_score() == 0.7


class TestFoodDriveInitialization:
    """FoodDrive construction."""

    @pytest.fixture
    def commodity_registry(self):
        """Registry loaded from data/commodities.yaml."""
        registry = CommodityRegistry()
        registry.load_from_file("data/commodities.yaml")
        return registry

    def test_initialization(self, commodity_registry):
        """A new drive resolves both food commodities and starts healthy."""
        drive = FoodDrive(commodity_registry)

        assert drive.staple_commodity.id == "processed_food"
        assert drive.quality_commodity.id == "food"
        assert [m.id for m in drive.materials()] == ["processed_food", "food"]
        assert isinstance(drive.metrics, FoodDriveMetrics)
        assert drive.metrics.health == 1.0
        assert drive.metrics.debt == 0.0


class TestFoodDriveTick:
    """FoodDrive.tick() behavior."""

    @pytest.fixture
    def commodity_registry(self):
        """Registry loaded from data/commodities.yaml."""
        registry = CommodityRegistry()
        registry.load_from_file("data/commodities.yaml")
        return registry

    @pytest.fixture
    def food_drive(self, commodity_registry):
        """FoodDrive instance."""
        return FoodDrive(commodity_registry)

    @pytest.fixture
    def mock_actor(self, commodity_registry):
        """Mock actor with a real inventory."""
        actor = Mock()
        actor.inventory = Inventory()
        actor.food_consumed_this_turn = False
        return actor

    def test_tick_consumes_food_when_available(
        self, food_drive, mock_actor, commodity_registry
    ):
        """tick() consumes DAILY_CONSUMPTION food when the actor has food."""
        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(food, 5)

        initial_food = mock_actor.inventory.get_quantity(food)
        assert initial_food == 5

        food_drive.tick(mock_actor)

        final_food = mock_actor.inventory.get_quantity(food)
        assert final_food == 5 - DAILY_CONSUMPTION
        assert mock_actor.food_consumed_this_turn is True

    def test_tick_sets_food_consumed_flag_true_when_ate(
        self, food_drive, mock_actor, commodity_registry
    ):
        """food_consumed_this_turn becomes True after eating."""
        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(food, 1)

        assert mock_actor.food_consumed_this_turn is False

        food_drive.tick(mock_actor)

        assert mock_actor.food_consumed_this_turn is True

    def test_tick_sets_food_consumed_flag_false_when_no_food(
        self, food_drive, mock_actor
    ):
        """food_consumed_this_turn becomes False when there is no food."""
        mock_actor.food_consumed_this_turn = True

        food_drive.tick(mock_actor)

        assert mock_actor.food_consumed_this_turn is False

    def test_tick_updates_health_when_ate(
        self, food_drive, mock_actor, commodity_registry
    ):
        """Health is 1.0 after eating."""
        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(food, 1)

        metrics = food_drive.tick(mock_actor)

        assert metrics.health == 1.0

    def test_tick_updates_health_when_did_not_eat(self, food_drive, mock_actor):
        """Health is 0.0 when there is no food."""
        metrics = food_drive.tick(mock_actor)

        assert metrics.health == 0.0

    def test_tick_accumulates_debt_when_not_eating(self, food_drive, mock_actor):
        """Each missed meal sets debt to debt * decay + penalty."""
        food_drive.tick(mock_actor)
        debt_after_one_miss = food_drive.metrics.debt

        assert debt_after_one_miss == DEBT_MISS_PENALTY

        food_drive.tick(mock_actor)
        debt_after_two_misses = food_drive.metrics.debt

        expected = debt_after_one_miss * DEBT_DECAY_FACTOR + DEBT_MISS_PENALTY
        assert abs(debt_after_two_misses - expected) < 0.001

    def test_tick_decays_debt_when_eating(
        self, food_drive, mock_actor, commodity_registry
    ):
        """Eating decays debt by DEBT_DECAY_FACTOR with no penalty."""
        food_drive.tick(mock_actor)  # miss a meal
        initial_debt = food_drive.metrics.debt
        assert initial_debt > 0

        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(food, 1)
        food_drive.tick(mock_actor)

        expected_debt = initial_debt * DEBT_DECAY_FACTOR
        assert abs(food_drive.metrics.debt - expected_debt) < 0.001

    def test_tick_updates_buffer_based_on_pantry(
        self, food_drive, mock_actor, commodity_registry
    ):
        """Buffer is positive at the pantry target and does not fall with more food."""
        food = commodity_registry.get_commodity("processed_food")

        mock_actor.inventory.add_commodity(food, int(PANTRY_TARGET))
        food_drive.tick(mock_actor)
        buffer_with_target = food_drive.metrics.buffer

        assert buffer_with_target > 0

        mock_actor.inventory.add_commodity(food, int(PANTRY_MAX - PANTRY_TARGET))
        food_drive.tick(mock_actor)
        buffer_with_max = food_drive.metrics.buffer

        assert buffer_with_max >= buffer_with_target

    def test_tick_buffer_zero_when_no_food_remaining(
        self, food_drive, mock_actor, commodity_registry
    ):
        """Buffer is 0 when eating leaves no food."""
        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(
            food, DAILY_CONSUMPTION
        )  # enough for one meal

        food_drive.tick(mock_actor)

        assert food_drive.metrics.buffer == 0.0

    def test_tick_returns_metrics(self, food_drive, mock_actor, commodity_registry):
        """tick() returns the drive's own metrics object."""
        food = commodity_registry.get_commodity("processed_food")
        mock_actor.inventory.add_commodity(food, 10)

        result = food_drive.tick(mock_actor)

        assert isinstance(result, FoodDriveMetrics)
        assert result is food_drive.metrics


class TestFoodDriveIntegration:
    """FoodDrive with a real Actor."""

    @pytest.fixture
    def simulation(self):
        """Minimal simulation stand-in with a registry and data logger."""
        from spacesim2.core.data_logger import DataLogger

        registry = CommodityRegistry()
        registry.load_from_file("data/commodities.yaml")
        sim = Mock()
        sim.commodity_registry = registry
        sim.data_logger = DataLogger()
        return sim

    def test_food_consumed_flag_updated_during_actor_turn(self, simulation):
        """take_turn() updates actor.food_consumed_this_turn."""
        food_drive = FoodDrive(simulation.commodity_registry)

        actor = Actor(
            name="TestActor",
            sim=simulation,
            actor_type=ActorType.REGULAR,
            drives=[food_drive],
            brain=Mock(
                decide_economic_action=lambda _: None,
                decide_market_actions=lambda _: [],
            ),
            planet=None,
            initial_money=50,
            initial_skills={},
        )

        assert actor.food_consumed_this_turn is False

        actor.take_turn()

        assert actor.food_consumed_this_turn is False

        food = simulation.commodity_registry.get_commodity("processed_food")
        actor.inventory.add_commodity(food, 5)

        actor.take_turn()

        assert actor.food_consumed_this_turn is True

    def test_multiple_turns_consumption(self, simulation):
        """Three food units feed three turns, then the actor goes hungry."""
        food_drive = FoodDrive(simulation.commodity_registry)
        food = simulation.commodity_registry.get_commodity("processed_food")

        actor = Actor(
            name="TestActor",
            sim=simulation,
            actor_type=ActorType.REGULAR,
            drives=[food_drive],
            brain=Mock(
                decide_economic_action=lambda _: None,
                decide_market_actions=lambda _: [],
            ),
            planet=None,
            initial_money=50,
            initial_skills={},
        )

        actor.inventory.add_commodity(food, 3)

        actor.take_turn()
        assert actor.food_consumed_this_turn is True
        assert actor.inventory.get_quantity(food) == 2

        actor.take_turn()
        assert actor.food_consumed_this_turn is True
        assert actor.inventory.get_quantity(food) == 1

        actor.take_turn()
        assert actor.food_consumed_this_turn is True
        assert actor.inventory.get_quantity(food) == 0

        actor.take_turn()
        assert actor.food_consumed_this_turn is False
        assert actor.inventory.get_quantity(food) == 0


class TestFoodDriveConstants:
    """FoodDrive constants."""

    def test_daily_consumption_is_positive(self):
        """Daily consumption is positive."""
        assert DAILY_CONSUMPTION > 0

    def test_debt_stays_bounded(self):
        """Decay and penalty keep debt at or below 1.0."""
        # The fixed point of debt = debt * decay + penalty is
        # penalty / (1 - decay), which is <= 1 iff decay + penalty <= 1.
        assert DEBT_DECAY_FACTOR + DEBT_MISS_PENALTY <= 1.0

    def test_pantry_target_less_than_max(self):
        """Pantry target is below the max."""
        assert PANTRY_TARGET < PANTRY_MAX


class TestFoodSecurity:
    """FoodDrive.security() counts purchasing power as well as the pantry."""

    @pytest.fixture
    def food_drive(self):
        registry = CommodityRegistry()
        registry.load_from_file("data/commodities.yaml")
        return FoodDrive(registry)

    def _actor(self, food_drive, money: int, food: int) -> Actor:
        actor = Mock(spec=Actor)
        actor.inventory = Inventory()
        actor.inventory.add_commodity(food_drive.staple_commodity, food)
        actor.money = money
        return actor

    def test_pantry_alone_stays_low(self, food_drive):
        """A six-day pantry with no money is well below saturation."""
        actor = self._actor(food_drive, money=0, food=6)
        assert food_drive.security(actor, unit_price=7.0) < 0.5

    def test_solvent_actor_is_secure(self, food_drive):
        """Money worth more than PANTRY_MAX days of food saturates security."""
        actor = self._actor(food_drive, money=int(PANTRY_MAX * 7 * 2), food=6)
        assert food_drive.security(actor, unit_price=7.0) == pytest.approx(1.0)

    def test_security_rises_with_money(self, food_drive):
        poor = self._actor(food_drive, money=20, food=6)
        rich = self._actor(food_drive, money=500, food=6)
        assert food_drive.security(rich, 7.0) > food_drive.security(poor, 7.0)

    def test_zero_price_counts_pantry_only(self, food_drive):
        actor = self._actor(food_drive, money=1000, food=6)
        assert food_drive.security(actor, unit_price=0.0) == food_drive.security(
            self._actor(food_drive, money=0, food=6), unit_price=7.0
        )


class TestStapleDemandBid:
    """A drive material nobody sells locally still gets a bid.

    Ships read the destination order book, so a planet with no local
    ``processed_food`` seller has to post its own bid or its demand is
    invisible off-world. See ``ActorBrain._add_substitute_material_bids``.
    """

    @pytest.fixture
    def sim(self):
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=1, num_regular_actors=4, num_market_makers=0, num_ships=0
        )
        return sim

    def _actors(self, sim):
        regular = [a for a in sim.actors if a.actor_type == ActorType.REGULAR]
        return regular[0], regular[1]

    def _food_drive(self, actor) -> FoodDrive:
        return next(d for d in actor.drives if isinstance(d, FoodDrive))

    def _post_premium_ask(self, seller, price: int, quantity: int = 20) -> FoodDrive:
        """Put ``food`` on the shelf and nothing else."""
        drive = self._food_drive(seller)
        market = seller.planet.market
        market.buy_orders.clear()
        market.sell_orders.clear()
        seller.inventory.add_commodity(drive.quality_commodity, quantity)
        market.place_sell_order(seller, drive.quality_commodity, quantity, price)
        return drive

    def test_absent_staple_gets_a_bid(self, sim):
        """With only premium food for sale, the actor still bids for the staple."""
        buyer, seller = self._actors(sim)
        self._post_premium_ask(seller, price=15)
        buyer.money = 5000
        buyer.inventory = Inventory()

        commands = buyer.brain._drive_buy_commands(buyer, buyer.planet.market)
        by_id = {c.commodity_type.id: c for c in commands}
        assert "processed_food" in by_id
        assert by_id["processed_food"].quantity > 0

    def test_staple_bid_within_willingness_to_pay(self, sim):
        """The staple bid never exceeds the drive's WTP or the premium ask."""
        buyer, seller = self._actors(sim)
        ask = 15
        staple = self._post_premium_ask(seller, price=ask).staple_commodity
        buyer.money = 5000
        buyer.inventory = Inventory()

        market = buyer.planet.market
        drive = self._food_drive(buyer)
        cache = buyer.brain._turn_cache(buyer)
        lam = buyer.brain._value_of_money(buyer, market, cache)
        wtp = buyer.brain._drive_willingness_to_pay(
            buyer, market, drive, staple, lam, cache
        )

        commands = buyer.brain._drive_buy_commands(buyer, market)
        bid = next(c for c in commands if c.commodity_type.id == "processed_food")
        assert 0 < bid.price <= min(wtp, int(ask * SUBSTITUTE_BID_DISCOUNT))

    def test_staple_bid_stands_under_a_pricier_local_staple_ask(self, sim):
        """A local staple seller above the food price does not silence the bid.

        Hand-feeding actors are the staple's deep demand. Their bid sits
        under the food ask and below the premium staple ask, so it rests
        unfilled locally and shows a ship a full-hold price.
        """
        buyer, seller = self._actors(sim)
        food_ask = 10
        drive = self._post_premium_ask(seller, price=food_ask)
        staple = drive.staple_commodity
        market = buyer.planet.market
        seller.inventory.add_commodity(staple, 5)
        market.place_sell_order(seller, staple, 5, 25)
        buyer.money = 5000
        buyer.inventory = Inventory()

        commands = buyer.brain._drive_buy_commands(buyer, market)
        by_id = {c.commodity_type.id: c for c in commands}
        assert by_id["food"].price == food_ask
        assert by_id["processed_food"].quantity > 0
        assert by_id["processed_food"].price <= int(food_ask * SUBSTITUTE_BID_DISCOUNT)
        assert by_id["processed_food"].price < 25

    def test_bids_stay_within_budget(self, sim):
        """Premium and staple bids together never exceed the actor's money."""
        buyer, seller = self._actors(sim)
        self._post_premium_ask(seller, price=15)
        buyer.money = 40
        buyer.inventory = Inventory()

        commands = buyer.brain._drive_buy_commands(buyer, buyer.planet.market)
        assert commands
        assert sum(c.quantity * c.price for c in commands) <= buyer.money

    def test_cheapest_material_bid_at_ask_and_other_under_it(self, sim):
        """The cheapest material is bid at its ask; the other rests under it."""
        buyer, seller = self._actors(sim)
        drive = self._food_drive(seller)
        market = seller.planet.market
        market.buy_orders.clear()
        market.sell_orders.clear()
        seller.inventory.add_commodity(drive.staple_commodity, 20)
        seller.inventory.add_commodity(drive.quality_commodity, 20)
        market.place_sell_order(seller, drive.staple_commodity, 20, 6)
        market.place_sell_order(seller, drive.quality_commodity, 20, 15)
        buyer.money = 5000
        buyer.inventory = Inventory()

        commands = buyer.brain._drive_buy_commands(buyer, market)
        food_ids = {"processed_food", "food"}
        by_id = {
            c.commodity_type.id: c for c in commands if c.commodity_type.id in food_ids
        }
        assert by_id["processed_food"].price == 6
        assert 0 < by_id["food"].price <= int(6 * SUBSTITUTE_BID_DISCOUNT)
