import math
from unittest.mock import Mock

import pytest

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import GOVERNMENT_WAGE, ActorBrain, BrainCache
from spacesim2.core.brains.industrialist import (
    BUILD_INPUT_CEILING_CAP,
    ENTRY_MARGIN,
    NETBACK_VALUE_CAP,
    RECIPE_COOLDOWN_TURNS,
    RECIPE_STUCK_TURNS,
    IndustrialistBrain,
)
from spacesim2.core.commands import (
    GovernmentWorkCommand,
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
    ProcessCommand,
)
from spacesim2.core.commodity import (
    CommodityDefinition,
    CommodityRegistry,
    Inventory,
)
from spacesim2.core.drives.food_drive import FoodDrive
from spacesim2.core.land import Land
from spacesim2.core.market import Market
from spacesim2.core.process import ProcessDefinition, ResourceAttribute


def _real_registry() -> CommodityRegistry:
    """Registry loaded from the project's commodity data."""
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


def _hand_food_process_stub(commodity):
    """A ``get_process`` side effect that knows only the hand-food track.

    ``hand_food_viable`` prices ``gather_biomass`` and ``make_food`` from the
    registry before the pantry gate is read, so a mock universe with no
    processes at all never lets the gate fire. The mock registry resolves
    every commodity id to one object, so food and biomass are the same here.
    """
    gather = ProcessDefinition(
        id="gather_biomass",
        name="Gather Biomass",
        inputs={},
        outputs={commodity: 8},
        tools_required=[],
        facilities_required=[],
        labor=1,
        description="",
        resource_attribute=ResourceAttribute(commodity="biomass", effect="output"),
    )
    cook = ProcessDefinition(
        id="make_food",
        name="Make Food",
        inputs={commodity: 4},
        outputs={commodity: 4},
        tools_required=[],
        facilities_required=[],
        labor=1,
        description="",
    )
    by_id = {"gather_biomass": gather, "make_food": cook}
    return lambda process_id: by_id.get(process_id)


def _wire_producer_index(sim_mock):
    """Derive the mock's process indexes from all_processes.return_value.

    Evaluated lazily, so tests may set the list after the fixture runs.
    Mirrors the id-keyed producer and consumer indexes in ProcessRegistry.
    """
    registry = sim_mock.process_registry

    def producing(commodity):
        processes = registry.all_processes.return_value
        if not isinstance(processes, list):
            return []  # a test that never set the list has no recipes
        return [p for p in processes if any(o.id == commodity.id for o in p.outputs)]

    def consuming(commodity):
        processes = registry.all_processes.return_value
        if not isinstance(processes, list):
            return []
        return [p for p in processes if any(i.id == commodity.id for i in p.inputs)]

    registry.get_processes_producing.side_effect = producing
    registry.get_processes_consuming.side_effect = consuming


class _StubDrive:
    """Minimal drive with the interface ActorBrain pricing uses."""

    WELLBEING = True

    def __init__(self, name, materials, target, miss_penalty=0.2, buffer=0.0):
        self._materials = materials
        self._target = target
        self._stake = miss_penalty
        self._buffer = buffer
        self.metrics = Mock()
        self.metrics.get_name.return_value = name
        self.metrics.buffer = buffer

    def materials(self):
        return list(self._materials)

    def target_units(self):
        return self._target

    def material_servings(self, commodity_id):
        return 1.0

    def deprivation_stake(self):
        return self._stake

    def marginal_welfare(self):
        return self._stake * (1.0 - self._buffer)

    def security(self, actor, unit_price):
        return self._buffer

    def can_purchase(self, actor):
        return True


