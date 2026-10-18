"""Per-facility data loaded from ``data/facilities.yaml``.

Facilities themselves are non-transportable commodities held in inventory.
This module carries the extra data a facility needs beyond its commodity
definition: today just the upkeep failure table consumed by
``FacilityUpkeepDrive``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from spacesim2.core.commodity import CommodityRegistry


class FacilityDataError(ValueError):
    """Raised when facility data is malformed or references unknown goods."""


@dataclass(frozen=True)
class FailureType:
    """One row of a facility's upkeep failure table.

    ``weight`` is a relative probability within the table (not required to
    sum to 1); ``material_id`` names the commodity consumed to repair it.
    """

    name: str
    weight: float
    material_id: str
    quantity: int


@dataclass(frozen=True)
class UpkeepSpec:
    """Stochastic upkeep model for one facility."""

    event_probability: float
    failures: Tuple[FailureType, ...]


@dataclass(frozen=True)
class FacilityDefinition:
    """Data for one facility, keyed by its commodity id."""

    id: str
    upkeep: UpkeepSpec


class FacilityRegistry:
    """Registry that loads and manages facility definitions.

    Validation is eager and happens at load: the registry is constructed with
    the ``CommodityRegistry`` so an unknown facility id or repair material is
    a startup failure, not a silent no-op deep inside a drive. That is the
    same wiring ``ProcessRegistry`` uses, and it means every consumer can
    assume the materials resolve.
    """

    def __init__(self, commodity_registry: CommodityRegistry):
        self._facilities: Dict[str, FacilityDefinition] = {}
        self._commodity_registry = commodity_registry
        self._all_cache: Optional[List[FacilityDefinition]] = None

    def load_from_file(self, filepath: str | Path) -> None:
        """Load facility definitions from a YAML file.

        Raises FacilityDataError if the file is malformed, a weight or
        quantity is out of range, or a material is not a known commodity.
        """
        with open(filepath, "r") as f:
            facilities_data = yaml.safe_load(f)

        if not isinstance(facilities_data, list):
            raise FacilityDataError(
                f"{filepath}: expected a list of facilities, got "
                f"{type(facilities_data).__name__}"
            )

        for facility_data in facilities_data:
            facility = self._parse_facility(facility_data, filepath)
            self._facilities[facility.id] = facility
        self._all_cache = None

    def _parse_facility(self, data: object, filepath: str | Path) -> FacilityDefinition:
        if not isinstance(data, dict):
            raise FacilityDataError(f"{filepath}: each facility must be a mapping")

        facility_id = data.get("id")
        if not isinstance(facility_id, str) or not facility_id:
            raise FacilityDataError(f"{filepath}: facility entry is missing an 'id'")
        if self._commodity_registry.get_commodity(facility_id) is None:
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' is not a registered commodity"
            )

        upkeep_data = data.get("upkeep")
        if not isinstance(upkeep_data, dict):
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' is missing an 'upkeep' block"
            )

        event_probability = float(upkeep_data["event_probability"])
        if not 0.0 < event_probability <= 1.0:
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' event_probability must be in "
                f"(0, 1], got {event_probability}"
            )

        failures_data = upkeep_data.get("failures")
        if not isinstance(failures_data, list) or not failures_data:
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' needs a non-empty "
                "'failures' list"
            )

        failures = tuple(
            self._parse_failure(failure_data, facility_id, filepath)
            for failure_data in failures_data
        )
        return FacilityDefinition(
            id=facility_id,
            upkeep=UpkeepSpec(event_probability=event_probability, failures=failures),
        )

    def _parse_failure(
        self, data: object, facility_id: str, filepath: str | Path
    ) -> FailureType:
        if not isinstance(data, dict):
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' has a non-mapping failure entry"
            )

        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise FacilityDataError(
                f"{filepath}: facility '{facility_id}' has a failure without a name"
            )

        weight = float(data["weight"])
        if weight <= 0.0:
            raise FacilityDataError(
                f"{filepath}: failure '{name}' on facility '{facility_id}' must have "
                f"a positive weight, got {weight}"
            )

        quantity = int(data["quantity"])
        if quantity < 1:
            raise FacilityDataError(
                f"{filepath}: failure '{name}' on facility '{facility_id}' must have "
                f"quantity >= 1, got {quantity}"
            )

        material_id = data.get("material")
        if not isinstance(material_id, str) or not material_id:
            raise FacilityDataError(
                f"{filepath}: failure '{name}' on facility '{facility_id}' is missing "
                "a material"
            )
        if self._commodity_registry.get_commodity(material_id) is None:
            raise FacilityDataError(
                f"{filepath}: failure '{name}' on facility '{facility_id}' references "
                f"unknown commodity '{material_id}'"
            )

        return FailureType(
            name=name, weight=weight, material_id=material_id, quantity=quantity
        )

    def add_facility(self, facility: FacilityDefinition) -> None:
        """Add a facility definition, for tests and hand-built worlds."""
        self._facilities[facility.id] = facility
        self._all_cache = None

    def get_facility(self, facility_id: str) -> Optional[FacilityDefinition]:
        """Get a facility definition by id, or None if it has no extra data."""
        return self._facilities.get(facility_id)

    def all_facilities(self) -> List[FacilityDefinition]:
        """All facility definitions, as a cached shared read-only list."""
        if self._all_cache is None:
            self._all_cache = list(self._facilities.values())
        return self._all_cache

    def __getitem__(self, facility_id: str) -> FacilityDefinition:
        facility = self.get_facility(facility_id)
        if facility is None:
            raise KeyError(f"Facility with ID '{facility_id}' not found.")
        return facility
