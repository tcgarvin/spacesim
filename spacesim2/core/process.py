from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry


@dataclass
class ResourceAttribute:
    """How a planet attribute affects a gathering process.

    Names the attribute to read and whether it scales success probability
    or output quantity.
    """

    commodity: str  # Planet attribute to read, by commodity id
    effect: str  # "success" or "output"

    def __post_init__(self) -> None:
        """Validate effect type."""
        valid_effects = ("success", "output")
        if self.effect not in valid_effects:
            raise ValueError(
                f"effect must be one of {valid_effects}, got {self.effect!r}"
            )


@dataclass
class ProcessDefinition:
    """Definition of a production process."""

    id: str
    name: str
    inputs: Dict[CommodityDefinition, int]
    outputs: Dict[CommodityDefinition, int]
    tools_required: List[CommodityDefinition]
    facilities_required: List[
        CommodityDefinition
    ]  # List of CommodityDefinition for facilities
    labor: int
    description: str
    relevant_skills: List[str] = field(default_factory=list)
    # Only gathering processes set this.
    resource_attribute: Optional[ResourceAttribute] = None
    # Facility upkeep: commodity to the probability that one unit of it is
    # consumed by a single run. Unlike tools, upkeep goods are not required
    # to start a run, so they are absent from ``requirements``.
    upkeep: Dict[CommodityDefinition, float] = field(default_factory=dict)
    # Productive capital: commodity to the fractional output bonus one held
    # unit adds. Like upkeep, capital is not required to start a run; unlike
    # upkeep it is not consumed per run, only broken occasionally. Bonuses
    # from several capital goods sum.
    capital: Dict[CommodityDefinition, float] = field(default_factory=dict)
    # Read-only flattenings of the fields above, built once for hot scan
    # loops. Definitions are immutable after registry load, so they never go
    # stale. inputs/outputs as (commodity, quantity) tuples save a fresh
    # dict.items() view per process per scan.
    inputs_items: Tuple[Tuple[CommodityDefinition, int], ...] = field(
        init=False, repr=False
    )
    outputs_items: Tuple[Tuple[CommodityDefinition, int], ...] = field(
        init=False, repr=False
    )
    # Everything Actor.can_execute checks, in check order: inputs at their
    # quantities, then tools and facilities at quantity 1.
    requirements: Tuple[Tuple[CommodityDefinition, int], ...] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        for commodity, probability in self.upkeep.items():
            if not 0.0 < probability <= 1.0:
                raise ValueError(
                    f"upkeep probability for {commodity} in process "
                    f"{self.id!r} must be in (0, 1], got {probability!r}"
                )
        for commodity, bonus in self.capital.items():
            if not 0.0 < bonus <= 1.0:
                raise ValueError(
                    f"capital bonus for {commodity} in process "
                    f"{self.id!r} must be in (0, 1], got {bonus!r}"
                )
        self.inputs_items = tuple(self.inputs.items())
        self.outputs_items = tuple(self.outputs.items())
        self.requirements = (
            self.inputs_items
            + tuple((tool, 1) for tool in self.tools_required)
            + tuple((facility, 1) for facility in self.facilities_required)
        )

    def __str__(self) -> str:
        return self.name