class TestIndustrialistBrain:
    @pytest.fixture
    def mock_actor(self):
        """Mock actor with a mocked planet, market, sim, and inventory."""
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.planet = Mock()
        actor.planet.market = Mock()
        # Default the depth-aware output valuation to "the book cannot absorb
        # a run", and route its trade-history fallback to the same stub the
        # tests below already set, so they keep asserting on one price.
        actor.planet.market.get_bid_price_at_depth.return_value = None
        actor.planet.market.get_30_day_average_price.side_effect = (
            lambda commodity: float(actor.planet.market.get_avg_price(commodity))
        )
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        # The liquidation sweep iterates the registry; these tests are about
        # other behavior, so give it an empty universe by default.
        actor.sim.commodity_registry.all_commodities.return_value = []
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        actor.drives = []
        return actor

    @pytest.fixture
    def mock_food_commodity(self):
        """Mock food commodity."""
        food = Mock(spec=CommodityDefinition)
        food.id = "food"
        return food

    @pytest.fixture
    def brain(self):
        """IndustrialistBrain instance."""
        return IndustrialistBrain()

    def test_initial_state(self, brain):
        """A new brain has no chosen recipe."""
        assert brain.chosen_recipe_id is None
        assert brain.turns_since_recipe_evaluation == 0

    def test_recipe_reevaluation_chance(self, brain):
        """Recipe reevaluation fires about 1% of the time."""
        reevaluations = 0
        iterations = 5000

        for _ in range(iterations):
            if brain._should_reevaluate_recipe():
                reevaluations += 1

        # Wide band around 1% to absorb sampling variance.
        reevaluation_rate = reevaluations / iterations
        assert 0.003 < reevaluation_rate < 0.025

    def test_food_shortage_emergency_action(
        self, brain, mock_actor, mock_food_commodity
    ):
        """With under 2 food the actor makes food before anything else."""
        mock_actor.sim.commodity_registry.get_commodity.return_value = (
            mock_food_commodity
        )
        mock_actor.sim.process_registry.all_processes.return_value = []
        # The gate first asks whether hand food pays on this actor's land,
        # so the two food recipes have to exist. Everything else is absent.
        mock_actor.land = Land({"biomass": 1.0})
        mock_actor.sim.process_registry.get_process.side_effect = (
            _hand_food_process_stub(mock_food_commodity)
        )
        mock_actor.inventory.get_quantity.return_value = 1
        mock_actor.can_execute_process.return_value = True

        brain.chosen_recipe_id = None

        action = brain.decide_economic_action(mock_actor)

        assert isinstance(action, ProcessCommand)
        assert action.process_id == "make_food"

    def test_executes_chosen_recipe_when_possible(
        self, brain, mock_actor, mock_food_commodity
    ):
        """The actor runs its chosen recipe when it can."""
        mock_actor.sim.commodity_registry.get_commodity.return_value = (
            mock_food_commodity
        )
        mock_actor.inventory.get_quantity.return_value = 10
        mock_actor.inventory.has_quantity.return_value = True
        # The food gate reads the FoodDrive pantry, so this actor needs one.
        mock_actor.inventory.get_available_quantity.return_value = 10
        mock_actor.drives = [FoodDrive(_real_registry())]
        mock_actor.can_execute_process.return_value = True

        mock_process = Mock(spec=ProcessDefinition)
        mock_process.upkeep = {}
        mock_process.capital = {}
        mock_process.tools_required = []
        mock_process.facilities_required = []
        mock_process.upkeep = {}
        mock_process.capital = {}
        mock_actor.sim.process_registry.get_process.return_value = mock_process

        brain.chosen_recipe_id = "test_recipe"

        action = brain.decide_economic_action(mock_actor)

        assert isinstance(action, ProcessCommand)
        assert action.process_id == "test_recipe"

    def test_falls_back_to_government_work(
        self, brain, mock_actor, mock_food_commodity
    ):
        """The actor does government work when it cannot run its recipe."""
        mock_actor.sim.commodity_registry.get_commodity.return_value = (
            mock_food_commodity
        )
        mock_actor.inventory.get_quantity.return_value = 10
        mock_actor.inventory.has_quantity.return_value = True
        # The food gate reads the FoodDrive pantry, so this actor needs one.
        mock_actor.inventory.get_available_quantity.return_value = 10
        mock_actor.drives = [FoodDrive(_real_registry())]
        mock_actor.can_execute_process.return_value = False

        mock_process = Mock(spec=ProcessDefinition)
        mock_process.upkeep = {}
        mock_process.capital = {}
        mock_process.tools_required = []
        mock_process.facilities_required = []
        mock_process.upkeep = {}
        mock_process.capital = {}
        mock_actor.sim.process_registry.get_process.return_value = mock_process

        brain.chosen_recipe_id = "test_recipe"

        action = brain.decide_economic_action(mock_actor)

        assert isinstance(action, GovernmentWorkCommand)

    def test_recipe_score_includes_labor_cost(self, brain, mock_actor):
        """Recipe scoring charges a turn of labor on top of input costs."""
        input_commodity = Mock()
        input_commodity.id = "input1"
        output_commodity = Mock()
        output_commodity.id = "output1"

        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.inputs = {input_commodity: 2}
        process.outputs = {output_commodity: 1}
        process.tools_required = []
        process.facilities_required = []
        process.upkeep = {}
        process.capital = {}
        process.resource_attribute = None

        market = mock_actor.planet.market

        market.get_bid_ask_spread.return_value = (None, None)

        # Viable recipe: inputs 2 x 10 + labor 10 = 30 cost, output sells for
        # 40, which clears the 20% margin, so the score is the profit of 10.
        def viable_prices(commodity):
            if commodity is input_commodity:
                return 10
            if commodity is output_commodity:
                return 40
            return 0

        market.get_avg_price.side_effect = viable_prices

        score = brain._calculate_recipe_score(mock_actor, market, process)
        assert score == pytest.approx(10.0, rel=0.01)

        # Covers inputs but not the turn of labor: cost 30, output sells for
        # 25, so not viable.
        def below_wage_prices(commodity):
            if commodity is input_commodity:
                return 10
            if commodity is output_commodity:
                return 25
            return 0

        market.get_avg_price.side_effect = below_wage_prices

        assert brain._calculate_recipe_score(mock_actor, market, process) == 0.0
        # The exit check sees the raw negative profit without the entry margin.
        raw = brain._calculate_recipe_score(
            mock_actor, market, process, require_entry_margin=False
        )
        assert raw == pytest.approx(-5.0, rel=0.01)

    def test_market_actions_cancel_existing_orders(self, brain, mock_actor):
        """decide_market_actions cancels the actor's existing orders."""
        existing_buy_order = Mock()
        existing_buy_order.order_id = "buy123"
        existing_sell_order = Mock()
        existing_sell_order.order_id = "sell456"

        mock_actor.planet.market.get_actor_orders.return_value = {
            "buy": [existing_buy_order],
            "sell": [existing_sell_order],
        }
        mock_actor.sim.commodity_registry.get_commodity.return_value = (
            None  # no food commodity
        )

        commands = brain.decide_market_actions(mock_actor)

        cancel_commands = [
            cmd for cmd in commands if cmd.__class__.__name__ == "CancelOrderCommand"
        ]
        assert len(cancel_commands) == 2
        order_ids = [cmd.order_id for cmd in cancel_commands]
        assert "buy123" in order_ids
        assert "sell456" in order_ids

    def test_food_purchase_behavior(self, brain, mock_actor, mock_food_commodity):
        """Drive-backed food demand lifts the resting ask up to the target."""
        mock_actor.inventory.get_quantity.return_value = 3  # below target of 6
        mock_actor.money = 100

        mock_actor.drives = [
            _StubDrive("food", [mock_food_commodity], target=6, miss_penalty=0.2)
        ]

        sell_order = Mock()
        sell_order.price = 5
        sell_order.actor = "different_actor"
        sell_order.timestamp = 0
        sell_order.cancelled = False

        mock_actor.planet.market.sell_orders = {mock_food_commodity: [sell_order]}
        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, 5)
        mock_actor.planet.market.get_actor_orders.return_value = {"buy": [], "sell": []}
        # No resting own orders: the cheapest-ask fast path checks this map.
        mock_actor.planet.market.actor_orders = {}
        mock_actor.sim.commodity_registry.get_commodity.return_value = (
            mock_food_commodity
        )
        # Food trades around 10, so willingness of price * (1 - buffer) = 10
        # sits above the 5 ask. No recipe produces food locally, so no make cap.
        mock_actor.planet.market.get_avg_price.return_value = 10
        mock_actor.sim.process_registry.all_processes.return_value = []

        commands = brain.decide_market_actions(mock_actor)

        buy_commands = [
            cmd for cmd in commands if isinstance(cmd, PlaceBuyOrderCommand)
        ]
        assert len(buy_commands) > 0

        food_buy_command = next(
            (cmd for cmd in buy_commands if cmd.commodity_type == mock_food_commodity),
            None,
        )
        assert food_buy_command is not None
        assert food_buy_command.quantity == 3  # 3 more reaches the target of 6
        assert food_buy_command.price == 5

    def test_recipe_viability_considers_land(self, brain, mock_actor):
        """Recipe score scales gathering output by the actor's land."""
        from spacesim2.core.process import ResourceAttribute

        output_commodity = Mock()
        output_commodity.id = "biomass"

        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.inputs = {}
        process.outputs = {output_commodity: 4}
        process.tools_required = []
        process.facilities_required = []
        process.upkeep = {}
        process.capital = {}
        process.resource_attribute = ResourceAttribute(
            commodity="biomass", effect="output"
        )

        mock_actor.inventory.has_quantity.return_value = True

        market = mock_actor.planet.market
        market.get_bid_ask_spread.return_value = (None, None)
        market.get_avg_price.return_value = 10

        from spacesim2.core.land import Land

        mock_actor.land = Land({"biomass": 0.9})

        # Output 4 * 0.9 = 3.6, value 36, minus a turn of labor at the
        # government wage gives profit 26.
        score_good = brain._calculate_recipe_score(mock_actor, market, process)
        assert score_good > 0
        assert score_good == pytest.approx(26.0, rel=0.01)

        mock_actor.land = Land({"biomass": 0.2})

        # Output 4 * 0.2 = 0.8, value 8, below the cost of the labor turn, so
        # gathering here is not viable.
        score_poor = brain._calculate_recipe_score(mock_actor, market, process)
        assert score_poor == 0.0

    def test_recipe_selection_prefers_profitable_recipes(self, brain, mock_actor):
        """Recipe selection is weighted by score."""
        from spacesim2.core.process import ResourceAttribute

        biomass_commodity = Mock()
        biomass_commodity.id = "biomass"
        fiber_commodity = Mock()
        fiber_commodity.id = "fiber"

        biomass_process = Mock(spec=ProcessDefinition)
        biomass_process.upkeep = {}
        biomass_process.capital = {}
        biomass_process.id = "gather_biomass"
        biomass_process.inputs = {}
        biomass_process.outputs = {biomass_commodity: 4}
        biomass_process.tools_required = []
        biomass_process.facilities_required = []
        biomass_process.upkeep = {}
        biomass_process.capital = {}
        biomass_process.resource_attribute = ResourceAttribute(
            commodity="biomass", effect="output"
        )

        fiber_process = Mock(spec=ProcessDefinition)
        fiber_process.upkeep = {}
        fiber_process.capital = {}
        fiber_process.id = "gather_fiber"
        fiber_process.inputs = {}
        fiber_process.outputs = {fiber_commodity: 3}
        fiber_process.tools_required = []
        fiber_process.facilities_required = []
        fiber_process.upkeep = {}
        fiber_process.capital = {}
        fiber_process.resource_attribute = ResourceAttribute(
            commodity="fiber", effect="output"
        )

        mock_actor.sim.process_registry.all_processes.return_value = [
            biomass_process,
            fiber_process,
        ]

        mock_actor.inventory.has_quantity.return_value = True

        market = mock_actor.planet.market
        market.get_bid_ask_spread.return_value = (None, None)
        market.get_avg_price.return_value = 10

        # The actor's land is rich in biomass and poor in fiber.
        from spacesim2.core.land import Land

        mock_actor.land = Land({"biomass": 0.9, "fiber": 0.1})

        import random

        random.seed(42)

        selections = {"gather_biomass": 0, "gather_fiber": 0}
        for _ in range(100):
            recipe = brain._select_new_recipe(mock_actor)
            if recipe:
                selections[recipe] += 1

        # Biomass has a 9x score advantage.
        assert selections["gather_biomass"] > selections["gather_fiber"] * 3


