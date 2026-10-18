"""Durable goods: productive capital and the prefab dwelling.

Computers are capital an industrialist holds for an output bonus; prefab
housing is a dwelling the shelter need holds. Both are bought once and used
over many turns, so the brain values them per use, not per purchase.
"""

from unittest.mock import Mock, patch

import pytest

from spacesim2.analysis.summary import compute_summary
from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.industrialist import (
    CAPITAL_BUFFER,
    ENTRY_MARGIN,
    IndustrialistBrain,
)
from spacesim2.core.commands import (
    CAPITAL_BREAK_PROBABILITY,
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
    ProcessCommand,
)
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry, Inventory
from spacesim2.core.drives.prosperity_drive import CATEGORY_NAMES
from spacesim2.core.drives.shelter_drive import (
    BUILDING_MATERIALS_NAME,
    PREFAB_HOUSING_NAME,
    PREFAB_SERVINGS,
    ShelterDrive,
)
from spacesim2.core.process import ProcessDefinition
from spacesim2.core.simulation import Simulation
from tests.helpers import get_actor


def _commodity(cid: str, transportable: bool = True) -> CommodityDefinition:
    return CommodityDefinition(
        id=cid, name=cid, transportable=transportable, description=cid
    )


def _process(capital=None, upkeep=None) -> ProcessDefinition:
    """A one-input, one-output recipe gated on a facility."""
    return ProcessDefinition(
        id="make_widgets",
        name="Make Widgets",
        inputs={_commodity("metal"): 1},
        outputs={_commodity("widget"): 4},
        tools_required=[],
        facilities_required=[_commodity("workshop", transportable=False)],
        labor=1,
        description="test recipe",
        upkeep=upkeep or {},
        capital=capital or {},
    )


def _process_actor(process: ProcessDefinition, holdings: dict) -> Actor:
    """Actor with a real inventory holding ``holdings`` and a mocked sim."""
    actor = get_actor("Maker")
    actor.sim = Mock()
    actor.sim.process_registry.get_process.return_value = process
    actor.sim.data_logger = None
    actor.planet = None
    actor.inventory = Inventory()
    for commodity, quantity in holdings.items():
        actor.inventory.add_commodity(commodity, quantity)
    return actor


class TestProcessDefinitionCapital:
    def test_bonus_must_be_a_fraction(self):
        with pytest.raises(ValueError):
            _process(capital={_commodity("computers"): 1.5})

    def test_data_file_gives_every_facility_recipe_a_computer(self):
        """Facility-gated recipes take capital; gathering and hand work do not."""
        registry = CommodityRegistry()
        registry.load_from_file("data/commodities.yaml")
        from spacesim2.core.process import ProcessRegistry

        processes = ProcessRegistry(registry)
        processes.load_from_file("data/processes.yaml")

        for process in processes.all_processes():
            if process.facilities_required:
                assert [c.id for c in process.capital] == ["computers"]
            else:
                assert not process.capital


