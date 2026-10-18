from unittest.mock import Mock

import pytest

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.commands import (
    PlaceBuyOrderCommand,
    ProcessCommand,
)
from spacesim2.core.commodity import (
    CommodityDefinition,
    CommodityRegistry,
    Inventory,
)
from spacesim2.core.drives.food_drive import FoodDrive
from spacesim2.core.process import ProcessDefinition


def _real_registry() -> CommodityRegistry:
    """Registry loaded from the project's commodity data."""
    registry = CommodityRegistry()
    registry.load_from_file("data/commodities.yaml")
    return registry


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
    registry.get_process.side_effect = lambda process_id: next(
        (p for p in registry.all_processes.return_value if p.id == process_id), None
    )


class TestColonistBrainToolMarket:
    """Colonist brain tool-market behavior."""

    @pytest.fixture
    def mock_actor(self):
        """Mock actor with a mocked planet, market, sim, and inventory."""
        actor = Mock(spec=Actor)
        actor.name = "TestColonist"
        actor.actor_type = ActorType.REGULAR
        actor.money = 100
        actor.planet = Mock()
        actor.planet.market = Mock()
        # Shared quote-table plumbing normally set up by Market.__init__.
        actor.planet.market.quote_version = 0
        actor.planet.market.shared_quote_table = None
        actor.sim = Mock()
        _wire_producer_index(actor.sim)
        actor.inventory = Mock(spec=Inventory)
        # No drives: these tests cover tool buying, which is not a drive-backed
        # need. Drive-backed demand is covered separately.
        actor.drives = []
        return actor

    @pytest.fixture
    def brain(self):
        return ColonistBrain()

    @pytest.fixture
    def mock_commodities(self):
        """Mock commodities keyed by id."""
        food = Mock(spec=CommodityDefinition)
        food.id = "food"
        food.transportable = True

        biomass = Mock(spec=CommodityDefinition)
        biomass.id = "biomass"
        biomass.transportable = True

        tools = Mock(spec=CommodityDefinition)
        tools.id = "simple_tools"
        tools.transportable = True

        common_metal = Mock(spec=CommodityDefinition)
        common_metal.id = "common_metal"
        common_metal.transportable = True

        return {
            "food": food,
            "biomass": biomass,
            "simple_tools": tools,
            "common_metal": common_metal,
        }

    def test_calculates_turn_opportunity_cost_minimum(self, brain, mock_actor):
        """Opportunity cost is the government wage with no planet or no profit."""
        actor_no_planet = Mock(spec=Actor)
        actor_no_planet.planet = None
        assert brain.labor_opportunity_cost(actor_no_planet) == 10

        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, None)
        mock_actor.planet.market.get_avg_price.return_value = 10
        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = False

        result = brain.labor_opportunity_cost(mock_actor)
        assert result == 10  # government wage

    def test_calculates_turn_opportunity_cost_from_profitable_process(
        self, brain, mock_actor
    ):
        """Opportunity cost is the profit of the best available process."""
        input_commodity = Mock()
        input_commodity.id = "input1"
        output_commodity = Mock()
        output_commodity.id = "output1"

        process = Mock(spec=ProcessDefinition)
        process.id = "profitable_process"
        process.inputs = {input_commodity: 1}
        process.outputs = {output_commodity: 1}
        # Precomputed flattenings normally built by __post_init__.
        process.inputs_items = ((input_commodity, 1),)
        process.outputs_items = ((output_commodity, 1),)
        process.resource_attribute = None
        process.relevant_skills = []

        mock_actor.sim.process_registry.all_processes.return_value = [process]
        mock_actor.can_execute_process.return_value = True

        # Input asks 5, output bids 30, so profit is 25.
        def get_spread(commodity):
            if commodity == input_commodity:
                return (None, 5)
            if commodity == output_commodity:
                return (30, None)
            return (None, None)

        mock_actor.planet.market.get_bid_ask_spread.side_effect = get_spread
        mock_actor.planet.market.get_avg_price.return_value = 10

        result = brain.labor_opportunity_cost(mock_actor)
        assert result == 25

    def test_calculates_tool_willingness_to_pay(
        self, brain, mock_actor, mock_commodities
    ):
        """Tool willingness to pay is input cost plus opportunity cost."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity

        # Metal asks 15 each.
        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, 15)
        mock_actor.planet.market.get_avg_price.return_value = 15

        # No profitable processes, so opportunity cost is the wage of 10.
        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = False

        willingness = brain._calculate_tool_willingness_to_pay(mock_actor)

        # input cost 15 * 2 = 30, plus opportunity cost 10
        assert willingness == 40

    def test_skips_making_tools_when_market_price_is_lower(
        self, brain, mock_actor, mock_commodities
    ):
        """The actor does not make tools when the market ask is at or below WTP."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity

        def get_quantity(commodity):
            if commodity == mock_commodities["simple_tools"]:
                return 0
            if commodity == mock_commodities["food"]:
                return 10
            return 10

        mock_actor.inventory.get_quantity.side_effect = get_quantity

        # Metal asks 10, so willingness is 10 * 2 + 10 = 30. Tools ask 25.
        def get_spread(commodity):
            if commodity == mock_commodities["common_metal"]:
                return (None, 10)
            if commodity == mock_commodities["simple_tools"]:
                return (None, 25)
            return (None, None)

        mock_actor.planet.market.get_bid_ask_spread.side_effect = get_spread
        mock_actor.planet.market.get_avg_price.return_value = 10

        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = True

        action = brain.decide_economic_action(mock_actor)

        if isinstance(action, ProcessCommand):
            assert action.process_id != "make_simple_tools"

    def test_makes_tools_when_market_price_too_high(
        self, brain, mock_actor, mock_commodities
    ):
        """The actor makes tools when the market ask is above WTP."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity

        def get_quantity(commodity):
            if commodity == mock_commodities["simple_tools"]:
                return 0
            if commodity == mock_commodities["food"]:
                return 10
            return 10

        mock_actor.inventory.get_quantity.side_effect = get_quantity
        # The food gate reads the FoodDrive pantry, so this actor needs one.
        mock_actor.inventory.get_available_quantity.return_value = 10
        mock_actor.drives = [FoodDrive(_real_registry())]

        # Metal asks 10, so willingness is 10 * 2 + 10 = 30. Tools ask 50.
        def get_spread(commodity):
            if commodity == mock_commodities["common_metal"]:
                return (None, 10)
            if commodity == mock_commodities["simple_tools"]:
                return (None, 50)
            return (None, None)

        mock_actor.planet.market.get_bid_ask_spread.side_effect = get_spread
        mock_actor.planet.market.get_avg_price.return_value = 10

        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = True

        action = brain.decide_economic_action(mock_actor)

        assert isinstance(action, ProcessCommand)
        assert action.process_id == "make_simple_tools"

    def test_places_buy_order_for_tools_at_willingness_to_pay_when_no_sellers(
        self, brain, mock_actor, mock_commodities
    ):
        """With no asks, the actor bids for tools at its willingness to pay."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity
        mock_actor.sim.commodity_registry.all_commodities.return_value = [
            mock_commodities["simple_tools"]
        ]

        mock_actor.planet.market.sell_orders = {mock_commodities["simple_tools"]: []}
        mock_actor.planet.market.buy_orders = {mock_commodities["simple_tools"]: []}
        mock_actor.planet.market.get_actor_orders.return_value = {"buy": [], "sell": []}

        mock_actor.inventory.get_quantity.return_value = 0
        mock_actor.inventory.get_available_quantity.return_value = 0

        # Metal asks 15 each.
        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, 15)
        mock_actor.planet.market.get_avg_price.return_value = 15

        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = False

        commands = brain.decide_market_actions(mock_actor)

        buy_commands = [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]
        assert len(buy_commands) == 1

        buy_cmd = buy_commands[0]
        assert buy_cmd.commodity_type == mock_commodities["simple_tools"]
        assert buy_cmd.price == 40  # 15 * 2 + 10

    def test_places_buy_order_at_market_price_when_below_willingness(
        self, brain, mock_actor, mock_commodities
    ):
        """The actor bids at the market ask when it is below willingness to pay."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity
        mock_actor.sim.commodity_registry.all_commodities.return_value = [
            mock_commodities["simple_tools"]
        ]

        sell_order = Mock()
        sell_order.price = 25
        sell_order.actor = Mock()  # a different actor
        sell_order.timestamp = 0
        sell_order.cancelled = False

        mock_actor.planet.market.sell_orders = {
            mock_commodities["simple_tools"]: [sell_order]
        }
        mock_actor.planet.market.buy_orders = {mock_commodities["simple_tools"]: []}
        mock_actor.planet.market.get_actor_orders.return_value = {"buy": [], "sell": []}

        mock_actor.inventory.get_quantity.return_value = 0
        mock_actor.inventory.get_available_quantity.return_value = 0

        # Metal asks 15, so willingness is 15 * 2 + 10 = 40.
        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, 15)
        mock_actor.planet.market.get_avg_price.return_value = 15

        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = False

        commands = brain.decide_market_actions(mock_actor)

        buy_commands = [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]
        assert len(buy_commands) == 1

        buy_cmd = buy_commands[0]
        assert buy_cmd.commodity_type == mock_commodities["simple_tools"]
        assert buy_cmd.price == 25  # the market ask, not willingness

    def test_does_not_buy_tools_when_price_exceeds_willingness(
        self, brain, mock_actor, mock_commodities
    ):
        """The actor places no tool bid when the ask exceeds willingness to pay."""

        def get_commodity(name):
            return mock_commodities.get(name)

        mock_actor.sim.commodity_registry.get_commodity.side_effect = get_commodity
        mock_actor.sim.commodity_registry.all_commodities.return_value = [
            mock_commodities["simple_tools"]
        ]

        sell_order = Mock()
        sell_order.price = 100
        sell_order.actor = Mock()
        sell_order.timestamp = 0
        sell_order.cancelled = False

        mock_actor.planet.market.sell_orders = {
            mock_commodities["simple_tools"]: [sell_order]
        }
        mock_actor.planet.market.buy_orders = {mock_commodities["simple_tools"]: []}
        mock_actor.planet.market.get_actor_orders.return_value = {"buy": [], "sell": []}

        mock_actor.inventory.get_quantity.return_value = 0
        mock_actor.inventory.get_available_quantity.return_value = 0

        # Metal asks 15, so willingness is 15 * 2 + 10 = 40.
        mock_actor.planet.market.get_bid_ask_spread.return_value = (None, 15)
        mock_actor.planet.market.get_avg_price.return_value = 15

        mock_actor.sim.process_registry.all_processes.return_value = []
        mock_actor.can_execute_process.return_value = False

        commands = brain.decide_market_actions(mock_actor)

        tool_buy_commands = [
            c
            for c in commands
            if isinstance(c, PlaceBuyOrderCommand)
            and c.commodity_type == mock_commodities["simple_tools"]
        ]
        assert len(tool_buy_commands) == 0