class TestImputedProcurementBids:
    """Cold-start bids for never-traded intermediates use imputed replacement cost.

    get_avg_price returns a default of 10 with no trades, which must not be
    used as the bid.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        c = Mock(spec=CommodityDefinition)
        c.id = cid
        c.transportable = True
        return c

    @staticmethod
    def _actor(money=100):
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = money
        actor.planet = Mock()
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        return actor

    def _refined_chain(self, actor):
        """refined_chemicals made from 3 chemicals with a live ask of 4.

        Imputed cost is labor 10 + 3 * 4 = 22.
        """
        refined = self._commodity("refined_chemicals")
        chem = self._commodity("chemicals")

        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.id = "refine_chemicals"
        process.inputs = {chem: 3}
        process.outputs = {refined: 1}
        process.tools_required = []
        process.facilities_required = []
        process.upkeep = {}
        process.capital = {}
        process.resource_attribute = None
        actor.sim.process_registry.all_processes.return_value = [process]
        return refined, chem

    def test_never_traded_input_bids_imputed_cost_not_default(self, brain):
        """With no ask and no price signal, bid imputed cost times the margin.

        The bootstrap margin must clear a supplier's 1.2x entry threshold:
        ceil(22 * 1.25) = 28.
        """
        actor = self._actor()
        refined, chem = self._refined_chain(actor)

        market = Mock()
        market.sell_orders = {}

        def spread(commodity):
            # chemicals has a live ask as its cost anchor; refined has none.
            return (None, 4) if commodity is chem else (None, None)

        market.get_bid_ask_spread.side_effect = spread
        market.has_price_signal.return_value = False
        market.get_avg_price.return_value = 10  # the no-trade default

        commands = brain._buy_command(actor, market, refined, 1)

        assert len(commands) == 1
        assert isinstance(commands[0], PlaceBuyOrderCommand)
        assert commands[0].commodity_type is refined
        assert commands[0].price == 28  # ceil(22 * PROCUREMENT_BOOTSTRAP_MARGIN)
        assert commands[0].quantity == 1

    def test_traded_input_lifts_resting_ask(self, brain):
        """A resting ask is lifted directly without imputation."""
        actor = self._actor()
        refined = self._commodity("refined_chemicals")

        ask_order = Mock()
        ask_order.price = 8
        ask_order.actor = "someone_else"
        ask_order.timestamp = 0
        ask_order.cancelled = False

        market = Mock()
        market.sell_orders = {refined: [ask_order]}

        commands = brain._buy_command(actor, market, refined, 1)

        assert len(commands) == 1
        assert commands[0].price == 8

    def test_traded_input_with_price_signal_uses_avg(self, brain):
        """With no ask but a real price signal, the bid anchors on the average."""
        actor = self._actor()
        refined = self._commodity("refined_chemicals")

        market = Mock()
        market.sell_orders = {}
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = True
        market.get_avg_price.return_value = 12
        # A healthy market: demand is being met and the good still turns over.
        market.scarcity_pressure_for.return_value = 0.0
        market.get_30_day_average_volume.return_value = 5.0

        commands = brain._buy_command(actor, market, refined, 1)

        assert len(commands) == 1
        assert commands[0].price == 12

    def test_stalled_traded_input_gets_the_bootstrap_premium(self, brain):
        """A traded good with no ask, unmet demand and no turnover is bid up.

        The stale average (11) sits below every supplier's 1.2x entry
        threshold, because suppliers cost the good off the same average. The
        bid escalates by the bootstrap margin: ceil(11 * 1.25) = 14.
        """
        actor = self._actor(money=1000)
        refined = self._commodity("refined_chemicals")

        market = Mock()
        market.sell_orders = {}
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = True
        market.get_avg_price.return_value = 11
        market.scarcity_pressure_for.return_value = 3.0
        market.get_30_day_average_volume.return_value = 0.0

        commands = brain._buy_command(actor, market, refined, 1)

        assert len(commands) == 1
        assert commands[0].price == 14

    def test_stalled_premium_needs_both_scarcity_and_no_turnover(self, brain):
        """Scarcity pressure alone, with the good still trading, is not enough.

        The premium is inflationary where supply exists, so a good that still
        turns over keeps its average-anchored bid.
        """
        actor = self._actor(money=1000)
        refined = self._commodity("refined_chemicals")

        market = Mock()
        market.sell_orders = {}
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = True
        market.get_avg_price.return_value = 11
        market.scarcity_pressure_for.return_value = 3.0
        market.get_30_day_average_volume.return_value = 4.0

        commands = brain._buy_command(actor, market, refined, 1)

        assert len(commands) == 1
        assert commands[0].price == 11

    def test_stalled_premium_does_not_apply_when_an_ask_rests(self, brain):
        """A resting ask is still lifted, however starved the market looks."""
        actor = self._actor(money=1000)
        refined = self._commodity("refined_chemicals")

        ask_order = Mock()
        ask_order.price = 8
        ask_order.actor = "someone_else"
        ask_order.timestamp = 0
        ask_order.cancelled = False

        market = Mock()
        market.sell_orders = {refined: [ask_order]}
        market.scarcity_pressure_for.return_value = 3.0
        market.get_30_day_average_volume.return_value = 0.0

        commands = brain._buy_command(actor, market, refined, 1)

        assert commands[0].price == 8

    def test_imputation_falls_through_when_no_price_signal(self, brain):
        """_imputed_unit_cost recurses into the recipe when there is no price signal."""
        actor = self._actor()
        refined, chem = self._refined_chain(actor)

        market = Mock()

        def spread(commodity):
            return (None, 4) if commodity is chem else (None, None)

        market.get_bid_ask_spread.side_effect = spread
        market.has_price_signal.return_value = False
        market.get_avg_price.return_value = 10

        cost = brain._imputed_unit_cost(actor, market, refined, 0, frozenset(), {})
        assert cost == pytest.approx(22.0)

    def test_imputation_trusts_avg_when_price_signal_exists(self, brain):
        """With a real price signal, the average is trusted."""
        actor = self._actor()
        refined = self._commodity("refined_chemicals")

        market = Mock()
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = True
        market.get_avg_price.return_value = 10

        cost = brain._imputed_unit_cost(actor, market, refined, 0, frozenset(), {})
        assert cost == pytest.approx(10.0)


class TestDriveBidReference:
    """Consumer-side twin of TestImputedProcurementBids.

    The reference a standing drive bid escalates from anchors on the buyer's
    imputed replacement cost for never-traded goods, not the get_avg_price
    default, and is cached per market and turn.
    """

    @pytest.fixture
    def brain(self):
        # Base-class behavior, inherited by every ActorBrain subclass.
        return ActorBrain()

    @staticmethod
    def _commodity(cid):
        c = Mock(spec=CommodityDefinition)
        c.id = cid
        c.transportable = True
        return c

    @staticmethod
    def _actor():
        actor = Mock(spec=Actor)
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.has_quantity.return_value = False
        return actor

    def _refined_chain(self, actor):
        """refined_chemicals made from 3 chemicals with a live ask of 4.

        Imputed cost is labor 10 + 3 * 4 = 22.
        """
        refined = self._commodity("refined_chemicals")
        chem = self._commodity("chemicals")

        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.id = "refine_chemicals"
        process.inputs = {chem: 3}
        process.outputs = {refined: 1}
        process.tools_required = []
        process.facilities_required = []
        process.upkeep = {}
        process.capital = {}
        process.resource_attribute = None
        actor.sim.process_registry.all_processes.return_value = [process]
        return refined, chem

    @staticmethod
    def _market(chem):
        market = Mock()
        market.current_turn = 5
        market.drive_anchor_cache = {}

        def spread(commodity):
            # chemicals has a live ask as its cost anchor; nothing else does.
            return (None, 4) if commodity is chem else (None, None)

        market.get_bid_ask_spread.side_effect = spread
        market.get_avg_price.return_value = 10  # the no-trade default
        return market

    def test_never_traded_good_anchors_on_imputed_cost(self, brain):
        """With no price signal the anchor is the imputed cost of 22, not 10."""
        actor = self._actor()
        refined, chem = self._refined_chain(actor)
        market = self._market(chem)
        market.has_price_signal.return_value = False

        ref = brain._drive_bid_reference(actor, market, refined)

        assert ref == pytest.approx(22.0)

    def test_price_signal_uses_avg_directly(self, brain):
        """With a real trade history the average is used without imputation."""
        actor = self._actor()
        refined, chem = self._refined_chain(actor)
        market = self._market(chem)
        market.has_price_signal.return_value = True
        market.get_avg_price.return_value = 15

        ref = brain._drive_bid_reference(actor, market, refined)

        assert ref == pytest.approx(15.0)

    def test_anchor_is_cached_per_turn_and_recomputed_next_turn(self, brain):
        """The imputed anchor is memoized per market and turn.

        The first call on turn 5 imputes 22 and caches it. Removing the
        producing recipe makes the good un-imputable, but a same-turn call
        still returns the cached 22. The next turn recomputes and, with no
        recipe, falls back to the default. A floor beats no bid.
        """
        actor = self._actor()
        refined, chem = self._refined_chain(actor)
        market = self._market(chem)
        market.has_price_signal.return_value = False

        first = brain._drive_bid_reference(actor, market, refined)
        assert first == pytest.approx(22.0)
        assert market.drive_anchor_cache[refined.id] == (5, pytest.approx(22.0))

        # With the recipe gone, fresh imputation yields inf and falls back.
        actor.sim.process_registry.all_processes.return_value = []

        # Same turn: served from cache, still 22.
        assert brain._drive_bid_reference(actor, market, refined) == pytest.approx(22.0)

        # New turn: recompute; with no recipe, fall back to the default.
        market.current_turn = 6
        assert brain._drive_bid_reference(actor, market, refined) == pytest.approx(10.0)


class TestDepthAwareOutputValuation:
    """Recipe output is valued at the bid level that can actually absorb it.

    A market maker seeding an illiquid good posts one-unit probes far above
    fair value. Read as the top of book they look like demand, and producers
    pile into a good nobody buys. See _output_unit_value.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        return CommodityDefinition(id=cid, name=cid, transportable=True, description="")

    @staticmethod
    def _actor():
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.planet = Mock()
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.sim.process_registry.all_processes.return_value = []
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        return actor

    @staticmethod
    def _bidder():
        bidder = Mock()
        bidder.name = "Bidder"
        bidder.money = 1_000_000
        bidder.reserved_money = 0
        bidder.active_orders = {}
        return bidder

    @staticmethod
    def _process(output_commodity, quantity=1):
        """A recipe with no inputs, so its cost is exactly a turn of labor."""
        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.id = "make_output"
        process.inputs = {}
        process.outputs = {output_commodity: quantity}
        process.tools_required = []
        process.facilities_required = []
        process.resource_attribute = None
        return process

    def test_phantom_top_bid_is_ignored_in_favor_of_depth(self, brain):
        """A 1-unit bid at 150 over a deep book at 20 scores against 20."""
        actor = self._actor()
        medicine = self._commodity("medicine")
        market = Market()
        bidder = self._bidder()

        market.place_buy_order(bidder, medicine, 1, 150)  # discovery probe
        market.place_buy_order(bidder, medicine, 20, 20)  # real demand

        assert market.get_bid_ask_spread(medicine)[0] == 150

        score = brain._calculate_recipe_score(actor, market, self._process(medicine))

        # Output valued at 20, not 150, minus a turn of labor at the
        # government wage.
        assert score == pytest.approx(10.0)

    def test_thin_book_below_cost_triggers_exit(self, brain):
        """Depth pricing is what the exit check sees too, so it goes negative."""
        actor = self._actor()
        medicine = self._commodity("medicine")
        market = Market()
        bidder = self._bidder()

        market.place_buy_order(bidder, medicine, 1, 150)
        market.place_buy_order(bidder, medicine, 20, 5)

        raw = brain._calculate_recipe_score(
            actor, market, self._process(medicine), require_entry_margin=False
        )

        assert raw == pytest.approx(-5.0)

    def test_real_bids_over_a_discovery_floor_are_not_discarded(self, brain):
        """Two bids at 300 over a 1-credit floor value a 3-unit run near 200.

        Valuing at the last level hit scored this book at 1 and kept every
        producer out of a good with live downstream demand.
        """
        actor = self._actor()
        medicine = self._commodity("medicine")
        market = Market()
        bidder = self._bidder()

        market.place_buy_order(bidder, medicine, 2, 300)  # downstream demand
        market.place_buy_order(bidder, medicine, 20, 1)  # discovery floor
        process = self._process(medicine)
        actor.sim.process_registry.all_processes.return_value = [process]

        raw = brain._calculate_recipe_score(
            actor, market, process, require_entry_margin=False
        )

        # Make cost is a turn of labor (10), so each level is clipped at 15
        # and the sweep is (15 + 15 + 1) / 3 = 10.33, minus labor. The old
        # last-level valuation gave 1 - 10 = -9.
        assert raw == pytest.approx((15 + 15 + 1) / 3 - 10.0)

    def test_sweep_average_clips_each_level(self):
        market = Market()
        bidder = self._bidder()
        widget = self._commodity("widget")
        market.place_buy_order(bidder, widget, 1, 150)
        market.place_buy_order(bidder, widget, 5, 20)

        assert market.get_bid_sweep_average(widget, 3) == pytest.approx(190 / 3)
        assert market.get_bid_sweep_average(widget, 3, 20.0) == pytest.approx(20.0)
        assert market.get_bid_sweep_average(widget, 7) is None
        assert market.get_bid_sweep_average(widget, 0) is None

    def test_never_traded_good_scores_via_reference_price(self, brain):
        """With no bids and no history, fall back to the reference price."""
        actor = self._actor()
        widget = self._commodity("widget")
        market = Market()

        assert not market.has_price_signal(widget)

        score = brain._calculate_recipe_score(
            actor, market, self._process(widget, quantity=4)
        )

        # 4 units at the reference price of 10, minus a turn of labor.
        assert score == pytest.approx(30.0)

    def test_never_traded_good_scores_via_a_bootstrap_bid(self, brain):
        """A lone procurement bid still enables a cold-start intermediate.

        Depth cannot cover the sales horizon here, and there is no trade
        history, so the resting bid is the only demand signal there is.
        Refusing it would re-open the producer/consumer standoff.
        """
        actor = self._actor()
        refined = self._commodity("refined_chemicals")
        market = Market()
        market.place_buy_order(self._bidder(), refined, 1, 28)

        score = brain._calculate_recipe_score(actor, market, self._process(refined))

        assert score == pytest.approx(18.0)

    def test_liquid_good_still_uses_the_top_bid(self, brain):
        """Turnover above the horizon means the top bid is backed by flow."""
        actor = self._actor()
        food = self._commodity("food")
        market = Market()
        market.place_buy_order(self._bidder(), food, 1, 40)

        # 30 turns of heavy trade: the book is thin only because it is being
        # rebuilt around us, not because demand is absent.
        market.price_history[food] = [10] * 30
        market.volume_history[food] = [50] * 30

        score = brain._calculate_recipe_score(actor, market, self._process(food))

        assert score == pytest.approx(30.0)

    def test_never_traded_output_is_capped_at_its_make_cost(self, brain):
        """A discovery probe above any plausible production cost is discounted."""
        actor = self._actor()
        widget = self._commodity("widget")
        process = self._process(widget)
        process.id = "make_widget"
        actor.sim.process_registry.all_processes.return_value = [process]
        actor.inventory.has_quantity.return_value = True

        market = Market()
        market.place_buy_order(self._bidder(), widget, 1, 150)

        capped = brain._output_unit_value(actor, market, widget, 1.0, {})
        uncapped = market.get_bid_ask_spread(widget)[0]

        assert uncapped == 150
        # Making one widget costs a turn of labor, so 150 is not demand.
        assert capped == pytest.approx(GOVERNMENT_WAGE * 1.5)


