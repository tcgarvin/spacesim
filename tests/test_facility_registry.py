"""Unit tests for the facility data file and its registry."""

from pathlib import Path

import pytest
import yaml

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.facility import FacilityDataError, FacilityRegistry

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def commodity_registry() -> CommodityRegistry:
    registry = CommodityRegistry()
    registry.load_from_file(DATA_DIR / "commodities.yaml")
    return registry


@pytest.fixture
def facility_registry(commodity_registry: CommodityRegistry) -> FacilityRegistry:
    registry = FacilityRegistry(commodity_registry)
    registry.load_from_file(DATA_DIR / "facilities.yaml")
    return registry


def _write_facilities(tmp_path: Path, data: list) -> Path:
    path = tmp_path / "facilities.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


class TestRealDataFile:
    def test_spaceport_is_a_registered_commodity(self, commodity_registry):
        spaceport = commodity_registry.get_commodity("spaceport")
        assert spaceport is not None
        assert spaceport.transportable is False

    def test_spaceport_upkeep_matches_the_design_table(self, facility_registry):
        spaceport = facility_registry["spaceport"]
        assert spaceport.id == "spaceport"
        assert spaceport.upkeep.event_probability == pytest.approx(1.0 / 90.0)

        rows = {f.name: f for f in spaceport.upkeep.failures}
        assert set(rows) == {"structural", "mechanical", "tooling", "tank_lining"}
        assert rows["structural"].material_id == "simple_building_materials"
        assert rows["structural"].quantity == 3
        assert rows["structural"].weight == pytest.approx(0.60)
        assert rows["mechanical"].material_id == "common_metal"
        assert rows["mechanical"].weight == pytest.approx(0.25)
        assert rows["tooling"].material_id == "simple_tools"
        assert rows["tooling"].weight == pytest.approx(0.10)
        assert rows["tank_lining"].material_id == "chemicals"
        assert rows["tank_lining"].weight == pytest.approx(0.05)
        assert all(f.quantity >= 1 for f in spaceport.upkeep.failures)
        assert sum(f.weight for f in spaceport.upkeep.failures) == pytest.approx(1.0)

    def test_get_facility_is_none_for_unknown_id(self, facility_registry):
        assert facility_registry.get_facility("smelting_facility") is None
        with pytest.raises(KeyError):
            facility_registry["smelting_facility"]


class TestValidation:
    def _base_entry(self) -> dict:
        return {
            "id": "spaceport",
            "upkeep": {
                "event_probability": 1.0 / 90.0,
                "failures": [
                    {
                        "name": "structural",
                        "weight": 0.6,
                        "material": "simple_building_materials",
                        "quantity": 3,
                    }
                ],
            },
        }

    def test_non_positive_weight_is_rejected(self, commodity_registry, tmp_path):
        entry = self._base_entry()
        entry["upkeep"]["failures"][0]["weight"] = 0.0
        path = _write_facilities(tmp_path, [entry])

        with pytest.raises(FacilityDataError, match="positive weight"):
            FacilityRegistry(commodity_registry).load_from_file(path)

    def test_unknown_material_is_rejected(self, commodity_registry, tmp_path):
        entry = self._base_entry()
        entry["upkeep"]["failures"][0]["material"] = "unobtanium"
        path = _write_facilities(tmp_path, [entry])

        with pytest.raises(FacilityDataError, match="unknown commodity 'unobtanium'"):
            FacilityRegistry(commodity_registry).load_from_file(path)

    def test_zero_quantity_is_rejected(self, commodity_registry, tmp_path):
        entry = self._base_entry()
        entry["upkeep"]["failures"][0]["quantity"] = 0
        path = _write_facilities(tmp_path, [entry])

        with pytest.raises(FacilityDataError, match="quantity >= 1"):
            FacilityRegistry(commodity_registry).load_from_file(path)

    def test_unknown_facility_commodity_is_rejected(self, commodity_registry, tmp_path):
        entry = self._base_entry()
        entry["id"] = "orbital_elevator"
        path = _write_facilities(tmp_path, [entry])

        with pytest.raises(FacilityDataError, match="not a registered commodity"):
            FacilityRegistry(commodity_registry).load_from_file(path)

    def test_missing_upkeep_block_is_rejected(self, commodity_registry, tmp_path):
        path = _write_facilities(tmp_path, [{"id": "spaceport"}])

        with pytest.raises(FacilityDataError, match="missing an 'upkeep' block"):
            FacilityRegistry(commodity_registry).load_from_file(path)

    def test_out_of_range_event_probability_is_rejected(
        self, commodity_registry, tmp_path
    ):
        entry = self._base_entry()
        entry["upkeep"]["event_probability"] = 1.5
        path = _write_facilities(tmp_path, [entry])

        with pytest.raises(FacilityDataError, match="event_probability"):
            FacilityRegistry(commodity_registry).load_from_file(path)
