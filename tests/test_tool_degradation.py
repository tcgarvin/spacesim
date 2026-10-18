"""Tests for tool degradation during process execution."""

from unittest.mock import MagicMock, patch

from spacesim2.core.commands import ProcessCommand
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.process import ProcessDefinition, ProcessRegistry
from spacesim2.core.simulation import Simulation

from .helpers import get_actor


def create_sim_with_tool_process():
    """Simulation with one process that requires simple_tools."""
    sim = Simulation()

    sim.commodity_registry = CommodityRegistry()
    sim.commodity_registry._commodities["input_commodity"] = CommodityDefinition(
        id="input_commodity",
        name="Input Commodity",
        transportable=True,
        description="Test input",
    )
    sim.commodity_registry._commodities["output_commodity"] = CommodityDefinition(
        id="output_commodity",
        name="Output Commodity",
        transportable=True,
        description="Test output",
    )
    sim.commodity_registry._commodities["simple_tools"] = CommodityDefinition(
        id="simple_tools",
        name="Simple Tools",
        transportable=True,
        description="Test tool",
    )

    sim.process_registry = ProcessRegistry(sim.commodity_registry)

    process_def = ProcessDefinition(
        id="test_process",
        name="Test Process",
        inputs={"input_commodity": 1},
        outputs={"output_commodity": 1},
        tools_required=["simple_tools"],
        facilities_required=[],
        labor=1,
        description="A test process requiring tools",
    )
    sim.process_registry._processes["test_process"] = process_def

    return sim


def test_tool_not_consumed_on_successful_process():
    """A successful process keeps its tools when the break roll misses."""
    sim = create_sim_with_tool_process()

    market = Market()
    planet = Planet("Test Planet", market)

    actor = get_actor("Test Actor", sim, planet=planet)
    actor.inventory.add_commodity("input_commodity", 5)
    actor.inventory.add_commodity("simple_tools", 3)

    with patch(
        "spacesim2.core.commands.random.random", return_value=0.5
    ):  # above the 0.01 break threshold
        command = ProcessCommand("test_process")
        result = command.execute(actor)

    assert result is True
    assert actor.inventory.get_quantity("simple_tools") == 3


def test_tool_breaks_when_random_below_threshold():
    """One tool breaks when the random roll is below the threshold."""
    sim = create_sim_with_tool_process()

    market = Market()
    planet = Planet("Test Planet", market)

    actor = get_actor("Test Actor", sim, planet=planet)
    actor.inventory.add_commodity("input_commodity", 5)
    actor.inventory.add_commodity("simple_tools", 3)

    with patch(
        "spacesim2.core.commands.random.random", return_value=0.005
    ):  # below the 0.01 break threshold
        command = ProcessCommand("test_process")
        result = command.execute(actor)

    assert result is True
    assert actor.inventory.get_quantity("simple_tools") == 2


def test_tool_break_logged_when_data_logger_present():
    """A tool break is logged through the data logger."""
    sim = create_sim_with_tool_process()

    mock_logger = MagicMock()
    sim.data_logger = mock_logger

    market = Market()
    planet = Planet("Test Planet", market)

    actor = get_actor("Test Actor", sim, planet=planet)
    actor.inventory.add_commodity("input_commodity", 5)
    actor.inventory.add_commodity("simple_tools", 3)

    with patch(
        "spacesim2.core.commands.random.random", return_value=0.005
    ):  # below the 0.01 break threshold
        command = ProcessCommand("test_process")
        result = command.execute(actor)

    assert result is True
    mock_logger.log_actor_note.assert_called_once()
    call_args = mock_logger.log_actor_note.call_args
    assert call_args[0][0] == actor
    assert "Tool broke" in call_args[0][1]
    assert "simple_tools" in call_args[0][1]  # string id in this setup


def test_tool_degradation_only_after_successful_process():
    """A process that fails for missing inputs does not roll for tool breaks."""
    sim = create_sim_with_tool_process()

    market = Market()
    planet = Planet("Test Planet", market)

    # Tools but no input_commodity, so the process fails.
    actor = get_actor("Test Actor", sim, planet=planet)
    actor.inventory.add_commodity("simple_tools", 3)

    with patch(
        "spacesim2.core.commands.random.random", return_value=0.005
    ):  # would break a tool if rolled
        command = ProcessCommand("test_process")
        result = command.execute(actor)

    assert result is False
    assert actor.inventory.get_quantity("simple_tools") == 3