class TestNetbackOutputValuation:
    """A thinly traded output is worth what a downstream consumer could pay.

    Tiers 2-4 of _output_unit_value price a good off its own thin book,
    which is circular for a raw material nobody has bought yet: the ore is
    cheap because no refiner exists, and no miner enters because the ore is
    cheap. See _netback_unit_value.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        return CommodityDefinition(id=cid, name=cid, transportable=True, description="")

    @staticmethod
    def _process(process_id, inputs, outputs):
        process = Mock(spec=ProcessDefinition)
        process.id = process_id
        process.inputs = inputs
        process.outputs = outputs
        process.upkeep = {}
        process.capital = {}
        process.tools_required = []
        process.facilities_required = []
        process.resource_attribute = None
        return process

    @staticmethod
    def _actor(processes):
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.planet = Mock()
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.sim.process_registry.all_processes.return_value = processes
        actor.inventory = Mock(spec=Inventory)
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.has_quantity.return_value = True
        return actor

    @staticmethod
    def _bidder():
        bidder = Mock()
        bidder.name = "Bidder"
        bidder.money = 1_000_000
        bidder.reserved_money = 0
        bidder.active_orders = {}
        return bidder

    def _ore_chain(self, refined_bid):
        """mine_ore (4 ore per run) then refine (2 ore -> 1 refined).

        ``refined`` is liquid at ``refined_bid``, ``ore`` has never traded.
        """
        ore = self._commodity("rare_earth_ore")
        refined = self._commodity("rare_earth")
        mine = self._process("mine_ore", {}, {ore: 4})
        refine = self._process("refine_ore", {ore: 2}, {refined: 1})
        actor = self._actor([mine, refine])

        market = Market()
        market.place_buy_order(self._bidder(), refined, 1, refined_bid)
        # Heavy turnover, so the refiner's output is valued at the top bid.
        market.price_history[refined] = [refined_bid] * 30
        market.volume_history[refined] = [50] * 30
        return actor, market, ore, refined

    def test_thin_ore_is_valued_at_the_refiners_netback(self, brain):
        """The refiner's margin, not the ore's own empty book, sets the value.

        One run of refine costs a turn of labor (10) plus 2 ore at the ore's
        own imputed make cost of 10/4 = 2.5. Substituting the ore out leaves
        10 of other costs, so the netback is (20 - 10) / (2 * 1.2).
        """
        actor, market, ore, _refined = self._ore_chain(refined_bid=20)

        value = brain._output_unit_value(actor, market, ore, 1.0, {})
        tier_only = brain._thin_book_unit_value(actor, market, ore, 3, {})

        assert value == pytest.approx(10.0 / 2.4)
        # The book-only tier prices the ore at 1.5x its make cost.
        assert tier_only == pytest.approx(2.5 * 1.5)
        assert value > tier_only

    def test_netback_is_capped_at_a_multiple_of_make_cost(self, brain):
        """A rich downstream margin cannot value ore at any price at all."""
        actor, market, ore, _refined = self._ore_chain(refined_bid=100)

        value = brain._output_unit_value(actor, market, ore, 1.0, {})

        # Uncapped the netback would be (100 - 10) / 2.4 = 37.5.
        assert value == pytest.approx(2.5 * NETBACK_VALUE_CAP)

    def test_netback_pulls_a_miner_into_a_good_nobody_buys(self, brain):
        """The whole point: the mining recipe now scores above zero."""
        actor, market, _ore, _refined = self._ore_chain(refined_bid=100)
        mine = actor.sim.process_registry.all_processes.return_value[0]

        score = brain._calculate_recipe_score(actor, market, mine)

        # 4 ore at the capped netback of 7.5, less a turn of labor.
        assert score == pytest.approx(4 * 7.5 - GOVERNMENT_WAGE)

    def test_a_cycle_in_the_recipe_graph_terminates(self, brain):
        """x to y to x is walked once and then cut by the visiting set."""
        x = self._commodity("x")
        y = self._commodity("y")
        make_x = self._process("make_x", {}, {x: 1})
        x_to_y = self._process("x_to_y", {x: 1}, {y: 1})
        y_to_x = self._process("y_to_x", {y: 1}, {x: 1})
        actor = self._actor([make_x, x_to_y, y_to_x])
        market = Market()

        value = brain._output_unit_value(actor, market, x, 1.0, {})

        assert math.isfinite(value)
        assert value > 0

    def test_netback_is_memoized_per_actor_turn(self, brain):
        """A second valuation in the same turn does not rewalk the graph."""
        actor, market, ore, _refined = self._ore_chain(refined_bid=100)
        cache = BrainCache()

        first = brain._netback_unit_value(actor, market, ore, {}, cache, frozenset(), 0)
        cache.netback_value[ore.id] = 999.0
        second = brain._netback_unit_value(
            actor, market, ore, {}, cache, frozenset(), 0
        )

        assert first == pytest.approx(2.5 * NETBACK_VALUE_CAP)
        assert second == pytest.approx(999.0)

    def _facility_cycle_chain(self):
        """A consumer behind a facility whose build runs back through itself.

        Mirrors the real electronics chain: ``make_computers`` needs an
        advanced_factory, and ``build_advanced_factory`` consumes electronics
        and precision parts. Here ``build_advanced_factory`` draws the widget
        under valuation plus a part whose only recipe needs the very facility
        being built, so costing the build is circular and returns inf.

        Returns the actor, market, the widget, and the consumer process.
        """
        widget = self._commodity("electronics")
        part = self._commodity("precision_parts")
        factory = CommodityDefinition(
            id="advanced_factory",
            name="advanced_factory",
            transportable=False,
            description="",
        )
        product = self._commodity("computers")

        make_widget = self._process("make_widget", {}, {widget: 1})
        make_part = self._process("make_part", {}, {part: 1})
        make_part.facilities_required = [factory]
        build_factory = self._process(
            "build_advanced_factory", {widget: 2, part: 1}, {factory: 1}
        )
        consumer = self._process("make_computers", {widget: 1}, {product: 1})
        consumer.facilities_required = [factory]

        actor = self._actor([make_widget, make_part, build_factory, consumer])
        by_id = {p.id: p for p in (make_widget, make_part, build_factory, consumer)}
        actor.sim.process_registry.get_process.side_effect = by_id.get
        # The actor owns its tools but not the factory, so the build cost is
        # the branch under test.
        actor.inventory.has_quantity.side_effect = (
            lambda commodity, quantity=1: commodity.id != factory.id
        )

        market = Market()
        market.place_buy_order(self._bidder(), product, 1, 400)
        market.price_history[product] = [400] * 30
        market.volume_history[product] = [50] * 30
        return actor, market, widget, consumer

    def test_netback_survives_a_facility_build_that_consumes_the_good(self, brain):
        """A circular facility build must not zero the good's demand signal.

        The consumer is reachable once its other costs are imputed without
        the amortized build, which is what a consumer already set up faces.
        """
        actor, market, widget, _consumer = self._facility_cycle_chain()

        value = brain._netback_unit_value(
            actor, market, widget, {}, None, frozenset(), 0
        )

        assert value > 0

    def test_default_impute_still_returns_inf_for_the_facility_cycle(self, brain):
        """Only the netback walk drops facilities; recipe costing does not."""
        actor, market, _widget, consumer = self._facility_cycle_chain()

        with_facilities = brain._impute_recipe_cost(
            actor, market, consumer, 0, frozenset(), {}
        )
        without_facilities = brain._impute_recipe_cost(
            actor, market, consumer, 0, frozenset(), {}, include_facilities=False
        )

        assert math.isinf(with_facilities)
        assert math.isfinite(without_facilities)


class TestStockAwareOutputValue:
    """Output value is discounted by the producer's own unsold stock."""

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        return CommodityDefinition(id=cid, name=cid, transportable=True, description="")

    @staticmethod
    def _process(output_commodity):
        process = Mock(spec=ProcessDefinition)
        process.id = "make_output"
        process.inputs = {}
        process.outputs = {output_commodity: 1}
        process.upkeep = {}
        process.capital = {}
        process.tools_required = []
        process.facilities_required = []
        process.resource_attribute = None
        return process

    @staticmethod
    def _actor(stock):
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.planet = Mock()
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.sim.process_registry.all_processes.return_value = []
        actor.inventory = Mock(spec=Inventory)
        actor.inventory.get_quantity.return_value = stock
        return actor

    @staticmethod
    def _market(commodity):
        """A book that absorbs 20 units at 20, so a run is worth 20."""
        market = Market()
        bidder = Mock()
        bidder.name = "Bidder"
        bidder.money = 1_000_000
        bidder.reserved_money = 0
        bidder.active_orders = {}
        market.place_buy_order(bidder, commodity, 20, 20)
        return market

    def test_zero_stock_is_undiscounted(self, brain):
        """The first entrant into a good nobody makes yet is unaffected."""
        medicine = self._commodity("medicine")
        actor = self._actor(stock=0)

        score = brain._calculate_recipe_score(
            actor, self._market(medicine), self._process(medicine)
        )

        assert score == pytest.approx(20.0 - GOVERNMENT_WAGE)

    def test_a_large_unsold_stock_scores_below_an_empty_one(self, brain):
        """With no turnover the reference stock is 30 units, so 100 bites."""
        medicine = self._commodity("medicine")
        market = self._market(medicine)
        process = self._process(medicine)

        empty = brain._calculate_recipe_score(self._actor(0), market, process)
        piled = brain._calculate_recipe_score(
            self._actor(100), market, process, require_entry_margin=False
        )

        assert piled < empty
        # 20 / (1 + 100/30) = 4.615, less a turn of labor.
        assert piled == pytest.approx(20.0 / (1 + 100 / 30) - GOVERNMENT_WAGE)

    def test_a_piled_up_producer_fails_the_exit_check(self, brain):
        """The exit check shares the valuation, so the stock brings exit on."""
        medicine = self._commodity("medicine")
        market = self._market(medicine)
        process = self._process(medicine)

        raw = brain._calculate_recipe_score(
            self._actor(100), market, process, require_entry_margin=False
        )

        assert raw < 0

    def test_market_turnover_raises_the_reference_stock(self, brain):
        """A stock the market turns over every few turns is not a pile."""
        medicine = self._commodity("medicine")
        market = self._market(medicine)
        market.price_history[medicine] = [20] * 30
        market.volume_history[medicine] = [40] * 30
        process = self._process(medicine)

        # Turnover of 40 a turn makes the reference 1200 units, so 100 is a
        # rounding error against it; the good is also liquid, so the top bid
        # of 20 is used directly.
        score = brain._calculate_recipe_score(self._actor(100), market, process)

        assert score == pytest.approx(20.0 / (1 + 100 / 1200) - GOVERNMENT_WAGE)

    def test_the_reserved_upkeep_unit_is_not_unsold_stock(self, brain):
        """The one upkeep unit the chosen recipe keeps back is never offered."""
        machinery = self._commodity("heavy_machinery")
        market = self._market(machinery)
        make_machinery = self._process(machinery)
        actor = self._actor(stock=1)

        # No chosen recipe: the single unit is stock like any other.
        without_upkeep = brain._calculate_recipe_score(
            actor, market, make_machinery, require_entry_margin=False
        )

        # The chosen recipe draws machinery as upkeep, so one unit is held
        # back rather than offered, and the discount is 1.0.
        farm = self._process(self._commodity("biomass"))
        farm.id = "farm_biomass"
        farm.upkeep = {machinery: 0.01}
        actor.sim.process_registry.get_process.return_value = farm
        brain.chosen_recipe_id = farm.id
        with_upkeep = brain._calculate_recipe_score(
            actor, market, make_machinery, require_entry_margin=False
        )

        assert without_upkeep == pytest.approx(20.0 / (1 + 1 / 30) - GOVERNMENT_WAGE)
        assert with_upkeep == pytest.approx(20.0 - GOVERNMENT_WAGE)


