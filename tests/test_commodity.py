import os

import yaml

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry, Inventory
from spacesim2.core.process import ProcessRegistry


def test_commodity_registry_loading() -> None:
    """CommodityRegistry loads definitions from a YAML file."""
    test_yaml_path = "temp_test_commodities.yaml"
    test_commodities = [
        {
            "id": "test_commodity",
            "name": "Test Commodity",
            "transportable": True,
            "description": "A test commodity",
        },
        {
            "id": "test_facility",
            "name": "Test Facility",
            "transportable": False,
            "description": "A test facility",
        },
    ]

    with open(test_yaml_path, "w") as f:
        yaml.dump(test_commodities, f)

    registry = CommodityRegistry()
    registry.load_from_file(test_yaml_path)

    os.remove(test_yaml_path)

    test_commodity = registry.get_commodity("test_commodity")
    test_facility = registry.get_commodity("test_facility")

    assert test_commodity is not None
    assert test_facility is not None

    assert test_commodity.id == "test_commodity"
    assert test_commodity.name == "Test Commodity"
    assert test_commodity.transportable

    assert test_facility.id == "test_facility"
    assert test_facility.name == "Test Facility"
    assert not test_facility.transportable

    all_commodities = registry.all_commodities()
    assert len(all_commodities) == 2
    assert test_commodity in all_commodities
    assert test_facility in all_commodities


def test_inventory_operations() -> None:
    """Inventory add, remove, and has_quantity keyed by CommodityDefinition."""
    food = CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )

    tools = CommodityDefinition(
        id="simple_tools",
        name="Simple Tools",
        transportable=True,
        description="Basic hand tools used in simple production processes.",
    )

    inventory = Inventory()

    assert inventory.get_quantity(food) == 0
    assert not inventory.has_quantity(food, 1)

    inventory.add_commodity(food, 5)
    assert inventory.get_quantity(food) == 5
    assert inventory.has_quantity(food, 3)
    assert not inventory.has_quantity(food, 6)

    inventory.add_commodity(tools, 2)
    assert inventory.get_quantity(tools) == 2

    assert inventory.get_total_quantity() == 7

    assert inventory.remove_commodity(food, 2)
    assert inventory.get_quantity(food) == 3

    # Removing more than available fails and leaves the quantity unchanged.
    assert not inventory.remove_commodity(food, 4)
    assert inventory.get_quantity(food) == 3

    assert inventory.remove_commodity(food, 3)
    assert inventory.get_quantity(food) == 0
    assert not inventory.has_quantity(food, 1)


def test_inventory_reservation() -> None:
    """Reserving splits quantity into available and reserved parts."""
    food = CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )

    inventory = Inventory()

    inventory.add_commodity(food, 10)
    assert inventory.get_quantity(food) == 10
    assert inventory.get_available_quantity(food) == 10
    assert inventory.get_reserved_quantity(food) == 0

    assert inventory.reserve_commodity(food, 3)
    assert inventory.get_quantity(food) == 10
    assert inventory.get_available_quantity(food) == 7
    assert inventory.get_reserved_quantity(food) == 3

    # Reserving more than available fails and changes nothing.
    assert not inventory.reserve_commodity(food, 8)
    assert inventory.get_quantity(food) == 10
    assert inventory.get_available_quantity(food) == 7
    assert inventory.get_reserved_quantity(food) == 3

    inventory.unreserve_commodity(food, 2)
    assert inventory.get_quantity(food) == 10
    assert inventory.get_available_quantity(food) == 9
    assert inventory.get_reserved_quantity(food) == 1

    inventory.unreserve_commodity(food, 1)
    assert inventory.get_quantity(food) == 10
    assert inventory.get_available_quantity(food) == 10
    assert inventory.get_reserved_quantity(food) == 0


def test_process_registry_loading() -> None:
    """ProcessRegistry loads a process and resolves its commodity references."""
    commodity_registry = CommodityRegistry()

    test_commodities = [
        {
            "id": "test_input",
            "name": "Test Input",
            "transportable": True,
            "description": "A test input commodity",
        },
        {
            "id": "test_output",
            "name": "Test Output",
            "transportable": True,
            "description": "A test output commodity",
        },
        {
            "id": "test_tool",
            "name": "Test Tool",
            "transportable": True,
            "description": "A test tool commodity",
        },
        {
            "id": "test_facility",
            "name": "Test Facility",
            "transportable": False,
            "description": "A test facility commodity",
        },
    ]

    test_commodity_path = "temp_test_commodities.yaml"
    with open(test_commodity_path, "w") as f:
        yaml.dump(test_commodities, f)

    commodity_registry.load_from_file(test_commodity_path)
    os.remove(test_commodity_path)

    test_yaml_path = "temp_test_processes.yaml"
    test_processes = [
        {
            "id": "test_process",
            "name": "Test Process",
            "inputs": {"test_input": 2},
            "outputs": {"test_output": 1},
            "tools_required": ["test_tool"],
            "facilities_required": ["test_facility"],
            "labor": 1,
            "description": "A test process",
        }
    ]

    with open(test_yaml_path, "w") as f:
        yaml.dump(test_processes, f)

    process_registry = ProcessRegistry(commodity_registry)
    process_registry.load_from_file(test_yaml_path)

    os.remove(test_yaml_path)

    test_process = process_registry.get_process("test_process")

    assert test_process is not None
    assert test_process.id == "test_process"
    assert test_process.name == "Test Process"

    test_input = commodity_registry.get_commodity("test_input")
    test_output = commodity_registry.get_commodity("test_output")
    test_tool = commodity_registry.get_commodity("test_tool")
    test_facility = commodity_registry.get_commodity("test_facility")

    # Inputs and outputs are keyed by CommodityDefinition objects.
    assert test_input in test_process.inputs
    assert test_process.inputs[test_input] == 2

    assert test_output in test_process.outputs
    assert test_process.outputs[test_output] == 1

    assert test_tool in test_process.tools_required
    assert test_facility in test_process.facilities_required

    producing_processes = process_registry.get_processes_producing(test_output)

    assert len(producing_processes) == 1
    assert producing_processes[0] == test_process
