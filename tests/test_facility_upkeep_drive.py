"""Unit tests for FacilityUpkeepDrive."""

from pathlib import Path

import pytest

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.drives.facility_upkeep_drive import (
    DEBT_MISS_PENALTY,
    DRIVE_NAME,
    FacilityUpkeepDrive,
)
from spacesim2.core.facility import FacilityDefinition, FailureType, UpkeepSpec
from tests.helpers import get_actor

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

EVENT_PROB = 1.0 / 90.0
STRUCTURAL = FailureType("structural", 0.60, "simple_building_materials", 3)
MECHANICAL = FailureType("mechanical", 0.25, "common_metal", 1)
TOOLING = FailureType("tooling", 0.10, "simple_tools", 1)


@pytest.fixture
def registry() -> CommodityRegistry:
    registry = CommodityRegistry()
    registry.load_from_file(DATA_DIR / "commodities.yaml")
    return registry


@pytest.fixture
def facility() -> FacilityDefinition:
    # Deliberately out of weight order to exercise materials() sorting.
    return FacilityDefinition(
        id="spaceport",
        upkeep=UpkeepSpec(
            event_probability=EVENT_PROB,
            failures=(TOOLING, STRUCTURAL, MECHANICAL),
        ),
    )


@pytest.fixture
def drive(registry, facility) -> FacilityUpkeepDrive:
    return FacilityUpkeepDrive(registry, facility)


def _force(monkeypatch, module, event: bool, failure: FailureType | None = None):
    """Make the next tick deterministic: fire (or skip) a chosen failure."""
    monkeypatch.setattr(module.random, "random", lambda: 0.0 if event else 1.0)
    if failure is not None:
        monkeypatch.setattr(
            module.random, "choices", lambda population, weights, k: [failure]
        )


class TestSurface:
    def test_materials_are_distinct_and_ordered_by_weight(self, drive, registry):
        assert [c.id for c in drive.materials()] == [
            "simple_building_materials",
            "common_metal",
            "simple_tools",
        ]
        assert drive.materials()[0] is registry["simple_building_materials"]

    def test_target_units_is_the_largest_repair(self, drive):
        assert drive.target_units() == 3

    def test_initial_state_is_a_pristine_facility(self, drive):
        assert drive.metrics.get_name() == DRIVE_NAME
        assert drive.metrics.health == 1.0
        assert drive.metrics.debt == 0.0
        assert drive.last_unmet is None

    def test_missing_material_commodity_raises(self, facility):
        empty = CommodityRegistry()
        with pytest.raises(ValueError, match="simple_building_materials"):
            FacilityUpkeepDrive(empty, facility)


class TestTick:
    def test_no_event_leaves_condition_untouched(
        self, drive, registry, monkeypatch, tmp_path
    ):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        actor.inventory.add_commodity(registry["simple_building_materials"], 3)
        _force(monkeypatch, module, event=False)

        drive.tick(actor)

        assert (
            actor.inventory.get_available_quantity(
                registry["simple_building_materials"]
            )
            == 3
        )
        assert drive.metrics.debt == 0.0
        assert drive.metrics.health == 1.0
        assert drive.last_unmet is None

    def test_covered_event_consumes_stock_and_keeps_health_high(
        self, drive, registry, monkeypatch
    ):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        actor.inventory.add_commodity(registry["simple_building_materials"], 5)
        _force(monkeypatch, module, event=True, failure=STRUCTURAL)

        drive.tick(actor)

        assert (
            actor.inventory.get_available_quantity(
                registry["simple_building_materials"]
            )
            == 2
        )
        assert drive.metrics.debt == 0.0
        assert drive.metrics.health == 1.0
        assert drive.last_unmet is None

    def test_uncovered_event_raises_debt_and_records_the_failure(
        self, drive, registry, monkeypatch
    ):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        actor.inventory.add_commodity(registry["simple_building_materials"], 2)
        _force(monkeypatch, module, event=True, failure=STRUCTURAL)

        drive.tick(actor)

        # Partial stock is not consumed; a 3-unit repair needs 3 units.
        assert (
            actor.inventory.get_available_quantity(
                registry["simple_building_materials"]
            )
            == 2
        )
        assert drive.metrics.debt == pytest.approx(DEBT_MISS_PENALTY)
        assert drive.metrics.health == pytest.approx(1.0 - DEBT_MISS_PENALTY)
        assert drive.last_unmet is STRUCTURAL

    def test_wrong_material_does_not_cover_a_failure(
        self, drive, registry, monkeypatch
    ):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        actor.inventory.add_commodity(registry["simple_building_materials"], 10)
        _force(monkeypatch, module, event=True, failure=MECHANICAL)

        drive.tick(actor)

        assert drive.last_unmet is MECHANICAL
        assert drive.metrics.debt == pytest.approx(DEBT_MISS_PENALTY)

    def test_repair_clears_last_unmet_and_decays_debt(
        self, drive, registry, monkeypatch
    ):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        _force(monkeypatch, module, event=True, failure=MECHANICAL)
        drive.tick(actor)
        neglected_debt = drive.metrics.debt
        assert drive.last_unmet is MECHANICAL

        actor.inventory.add_commodity(registry["common_metal"], 1)
        drive.tick(actor)

        assert drive.last_unmet is None
        assert drive.metrics.debt < neglected_debt
        assert drive.metrics.health > 1.0 - neglected_debt
        assert actor.inventory.get_available_quantity(registry["common_metal"]) == 0

    def test_repeated_misses_drive_condition_down(self, drive, monkeypatch):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        _force(monkeypatch, module, event=True, failure=TOOLING)
        for _ in range(10):
            drive.tick(actor)

        assert drive.metrics.debt > 0.9
        assert drive.metrics.health < 0.1
        assert drive.metrics.get_score() == pytest.approx(1 - drive.metrics.debt)

    def test_buffer_rises_with_stock(self, drive, registry, monkeypatch):
        import spacesim2.core.drives.facility_upkeep_drive as module

        actor = get_actor()
        _force(monkeypatch, module, event=False)
        drive.tick(actor)
        assert drive.metrics.buffer == 0.0

        actor.inventory.add_commodity(registry["simple_building_materials"], 3)
        drive.tick(actor)
        assert 0.0 < drive.metrics.buffer <= 1.0