class TestBidDepthPricing:
    """Market.get_bid_price_at_depth walks the book instead of the top of it."""

    @staticmethod
    def _market_with_levels(commodity, levels):
        market = Market()
        bidder = Mock()
        bidder.name = "Bidder"
        bidder.money = 1_000_000
        bidder.reserved_money = 0
        bidder.active_orders = {}
        for price, quantity in levels:
            market.place_buy_order(bidder, commodity, quantity, price)
        return market

    def test_walks_down_to_the_level_that_covers_the_quantity(self):
        commodity = CommodityDefinition(
            id="medicine", name="medicine", transportable=True, description=""
        )
        market = self._market_with_levels(commodity, [(150, 1), (30, 2), (20, 10)])

        assert market.get_bid_price_at_depth(commodity, 1) == 150
        assert market.get_bid_price_at_depth(commodity, 3) == 30
        assert market.get_bid_price_at_depth(commodity, 4) == 20
        assert market.get_bid_price_at_depth(commodity, 13) == 20

    def test_returns_none_when_the_book_is_too_thin(self):
        commodity = CommodityDefinition(
            id="medicine", name="medicine", transportable=True, description=""
        )
        market = self._market_with_levels(commodity, [(150, 1)])

        assert market.get_bid_price_at_depth(commodity, 2) is None
        assert market.get_bid_price_at_depth(commodity, 0) is None

    def test_cancelled_bids_do_not_count_as_depth(self):
        commodity = CommodityDefinition(
            id="medicine", name="medicine", transportable=True, description=""
        )
        market = self._market_with_levels(commodity, [(150, 1), (20, 10)])
        deep_order = next(o for o in market.buy_orders[commodity] if o.price == 20)
        market.cancel_order(deep_order.order_id)

        assert market.get_bid_price_at_depth(commodity, 3) is None