class TestCapitalInExecution:
    def test_bonus_raises_output_when_the_good_is_held(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        metal = next(iter(process.inputs))
        widget = next(iter(process.outputs))
        workshop = process.facilities_required[0]
        actor = _process_actor(process, {metal: 1, workshop: 1, computers: 1})

        with patch("spacesim2.core.commands.random.random", return_value=0.9):
            assert ProcessCommand("make_widgets").execute(actor) is True

        # 4 base output, +25% for the held computer.
        assert actor.inventory.get_quantity(widget) == 5
        assert actor.inventory.get_quantity(computers) == 1

    def test_no_bonus_without_the_good(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        metal = next(iter(process.inputs))
        widget = next(iter(process.outputs))
        workshop = process.facilities_required[0]
        actor = _process_actor(process, {metal: 1, workshop: 1})

        with patch("spacesim2.core.commands.random.random", return_value=0.9):
            assert ProcessCommand("make_widgets").execute(actor) is True

        assert actor.inventory.get_quantity(widget) == 4

    def test_bonuses_sum(self):
        computers = _commodity("computers")
        robots = _commodity("robots")
        process = _process(capital={computers: 0.25, robots: 0.25})
        metal = next(iter(process.inputs))
        widget = next(iter(process.outputs))
        workshop = process.facilities_required[0]
        actor = _process_actor(
            process, {metal: 1, workshop: 1, computers: 1, robots: 1}
        )

        with patch("spacesim2.core.commands.random.random", return_value=0.9):
            ProcessCommand("make_widgets").execute(actor)

        assert actor.inventory.get_quantity(widget) == 6  # 4 * 1.5

    def test_capital_breaks_on_a_low_roll(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        metal = next(iter(process.inputs))
        workshop = process.facilities_required[0]
        actor = _process_actor(process, {metal: 5, workshop: 1, computers: 1})

        with patch(
            "spacesim2.core.commands.random.random",
            return_value=CAPITAL_BREAK_PROBABILITY / 2,
        ):
            ProcessCommand("make_widgets").execute(actor)

        assert actor.inventory.get_quantity(computers) == 0

    def test_capital_is_not_consumed_on_a_high_roll(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        metal = next(iter(process.inputs))
        workshop = process.facilities_required[0]
        actor = _process_actor(process, {metal: 5, workshop: 1, computers: 1})

        with patch("spacesim2.core.commands.random.random", return_value=0.9):
            ProcessCommand("make_widgets").execute(actor)

        assert actor.inventory.get_quantity(computers) == 1


class TestCapitalValuation:
    @staticmethod
    def _market():
        market = Mock()
        market.sell_orders = {}
        market.buy_orders = {}
        market.get_actor_orders.return_value = {"buy": [], "sell": []}
        market.actor_orders = {}
        market.get_bid_ask_spread.return_value = (None, None)
        market.has_price_signal.return_value = False
        market.get_avg_price.return_value = 10
        market.get_30_day_average_price.return_value = 10.0
        market.get_30_day_average_volume.return_value = 0.0
        market.get_bid_price_at_depth.return_value = None
        market.scarcity_pressure_for.return_value = 0.0
        return market

    @staticmethod
    def _actor(holdings):
        actor = Mock(spec=Actor)
        actor.name = "Maker"
        actor.actor_type = ActorType.REGULAR
        actor.money = 10000
        actor.drives = []
        actor.planet = Mock()
        actor.sim = Mock()
        actor.sim.current_turn = 0
        actor.sim.process_registry.all_processes.return_value = []
        actor.sim.process_registry.get_processes_producing.return_value = []
        actor.sim.commodity_registry.all_commodities.return_value = list(holdings)
        actor.sim.commodity_registry.get_commodity.return_value = None
        actor.inventory = Mock(spec=Inventory)
        actor.inventory.get_quantity.side_effect = lambda c: holdings.get(c, 0)
        actor.inventory.get_available_quantity.side_effect = lambda c: holdings.get(
            c, 0
        )
        actor.inventory.has_quantity.side_effect = (
            lambda c, q=1: holdings.get(c, 0) >= q
        )
        return actor

    def test_willingness_to_pay_is_the_bonus_over_the_expected_life(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        brain = IndustrialistBrain()
        brain.facility_amortization_horizon = 400
        actor = self._actor({})
        market = self._market()
        brain._recipe_output_value = Mock(return_value=100.0)

        wtp = brain._capital_willingness_to_pay(actor, market, process, computers, None)

        expected_uses = 1.0 / CAPITAL_BREAK_PROBABILITY  # 200, below the horizon
        assert wtp == pytest.approx(100.0 * 0.25 * expected_uses / ENTRY_MARGIN)

    def test_a_short_horizon_caps_the_expected_life(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        brain = IndustrialistBrain()
        brain.facility_amortization_horizon = 50
        actor = self._actor({})
        market = self._market()
        brain._recipe_output_value = Mock(return_value=100.0)

        wtp = brain._capital_willingness_to_pay(actor, market, process, computers, None)

        assert wtp == pytest.approx(100.0 * 0.25 * 50 / ENTRY_MARGIN)

    def test_output_value_counts_the_bonus_only_while_the_good_is_held(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        brain = IndustrialistBrain()
        market = self._market()
        market.get_bid_ask_spread.return_value = (20, None)
        market.get_30_day_average_volume.return_value = 100.0

        without = brain._recipe_output_value(self._actor({}), market, process, {})
        with_capital = brain._recipe_output_value(
            self._actor({computers: 1}), market, process, {}
        )

        assert with_capital == pytest.approx(without * 1.25)

    def test_one_unit_is_bought_and_never_offered(self):
        computers = _commodity("computers")
        process = _process(capital={computers: 0.25})
        brain = IndustrialistBrain()
        brain.chosen_recipe_id = "make_widgets"
        widget = next(iter(process.outputs))

        actor = self._actor({widget: 10})
        actor.sim.process_registry.get_process.return_value = process
        actor.sim.commodity_registry.all_commodities.return_value = [
            computers,
            widget,
        ]
        actor.planet.market = self._market()

        commands = brain.decide_market_actions(actor)
        buys = [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]
        computer_buys = [c for c in buys if c.commodity_type is computers]
        assert len(computer_buys) == 1
        assert computer_buys[0].quantity == CAPITAL_BUFFER

        # Holding one, the actor neither buys another nor lists the one it has.
        holder = self._actor({computers: 1, widget: 10})
        holder.sim.process_registry.get_process.return_value = process
        holder.sim.commodity_registry.all_commodities.return_value = [
            computers,
            widget,
        ]
        holder.planet.market = self._market()

        commands = brain.decide_market_actions(holder)
        assert not [
            c
            for c in commands
            if isinstance(c, PlaceBuyOrderCommand) and c.commodity_type is computers
        ]
        assert not [
            c
            for c in commands
            if isinstance(c, PlaceSellOrderCommand) and c.commodity_type is computers
        ]


def _shelter_registry() -> CommodityRegistry:
    registry = CommodityRegistry()
    for cid in (BUILDING_MATERIALS_NAME, PREFAB_HOUSING_NAME):
        registry.add_commodity(_commodity(cid))
    return registry


class TestShelterServings:
    def test_prefab_serves_many_events(self):
        drive = ShelterDrive(_shelter_registry())
        assert drive.material_servings(PREFAB_HOUSING_NAME) == PREFAB_SERVINGS
        assert drive.material_servings(BUILDING_MATERIALS_NAME) == 1.0

    def test_cheapest_ask_compares_per_serving(self):
        registry = _shelter_registry()
        drive = ShelterDrive(registry)
        bricks = registry.get_commodity(BUILDING_MATERIALS_NAME)
        prefab = registry.get_commodity(PREFAB_HOUSING_NAME)
        brain = IndustrialistBrain()
        actor = get_actor("Buyer")

        market = Mock()
        market.actor_orders = {}
        asks = {bricks: 15, prefab: 75}
        market.get_bid_ask_spread.side_effect = lambda c: (None, asks[c])

        # 75 for ten events beats 15 for one.
        chosen, ask = brain._cheapest_material_ask(
            actor, market, [bricks, prefab], None, drive
        )
        assert chosen is prefab
        assert ask == 75

        # At 200 the prefab costs 20 an event and the brick wins.
        asks[prefab] = 200
        chosen, ask = brain._cheapest_material_ask(
            actor, market, [bricks, prefab], None, drive
        )
        assert chosen is bricks
        assert ask == 15

    def test_without_a_drive_the_comparison_is_per_unit(self):
        registry = _shelter_registry()
        bricks = registry.get_commodity(BUILDING_MATERIALS_NAME)
        prefab = registry.get_commodity(PREFAB_HOUSING_NAME)
        brain = IndustrialistBrain()
        actor = get_actor("Buyer")

        market = Mock()
        market.actor_orders = {}
        market.get_bid_ask_spread.side_effect = lambda c: (
            (None, 15) if c is bricks else (None, 75)
        )

        chosen, ask = brain._cheapest_material_ask(actor, market, [bricks, prefab])
        assert chosen is bricks
        assert ask == 15


class _DurableStubDrive:
    """Drive whose one material covers several consumption events."""

    WELLBEING = True

    def __init__(self, name, material, target, servings):
        self._material = material
        self._target = target
        self._servings = servings
        self.metrics = Mock()
        self.metrics.get_name.return_value = name
        self.metrics.buffer = 0.0
        self.metrics.debt = 0.0

    def materials(self):
        return [self._material]

    def material_servings(self, commodity_id):
        return self._servings

    def target_units(self):
        return self._target

    def deprivation_stake(self):
        return 0.2

    def marginal_welfare(self):
        return 0.2

    def security(self, actor, unit_price):
        return 0.0

    def can_purchase(self, actor):
        return True


class TestDurableBuyQuantity:
    def test_one_durable_covers_the_whole_target(self):
        """Six events of need buys one unit that serves ten, not six units."""
        durable = Mock(spec=CommodityDefinition)
        durable.id = "food"
        brain = IndustrialistBrain()

        actor = Mock(spec=Actor)
        actor.name = "Buyer"
        actor.actor_type = ActorType.REGULAR
        actor.money = 1000
        actor.planet = Mock()
        actor.sim = Mock()
        actor.sim.process_registry.all_processes.return_value = []
        actor.sim.process_registry.get_processes_producing.return_value = []
        actor.sim.commodity_registry.all_commodities.return_value = []
        actor.sim.commodity_registry.get_commodity.return_value = durable
        actor.inventory = Mock(spec=Inventory)
        actor.inventory.get_quantity.return_value = 0
        actor.inventory.get_available_quantity.return_value = 0
        actor.inventory.has_quantity.return_value = False
        actor.drives = [_DurableStubDrive("food", durable, target=6, servings=10.0)]

        sell_order = Mock()
        sell_order.price = 5
        sell_order.actor = "someone_else"
        sell_order.timestamp = 0
        sell_order.cancelled = False

        market = actor.planet.market
        market.sell_orders = {durable: [sell_order]}
        market.actor_orders = {}
        market.get_actor_orders.return_value = {"buy": [], "sell": []}
        market.get_bid_ask_spread.return_value = (None, 5)
        market.get_avg_price.return_value = 10
        market.scarcity_pressure_for.return_value = 0.0

        brain.chosen_recipe_id = None
        buys = [
            c
            for c in brain.decide_market_actions(actor)
            if isinstance(c, PlaceBuyOrderCommand)
        ]

        assert len(buys) == 1
        assert buys[0].quantity == 1  # ceil(6 events / 10 per unit)


class TestProsperityCategories:
    def test_durable_categories_are_gone(self):
        assert "computing" not in CATEGORY_NAMES
        assert "shelter" not in CATEGORY_NAMES
        assert set(CATEGORY_NAMES) == {"food", "clothing", "health", "luxury"}


class TestSummaryDurablesBlock:
    def test_block_has_the_expected_keys(self):
        sim = Simulation()
        sim.setup_simple(
            num_planets=1,
            num_regular_actors=4,
            num_market_makers=1,
            num_ships=0,
        )
        sim.run_turn()

        durables = compute_summary(sim)["durables"]

        assert set(durables) == {
            "capital_recipe_actors",
            "computer_holder_share",
            "prefab_holder_share",
            "volume_per_planet_turn",
        }
        assert set(durables["volume_per_planet_turn"]) == {
            "computers",
            PREFAB_HOUSING_NAME,
        }
        assert 0.0 <= durables["prefab_holder_share"] <= 1.0
        assert 0.0 <= durables["computer_holder_share"] <= 1.0


class _TwoMaterialDrive(_DurableStubDrive):
    """Consumable plus durable, like the shelter drive."""

    def __init__(self, consumable, durable):
        super().__init__("shelter", consumable, target=3, servings=1.0)
        self._durable = durable

    def materials(self):
        return [self._material, self._durable]

    def material_servings(self, commodity_id):
        return 10.0 if commodity_id == self._durable.id else 1.0


class TestDurableReplacementCap:
    """A durable's amortized cost caps the durable, not the consumable.

    Making a prefab is not a way to serve the next shelter event, so its
    per-event make cost must not cap the bid for a building material below
    what the material itself costs to make.
    """

    def _setup(self):
        consumable = Mock(spec=CommodityDefinition)
        consumable.id = "simple_building_materials"
        durable = Mock(spec=CommodityDefinition)
        durable.id = "prefab_housing"
        drive = _TwoMaterialDrive(consumable, durable)
        brain = IndustrialistBrain()
        costs = {consumable.id: 19.0, durable.id: 100.0}
        brain._replacement_cost = lambda actor, market, c, cache=None, **kw: costs[c.id]
        return brain, drive, consumable, durable

    def test_consumable_is_capped_by_its_own_make_cost(self):
        brain, drive, consumable, _durable = self._setup()
        # Welfare term far above every cap: lam tiny.
        wtp = brain._drive_wtp_per_serving(Mock(), Mock(), drive, consumable, 1e-6)
        assert wtp == 19.0

    def test_durable_is_capped_by_the_cheaper_of_both_per_event(self):
        brain, drive, _consumable, durable = self._setup()
        wtp = brain._drive_wtp_per_serving(Mock(), Mock(), drive, durable, 1e-6)
        assert wtp == 10.0