def test_tool_degradation_probability_is_independent_per_tool():
    """Each required tool rolls for breakage independently."""
    sim = Simulation()

    sim.commodity_registry = CommodityRegistry()
    sim.commodity_registry._commodities["input_commodity"] = CommodityDefinition(
        id="input_commodity",
        name="Input Commodity",
        transportable=True,
        description="Test input",
    )
    sim.commodity_registry._commodities["output_commodity"] = CommodityDefinition(
        id="output_commodity",
        name="Output Commodity",
        transportable=True,
        description="Test output",
    )
    sim.commodity_registry._commodities["tool_a"] = CommodityDefinition(
        id="tool_a",
        name="Tool A",
        transportable=True,
        description="First tool",
    )
    sim.commodity_registry._commodities["tool_b"] = CommodityDefinition(
        id="tool_b",
        name="Tool B",
        transportable=True,
        description="Second tool",
    )

    sim.process_registry = ProcessRegistry(sim.commodity_registry)

    process_def = ProcessDefinition(
        id="multi_tool_process",
        name="Multi Tool Process",
        inputs={"input_commodity": 1},
        outputs={"output_commodity": 1},
        tools_required=["tool_a", "tool_b"],
        facilities_required=[],
        labor=1,
        description="Process requiring two tools",
    )
    sim.process_registry._processes["multi_tool_process"] = process_def

    market = Market()
    planet = Planet("Test Planet", market)

    actor = get_actor("Test Actor", sim, planet=planet)
    actor.inventory.add_commodity("input_commodity", 5)
    actor.inventory.add_commodity("tool_a", 3)
    actor.inventory.add_commodity("tool_b", 3)

    # Both rolls are below the threshold, so both tools break.
    random_values = iter([0.005, 0.005])
    with patch(
        "spacesim2.core.commands.random.random", side_effect=lambda: next(random_values)
    ):
        command = ProcessCommand("multi_tool_process")
        result = command.execute(actor)

    assert result is True
    assert actor.inventory.get_quantity("tool_a") == 2
    assert actor.inventory.get_quantity("tool_b") == 2


def test_process_without_tools_has_no_degradation():
    """A process with no tool requirements runs without any break roll."""
    sim = Simulation()

    sim.commodity_registry = CommodityRegistry()
    sim.commodity_registry._commodities["biomass"] = CommodityDefinition(
        id="biomass",
        name="Biomass",
        transportable=True,
        description="Raw biomass",
    )

    sim.process_registry = ProcessRegistry(sim.commodity_registry)

    process_def = ProcessDefinition(
        id="gather_biomass",
        name="Gather Biomass",
        inputs={},
        outputs={"biomass": 4},
        tools_required=[],
        facilities_required=[],
        labor=1,
        description="Gather biomass by hand",
    )
    sim.process_registry._processes["gather_biomass"] = process_def

    market = Market()
    planet = Planet("Test Planet", market)

    actor = get_actor("Test Actor", sim, planet=planet)

    command = ProcessCommand("gather_biomass")
    result = command.execute(actor)

    assert result is True
    assert actor.inventory.get_quantity("biomass") == 4


def test_statistical_tool_degradation_rate():
    """The tool break rate over many runs is about 1%."""
    sim = create_sim_with_tool_process()

    market = Market()
    planet = Planet("Test Planet", market)

    num_runs = 10000
    degradation_count = 0

    for _ in range(num_runs):
        actor = get_actor("Test Actor", sim, planet=planet)
        actor.inventory.add_commodity("input_commodity", 1)
        actor.inventory.add_commodity("simple_tools", 1)

        command = ProcessCommand("test_process")
        command.execute(actor)

        if actor.inventory.get_quantity("simple_tools") == 0:
            degradation_count += 1

    # Expect about 100 breaks; allow 4 standard deviations of variance.
    expected = num_runs * 0.01
    std_dev = (num_runs * 0.01 * 0.99) ** 0.5
    tolerance = 4 * std_dev

    assert abs(degradation_count - expected) < tolerance, (
        f"Degradation rate {degradation_count / num_runs:.4f} "
        f"differs significantly from expected 0.01"
    )