class TestStuckRecipeAbandonment:
    """An industrialist drops a recipe it can never run.

    The exit check only fires on a negative score, so a recipe that scores
    well but is permanently missing an input was held forever while the actor
    fell through to government work every turn.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        c = Mock(spec=CommodityDefinition)
        c.id = cid
        c.transportable = True
        return c

    def _actor(self, quantities, facilities=()):
        """Actor whose inventory holds ``quantities`` (commodity id -> units)."""
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 1000
        actor.planet = Mock()
        actor.sim = Mock()
        actor.sim.current_turn = 100
        _wire_producer_index(actor.sim)
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.get_quantity.side_effect = lambda c: quantities.get(c.id, 0)
        actor.inventory.has_quantity.side_effect = (
            lambda c, q=1: quantities.get(c.id, 0) >= q
        )
        actor.can_execute.side_effect = lambda p: all(
            quantities.get(c.id, 0) >= q for c, q in p.requirements
        )
        return actor

    def _process(self, pid, inputs, facilities=()):
        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.capital = {}
        process.id = pid
        process.inputs = dict(inputs)
        process.outputs = {}
        process.tools_required = []
        process.facilities_required = list(facilities)
        process.resource_attribute = None
        process.requirements = [(c, q) for c, q in inputs.items()] + [
            (f, 1) for f in facilities
        ]
        return process

    def test_unrunnable_static_recipe_is_abandoned_and_cooled_down(self, brain):
        chem = self._commodity("chemicals")
        quantities = {}
        actor = self._actor(quantities)
        process = self._process("make_medicine", {chem: 3})
        actor.sim.process_registry.get_process.return_value = process

        brain.chosen_recipe_id = "make_medicine"
        for _ in range(RECIPE_STUCK_TURNS):
            brain._update_stuck_tracking(actor)
            assert brain.chosen_recipe_id == "make_medicine"

        brain._update_stuck_tracking(actor)

        assert brain.chosen_recipe_id is None
        assert brain.recipe_cooldown_until["make_medicine"] == (
            actor.sim.current_turn + RECIPE_COOLDOWN_TURNS
        )

    def test_procurement_progress_resets_the_stuck_counter(self, brain):
        chem = self._commodity("chemicals")
        quantities = {}
        actor = self._actor(quantities)
        process = self._process("make_medicine", {chem: 3})
        actor.sim.process_registry.get_process.return_value = process

        brain.chosen_recipe_id = "make_medicine"
        for turn in range(RECIPE_STUCK_TURNS * 3):
            # One unit trickles in every 10 turns: slow, but not stuck.
            if turn % 10 == 9:
                quantities["chemicals"] = quantities.get("chemicals", 0) + 1
            brain._update_stuck_tracking(actor)

        assert brain.chosen_recipe_id == "make_medicine"
        assert brain.recipe_cooldown_until == {}

    def test_facility_build_progress_is_not_stuck(self, brain):
        """Assembling a facility's build inputs counts as progress."""
        lab = self._commodity("chemistry_lab")
        lab.transportable = False
        bricks = self._commodity("simple_building_materials")
        quantities = {}
        actor = self._actor(quantities)
        process = self._process("make_medicine", {}, facilities=[lab])
        build = self._process("build_chemistry_lab", {bricks: 20})

        def get_process(pid):
            return {"make_medicine": process, "build_chemistry_lab": build}[pid]

        actor.sim.process_registry.get_process.side_effect = get_process
        actor.sim.process_registry.all_processes.return_value = [process, build]
        _wire_producer_index(actor.sim)

        brain.chosen_recipe_id = "make_medicine"
        for turn in range(RECIPE_STUCK_TURNS * 2):
            if turn % 5 == 4:
                quantities["simple_building_materials"] = (
                    quantities.get("simple_building_materials", 0) + 1
                )
            brain._update_stuck_tracking(actor)

        assert brain.chosen_recipe_id == "make_medicine"

    def test_runnable_recipe_is_never_stuck(self, brain):
        chem = self._commodity("chemicals")
        actor = self._actor({"chemicals": 5})
        process = self._process("make_medicine", {chem: 3})
        actor.sim.process_registry.get_process.return_value = process

        brain.chosen_recipe_id = "make_medicine"
        for _ in range(RECIPE_STUCK_TURNS * 2):
            brain._update_stuck_tracking(actor)

        assert brain.chosen_recipe_id == "make_medicine"
        assert brain.stuck_turns == 0

    def test_cooled_down_recipe_is_excluded_then_selectable_again(self, brain):
        chem = self._commodity("chemicals")
        actor = self._actor({})
        process = self._process("make_medicine", {chem: 3})
        actor.sim.process_registry.all_processes.return_value = [process]
        actor.sim.process_registry.get_process.return_value = process

        brain.recipe_cooldown_until["make_medicine"] = actor.sim.current_turn + 10
        assert brain._recipes_on_cooldown(actor) == {"make_medicine"}
        assert brain._select_new_recipe(actor) is None

        actor.sim.current_turn += 20
        assert brain._recipes_on_cooldown(actor) == set()
        assert brain.recipe_cooldown_until == {}