class ProcessRegistry:
    """Registry that loads and manages process definitions."""

    def __init__(self, commodity_registry: CommodityRegistry):
        self._processes: Dict[str, ProcessDefinition] = {}
        self._commodity_registry = commodity_registry
        # Lazily built caches, dropped whenever a load adds processes. The
        # registry is immutable after setup. Returned lists are shared, so
        # callers must treat them as read-only.
        self._all_cache: Optional[List[ProcessDefinition]] = None
        self._producers_index: Optional[Dict[str, List[ProcessDefinition]]] = None
        self._consumers_index: Optional[Dict[str, List[ProcessDefinition]]] = None

    def load_from_file(self, filepath: str | Path) -> None:
        """Load process definitions from a YAML file."""
        try:
            with open(filepath, "r") as f:
                processes_data = yaml.safe_load(f)

            for process_data in processes_data:
                # Resolve commodity ids to definitions; unknown ids are skipped.
                inputs = {}
                for commodity_id, quantity in process_data["inputs"].items():
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        inputs[commodity] = quantity
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process inputs"
                        )

                outputs = {}
                for commodity_id, quantity in process_data["outputs"].items():
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        outputs[commodity] = quantity
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process outputs"
                        )

                tools_required = []
                for commodity_id in process_data["tools_required"]:
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        tools_required.append(commodity)
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process tools required"
                        )

                facilities_required = []
                for commodity_id in process_data["facilities_required"]:
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        facilities_required.append(commodity)
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process facilities required"
                        )

                upkeep = {}
                for commodity_id, probability in process_data.get("upkeep", {}).items():
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        upkeep[commodity] = float(probability)
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process upkeep"
                        )

                capital = {}
                for commodity_id, bonus in process_data.get("capital", {}).items():
                    commodity = self._commodity_registry.get_commodity(commodity_id)
                    if commodity:
                        capital[commodity] = float(bonus)
                    else:
                        print(
                            f"Warning: Skipping unknown commodity ID '{commodity_id}' in process capital"
                        )

                relevant_skills = process_data.get("relevant_skills", [])

                resource_attribute = None
                if "resource_attribute" in process_data:
                    ra_data = process_data["resource_attribute"]
                    resource_attribute = ResourceAttribute(
                        commodity=ra_data["commodity"],
                        effect=ra_data["effect"],
                    )

                process_def = ProcessDefinition(
                    id=process_data["id"],
                    name=process_data["name"],
                    inputs=inputs,
                    outputs=outputs,
                    tools_required=tools_required,
                    facilities_required=facilities_required,
                    labor=process_data["labor"],
                    description=process_data["description"],
                    relevant_skills=relevant_skills,
                    resource_attribute=resource_attribute,
                    upkeep=upkeep,
                    capital=capital,
                )
                self._processes[process_def.id] = process_def
            self._all_cache = None
            self._producers_index = None
            self._consumers_index = None
        except Exception as e:
            print(f"Error loading processes from {filepath}: {e}")

    def get_process(self, process_id: str) -> Optional[ProcessDefinition]:
        """Get a process definition by ID."""
        return self._processes.get(process_id)

    def all_processes(self) -> List[ProcessDefinition]:
        """All process definitions, as a cached shared list.

        Called on hot decision paths every turn. Callers must not mutate it.
        """
        if self._all_cache is None:
            self._all_cache = list(self._processes.values())
        return self._all_cache

    def get_processes_producing(
        self, commodity: CommodityDefinition
    ) -> List[ProcessDefinition]:
        """Processes that output the given commodity.

        Backed by a commodity-id to producers index built once per registry
        load, so lookups are O(1). The returned list is shared with the
        index; callers must not mutate it.
        """
        if self._producers_index is None:
            index: Dict[str, List[ProcessDefinition]] = {}
            for process in self._processes.values():
                for output in process.outputs:
                    index.setdefault(output.id, []).append(process)
            self._producers_index = index
        return self._producers_index.get(commodity.id, [])

    def get_processes_consuming(
        self, commodity: CommodityDefinition
    ) -> List[ProcessDefinition]:
        """Processes that take the given commodity as an input.

        The mirror of ``get_processes_producing``, backed by its own index
        built once per registry load. Tools and facilities are not inputs and
        are not indexed here: they are amortized, not consumed per run.
        The returned list is shared with the index; callers must not mutate
        it.
        """
        if self._consumers_index is None:
            index: Dict[str, List[ProcessDefinition]] = {}
            for process in self._processes.values():
                for input_commodity in process.inputs:
                    index.setdefault(input_commodity.id, []).append(process)
            self._consumers_index = index
        return self._consumers_index.get(commodity.id, [])