class TestIndustrialistLiquidation:
    """Stock left over from an abandoned recipe is offered back to the market.

    The recipe sweep only lists the outputs of the CURRENT recipe, so without
    an unconditional sweep an actor that switches or drops a line sits on the
    goods it already produced forever.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid, transportable=True):
        commodity = Mock(spec=CommodityDefinition)
        commodity.id = cid
        commodity.transportable = transportable
        return commodity

    @staticmethod
    def _process(pid, inputs, outputs, tools=(), facilities=()):
        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.capital = {}
        process.id = pid
        process.inputs = dict(inputs)
        process.outputs = dict(outputs)
        process.tools_required = list(tools)
        process.facilities_required = list(facilities)
        process.resource_attribute = None
        process.requirements = (
            tuple(inputs.items())
            + tuple((tool, 1) for tool in tools)
            + tuple((facility, 1) for facility in facilities)
        )
        return process

    @staticmethod
    def _actor(holdings, commodities):
        """Actor holding ``holdings`` (commodity id -> quantity), no drives."""
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.drives = []
        actor.planet = Mock()
        actor.sim = Mock()
        actor.sim.current_turn = 0
        _wire_producer_index(actor.sim)
        actor.sim.process_registry.all_processes.return_value = []
        actor.sim.commodity_registry.all_commodities.return_value = list(commodities)
        actor.sim.commodity_registry.get_commodity.return_value = None
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.get_quantity.side_effect = lambda c: holdings.get(c.id, 0)
        actor.inventory.get_available_quantity.side_effect = lambda c: holdings.get(
            c.id, 0
        )
        actor.inventory.has_quantity.side_effect = (
            lambda c, q=1: holdings.get(c.id, 0) >= q
        )

        market = actor.planet.market
        market.get_actor_orders.return_value = {"buy": [], "sell": []}
        market.sell_orders = {}
        market.buy_orders = {}
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = False
        market.get_avg_price.return_value = 10
        market.get_30_day_average_price.return_value = 10.0
        market.get_30_day_average_volume.return_value = 0.0
        market.get_bid_price_at_depth.return_value = None
        market.scarcity_pressure_for.return_value = 0.0
        return actor

    @staticmethod
    def _sell_orders(commands):
        return [cmd for cmd in commands if isinstance(cmd, PlaceSellOrderCommand)]

    def test_stock_from_an_abandoned_recipe_is_offered(self, brain):
        """Fuel produced under an old line is listed after switching recipes."""
        nova_fuel = self._commodity("nova_fuel")
        ore = self._commodity("common_metal_ore")
        metal = self._commodity("refined_metal")
        commodities = [nova_fuel, ore, metal]

        actor = self._actor({"nova_fuel": 40, "common_metal_ore": 3}, commodities)
        current = self._process("refine_metal", {ore: 3}, {metal: 1})
        actor.sim.process_registry.get_process.return_value = current
        brain.chosen_recipe_id = "refine_metal"

        sells = self._sell_orders(brain.decide_market_actions(actor))

        fuel_sells = [s for s in sells if s.commodity_type is nova_fuel]
        assert len(fuel_sells) == 1
        assert fuel_sells[0].quantity == 40

    def test_stock_is_offered_with_no_chosen_recipe(self, brain):
        """The sweep runs even when the actor holds no recipe at all."""
        nova_fuel = self._commodity("nova_fuel")
        actor = self._actor({"nova_fuel": 12}, [nova_fuel])
        actor.sim.process_registry.get_process.return_value = None
        brain.chosen_recipe_id = None

        sells = self._sell_orders(brain.decide_market_actions(actor))

        assert [(s.commodity_type, s.quantity) for s in sells] == [(nova_fuel, 12)]

    def test_recipe_inputs_tools_and_build_materials_are_kept(self, brain):
        """Nothing the current recipe or its facility build needs is sold."""
        ore = self._commodity("common_metal_ore")
        metal = self._commodity("refined_metal")
        tools = self._commodity("mining_tools")
        bricks = self._commodity("simple_building_materials")
        smelter = self._commodity("smelting_facility", transportable=False)
        nova_fuel = self._commodity("nova_fuel")
        commodities = [ore, metal, tools, bricks, nova_fuel]

        holdings = {
            "common_metal_ore": 9,
            "mining_tools": 5,
            "simple_building_materials": 7,
            "nova_fuel": 4,
        }
        actor = self._actor(holdings, commodities)

        build = self._process(
            "build_smelting_facility", {bricks: 10}, {smelter: 1}, tools=[tools]
        )
        current = self._process(
            "refine_metal",
            {ore: 3},
            {metal: 1},
            tools=[tools],
            facilities=[smelter],
        )

        def get_process(process_id):
            if process_id == "refine_metal":
                return current
            if process_id == "build_smelting_facility":
                return build
            return None

        actor.sim.process_registry.get_process.side_effect = get_process
        actor.sim.process_registry.all_processes.return_value = [build, current]
        brain.chosen_recipe_id = "refine_metal"

        sells = self._sell_orders(brain.decide_market_actions(actor))
        sold = {s.commodity_type for s in sells}

        assert ore not in sold
        assert tools not in sold
        assert bricks not in sold
        assert nova_fuel in sold

    def test_current_recipe_output_is_offered_once(self, brain):
        """The sweep does not duplicate the recipe sweep's own sell order."""
        ore = self._commodity("common_metal_ore")
        metal = self._commodity("refined_metal")
        actor = self._actor({"refined_metal": 6}, [ore, metal])
        current = self._process("refine_metal", {ore: 3}, {metal: 1})
        actor.sim.process_registry.get_process.return_value = current
        brain.chosen_recipe_id = "refine_metal"

        sells = self._sell_orders(brain.decide_market_actions(actor))

        metal_sells = [s for s in sells if s.commodity_type is metal]
        assert len(metal_sells) == 1
        assert metal_sells[0].quantity == 6

    def test_drive_stock_is_kept_to_the_drive_target(self, brain):
        """Personal-need goods are retained up to the drive's target."""
        food = self._commodity("food")
        actor = self._actor({"food": 10}, [food])
        actor.drives = [_StubDrive("food", [food], target=6)]
        actor.sim.process_registry.get_process.return_value = None
        brain.chosen_recipe_id = None

        sells = self._sell_orders(brain.decide_market_actions(actor))

        assert [(s.commodity_type, s.quantity) for s in sells] == [(food, 4)]


class TestNetbackInputBids:
    """Recipe inputs are bid against the value of the recipe's own output.

    Without this an input bid is priced off the input's own thin history, so
    a producer bidding 100 for medicine still rests 28 for the chemicals it
    needs and the tier below never starts. See _input_price_ceiling.
    """

    @pytest.fixture
    def brain(self):
        return IndustrialistBrain()

    @staticmethod
    def _commodity(cid):
        return CommodityDefinition(id=cid, name=cid, transportable=True, description="")

    @staticmethod
    def _participant(money=1_000_000, stock=1000):
        participant = Mock()
        participant.name = "Counterparty"
        participant.money = money
        participant.reserved_money = 0
        participant.active_orders = {}
        participant.inventory = Mock(spec=Inventory)
        participant.inventory.get_available_quantity.return_value = stock
        return participant

    def _actor(self, money=100_000):
        actor = Mock(spec=Actor)
        actor.name = "TestIndustrialist"
        actor.actor_type = ActorType.REGULAR
        actor.money = money
        actor.planet = Mock()
        actor.sim = Mock()
        actor.drives = []
        _wire_producer_index(actor.sim)
        actor.inventory = Mock(spec=Inventory)
        # Holding nothing, so the stock discount on output value is 1.0
        # unless a test sets a stock explicitly.
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.get_available_quantity.return_value = 0
        actor.inventory.has_quantity.return_value = False
        return actor

    @staticmethod
    def _process(pid, inputs, outputs, facilities=()):
        process = Mock(spec=ProcessDefinition)
        process.upkeep = {}
        process.capital = {}
        process.capital = {}
        process.id = pid
        process.inputs = dict(inputs)
        process.outputs = dict(outputs)
        process.tools_required = []
        process.facilities_required = list(facilities)
        process.resource_attribute = None
        process.requirements = list(inputs.items())
        return process

    def _medicine_chain(self, actor, market):
        """make_medicine from 2 refined_chemicals, itself made from 3 chemicals.

        chemicals rests an ask at 4, so imputed refined_chemicals is
        labor 10 + 3 * 4 = 22 and one run of make_medicine imputes to
        10 + 2 * 22 = 54.
        """
        medicine = self._commodity("medicine")
        refined = self._commodity("refined_chemicals")
        chemicals = self._commodity("chemicals")

        make_medicine = self._process("make_medicine", {refined: 2}, {medicine: 1})
        refine = self._process("refine_chemicals", {chemicals: 3}, {refined: 1})
        actor.sim.process_registry.all_processes.return_value = [make_medicine, refine]
        actor.sim.process_registry.get_process.side_effect = lambda pid: {
            "make_medicine": make_medicine,
            "refine_chemicals": refine,
        }.get(pid)
        actor.sim.commodity_registry.get_commodity.return_value = None

        market.place_sell_order(self._participant(), chemicals, 100, 4)
        return medicine, refined, chemicals, make_medicine

    @staticmethod
    def _demand_for(market, commodity, quantity, price):
        buyer = Mock()
        buyer.name = "Buyer"
        buyer.money = 10_000_000
        buyer.reserved_money = 0
        buyer.active_orders = {}
        market.place_buy_order(buyer, commodity, quantity, price)

    def test_resting_ask_is_lifted_at_the_ask(self, brain):
        """Supply that is already there is taken, ceiling or no ceiling."""
        actor = self._actor()
        market = Market()
        _, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, self._commodity("medicine"), 20, 100)
        market.place_sell_order(self._participant(), refined, 5, 9)

        ceiling = brain._input_price_ceiling(
            actor, market, refined, 2.0, 100.0, 54.0, {}
        )
        commands = brain._buy_command(actor, market, refined, 2, None, ceiling)

        assert not math.isinf(ceiling)
        assert [(c.price, c.quantity) for c in commands] == [(9, 2)]

    def test_ceiling_matches_the_netback_formula(self, brain):
        """The ceiling is (output value - other costs) / (qty * ENTRY_MARGIN)."""
        actor = self._actor()
        market = Market()
        medicine, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, medicine, 20, 100)

        memo = {}
        output_value = brain._recipe_output_value(actor, market, process, memo)
        recipe_cost = brain._impute_recipe_cost(
            actor, market, process, 0, frozenset(), memo
        )
        unit = brain._imputed_unit_cost(actor, market, refined, 0, frozenset(), memo)

        assert output_value == pytest.approx(100.0)
        assert recipe_cost == pytest.approx(54.0)
        assert unit == pytest.approx(22.0)

        expected = (output_value - (recipe_cost - 2 * unit)) / (2 * ENTRY_MARGIN)
        ceiling = brain._input_price_ceiling(
            actor, market, refined, 2.0, output_value, recipe_cost, memo
        )

        assert ceiling == pytest.approx(expected)
        assert ceiling == pytest.approx(37.5)

    def test_never_traded_input_opens_at_the_bootstrap_bid(self, brain):
        """With no pressure the bid is today's ceil(imputed * 1.25), under the ceiling."""
        actor = self._actor()
        market = Market()
        medicine, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, medicine, 20, 100)

        ceiling = brain._input_price_ceiling(
            actor, market, refined, 2.0, 100.0, 54.0, {}
        )
        commands = brain._buy_command(actor, market, refined, 2, None, ceiling)

        assert len(commands) == 1
        assert commands[0].price == 28  # ceil(22 * PROCUREMENT_BOOTSTRAP_MARGIN)
        assert commands[0].price <= ceiling

    def test_scarcity_pressure_escalates_the_bid_up_to_the_ceiling(self, brain):
        """Unfilled demand walks the bid up, and the ceiling stops it."""
        actor = self._actor()
        market = Market()
        medicine, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, medicine, 20, 100)

        ceiling = brain._input_price_ceiling(
            actor, market, refined, 2.0, 100.0, 54.0, {}
        )

        market.scarcity_pressure[refined] = 0.2
        mild = brain._buy_command(actor, market, refined, 2, None, ceiling)[0].price

        market.scarcity_pressure[refined] = 3.0
        starved = brain._buy_command(actor, market, refined, 2, None, ceiling)[0].price

        assert mild == 33  # ceil(27.5 * 1.2)
        assert starved == 37  # capped at the ceiling of 37.5
        assert starved <= ceiling

    def test_ceiling_falls_when_the_output_bid_falls(self, brain):
        """Recomputed from live market state every turn, never carried over."""
        actor = self._actor()
        market = Market()
        medicine, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, medicine, 20, 100)

        rich = brain._input_price_ceiling(
            actor,
            market,
            refined,
            2.0,
            brain._recipe_output_value(actor, market, process, {}),
            54.0,
            {},
        )

        for order in list(market.buy_orders[medicine]):
            market.cancel_order(order.order_id)
        self._demand_for(market, medicine, 20, 60)

        poor = brain._input_price_ceiling(
            actor,
            market,
            refined,
            2.0,
            brain._recipe_output_value(actor, market, process, {}),
            54.0,
            {},
        )

        assert rich == pytest.approx(37.5)
        assert poor == pytest.approx((60 - 10) / 2.4)
        assert poor < rich

    def test_no_ceiling_keeps_the_older_pricing(self, brain):
        """An un-netbackable input still gets today's bootstrap bid."""
        actor = self._actor()
        market = Market()
        _, refined, _, _ = self._medicine_chain(actor, market)

        commands = brain._buy_command(actor, market, refined, 2, None, math.inf)

        assert commands[0].price == 28

    def test_facility_build_input_uses_the_amortization_horizon(self, brain):
        """Glass for a chemistry lab is netbacked over the horizon it is amortized over.

        A longer horizon spreads the build over more runs, so each run's glass
        draw is smaller and one unit of glass can carry a higher price.
        """
        actor = self._actor()
        market = Market()
        medicine = self._commodity("medicine")
        glass = self._commodity("glass")
        lab = CommodityDefinition(
            id="chemistry_lab", name="lab", transportable=False, description=""
        )

        make_medicine = self._process("make_medicine", {}, {medicine: 1}, [lab])
        build_lab = self._process("build_chemistry_lab", {glass: 20}, {lab: 1})
        actor.sim.process_registry.all_processes.return_value = [
            make_medicine,
            build_lab,
        ]
        actor.sim.process_registry.get_process.side_effect = lambda pid: {
            "make_medicine": make_medicine,
            "build_chemistry_lab": build_lab,
        }.get(pid)
        market.place_sell_order(self._participant(), glass, 1000, 5)
        self._demand_for(market, medicine, 20, 100)

        ceilings = []
        for horizon in (200, 400):
            brain.facility_amortization_horizon = horizon
            memo = {}
            output_value = brain._recipe_output_value(
                actor, market, make_medicine, memo
            )
            recipe_cost = brain._impute_recipe_cost(
                actor, market, make_medicine, 0, frozenset(), memo
            )
            ceilings.append(
                brain._input_price_ceiling(
                    actor,
                    market,
                    glass,
                    20 / horizon,
                    output_value,
                    recipe_cost,
                    memo,
                )
            )

        assert ceilings[1] > ceilings[0]
        # horizon 200: build cost 10 + 20*5 = 110, so one run costs
        # 10 + 110/200 = 10.55 and draws 0.1 glass.
        assert ceilings[0] == pytest.approx((100 - (10.55 - 0.1 * 5)) / (0.1 * 1.2))

    def test_facility_build_input_ceiling_is_capped_at_a_cost_multiple(self, brain):
        """A build material's netback is bounded by BUILD_INPUT_CEILING_CAP.

        The uncapped figure divides a whole run's margin by a fractional draw
        and lands in the hundreds per brick; the cap keeps the bid within a
        small multiple of what the material itself costs.
        """
        actor = self._actor()
        market = Market()
        medicine = self._commodity("medicine")
        glass = self._commodity("glass")
        lab = CommodityDefinition(
            id="chemistry_lab", name="lab", transportable=False, description=""
        )
        make_medicine = self._process("make_medicine", {}, {medicine: 1}, [lab])
        build_lab = self._process("build_chemistry_lab", {glass: 20}, {lab: 1})
        actor.sim.process_registry.all_processes.return_value = [
            make_medicine,
            build_lab,
        ]
        actor.sim.process_registry.get_process.side_effect = lambda pid: {
            "make_medicine": make_medicine,
            "build_chemistry_lab": build_lab,
        }.get(pid)
        market.place_sell_order(self._participant(), glass, 1000, 5)
        self._demand_for(market, medicine, 20, 100)
        brain.facility_amortization_horizon = 200

        memo = {}
        output_value = brain._recipe_output_value(actor, market, make_medicine, memo)
        recipe_cost = brain._impute_recipe_cost(
            actor, market, make_medicine, 0, frozenset(), memo
        )
        args = (actor, market, glass, 20 / 200, output_value, recipe_cost, memo)
        uncapped = brain._input_price_ceiling(*args)
        capped = brain._input_price_ceiling(*args, BUILD_INPUT_CEILING_CAP)

        assert uncapped > 100
        assert capped == pytest.approx(5 * BUILD_INPUT_CEILING_CAP)

    def test_build_input_cap_anchors_on_recipe_cost_not_market_average(self, brain):
        """The cap follows what the material costs to make, not its quotes.

        A cap on the market average rose with every fill the bid caused. With
        a make_glass recipe present, the cap is a multiple of that recipe's
        unit cost even while the market quotes glass far higher.
        """
        actor = self._actor()
        market = Market()
        medicine = self._commodity("medicine")
        glass = self._commodity("glass")
        silica = self._commodity("silica")
        lab = CommodityDefinition(
            id="chemistry_lab", name="lab", transportable=False, description=""
        )
        make_medicine = self._process("make_medicine", {}, {medicine: 1}, [lab])
        build_lab = self._process("build_chemistry_lab", {glass: 20}, {lab: 1})
        make_glass = self._process("make_glass", {silica: 3}, {glass: 2})
        registry = {
            "make_medicine": make_medicine,
            "build_chemistry_lab": build_lab,
            "make_glass": make_glass,
        }
        actor.sim.process_registry.all_processes.return_value = list(registry.values())
        actor.sim.process_registry.get_process.side_effect = registry.get
        market.place_sell_order(self._participant(), silica, 1000, 2)
        market.place_sell_order(self._participant(), glass, 1000, 50)
        self._demand_for(market, medicine, 20, 100)
        brain.facility_amortization_horizon = 200

        memo = {}
        output_value = brain._recipe_output_value(actor, market, make_medicine, memo)
        recipe_cost = brain._impute_recipe_cost(
            actor, market, make_medicine, 0, frozenset(), memo
        )
        capped = brain._input_price_ceiling(
            actor,
            market,
            glass,
            20 / 200,
            output_value,
            recipe_cost,
            memo,
            BUILD_INPUT_CEILING_CAP,
        )

        # make_glass: 10 labor + 3 silica at 2 = 16 per run, 2 glass per run.
        assert capped == pytest.approx(8 * BUILD_INPUT_CEILING_CAP)
        assert capped < 50

    def test_output_value_helper_matches_the_recipe_score(self, brain):
        """The extracted helper reproduces what the score computes."""
        actor = self._actor()
        market = Market()
        medicine, refined, _, process = self._medicine_chain(actor, market)
        self._demand_for(market, medicine, 20, 100)

        memo = {}
        output_value = brain._recipe_output_value(actor, market, process, memo)
        cost = brain._impute_recipe_cost(actor, market, process, 0, frozenset(), memo)
        score = brain._calculate_recipe_score(actor, market, process)

        assert score == pytest.approx(output_value - cost)
        assert score == pytest.approx(46.0)
