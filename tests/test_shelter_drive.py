"""Unit tests for the ShelterDrive module."""

import random
from unittest.mock import Mock, patch

import pytest

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import DriveMetrics
from spacesim2.core.drives.shelter_drive import (
    BASE_EVENT_PROB,
    BUFFER_MAX_DAYS,
    BUFFER_TARGET_DAYS,
    BUILDING_MATERIALS_NAME,
    DEBT_DECAY_FACTOR,
    DEBT_MISS_PENALTY,
    PREFAB_HOUSING_NAME,
    PREFAB_SERVINGS,
    PREFAB_WEAR_PROBABILITY,
    QUALITY_DEBT_DECAY_FACTOR,
    URGENCY,
    ShelterDrive,
)
from tests.helpers import get_actor


class TestShelterDriveConstants:
    """ShelterDrive constants."""

    def test_constants_are_reasonable(self):
        assert 0 < BASE_EVENT_PROB < 1
        assert 0 < DEBT_DECAY_FACTOR < 1
        assert 0 < QUALITY_DEBT_DECAY_FACTOR < DEBT_DECAY_FACTOR
        assert 0 < DEBT_MISS_PENALTY <= 1
        assert BUFFER_TARGET_DAYS > 0
        assert BUFFER_MAX_DAYS > BUFFER_TARGET_DAYS
        assert URGENCY > 0
        assert BUILDING_MATERIALS_NAME == "simple_building_materials"
        assert PREFAB_HOUSING_NAME == "prefab_housing"
        assert 0 < PREFAB_WEAR_PROBABILITY < 1
        assert PREFAB_SERVINGS == 1.0 / PREFAB_WEAR_PROBABILITY


class TestShelterDrive:
    """ShelterDrive with a mocked registry."""

    @pytest.fixture
    def mock_commodity_registry(self):
        registry = Mock(spec=CommodityRegistry)

        basic_commodity = Mock()
        basic_commodity.id = BUILDING_MATERIALS_NAME
        basic_commodity.name = "Simple Building Materials"

        quality_commodity = Mock()
        quality_commodity.id = PREFAB_HOUSING_NAME
        quality_commodity.name = "Prefab Housing"

        def get_commodity(name):
            if name == BUILDING_MATERIALS_NAME:
                return basic_commodity
            elif name == PREFAB_HOUSING_NAME:
                return quality_commodity
            return None

        registry.get_commodity.side_effect = get_commodity
        return registry

    @pytest.fixture
    def shelter_drive(self, mock_commodity_registry):
        return ShelterDrive(mock_commodity_registry)

    @pytest.fixture
    def mock_actor(self):
        actor = get_actor("TestActor")
        actor.inventory.remove_commodity = Mock(return_value=True)
        actor.inventory.get_available_quantity = Mock(return_value=0)
        return actor

    def test_initialization(self, shelter_drive, mock_commodity_registry):
        assert isinstance(shelter_drive.metrics, DriveMetrics)
        assert shelter_drive.metrics.health == 1.0
        assert shelter_drive.metrics.debt == 0.0
        assert shelter_drive.metrics.buffer == 0.0
        assert shelter_drive.metrics.urgency == URGENCY
        assert mock_commodity_registry.get_commodity.call_count == 2

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_no_event(self, mock_random, shelter_drive, mock_actor):
        """With no event, debt decays and nothing is consumed."""
        mock_random.return_value = 0.9  # above BASE_EVENT_PROB

        def get_qty(commodity):
            return 5 if commodity.id == BUILDING_MATERIALS_NAME else 0

        mock_actor.inventory.get_available_quantity.side_effect = get_qty
        shelter_drive.metrics.debt = 0.5

        result = shelter_drive.tick(mock_actor)

        assert result.health == 1.0
        assert abs(result.debt - 0.5 * DEBT_DECAY_FACTOR) < 1e-6
        mock_actor.inventory.remove_commodity.assert_not_called()

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_event_consumes_basic_material(
        self, mock_random, shelter_drive, mock_actor
    ):
        """An event with only basic materials uses the normal debt decay."""
        mock_random.return_value = 0.001  # below BASE_EVENT_PROB

        def get_qty(commodity):
            return 5 if commodity.id == BUILDING_MATERIALS_NAME else 0

        mock_actor.inventory.get_available_quantity.side_effect = get_qty
        mock_actor.inventory.remove_commodity.side_effect = (
            lambda c, q: c.id == BUILDING_MATERIALS_NAME
        )
        initial_debt = 0.3
        shelter_drive.metrics.debt = initial_debt

        result = shelter_drive.tick(mock_actor)

        assert result.health == 1.0
        # Basic material was used, so the standard decay applies.
        assert abs(result.debt - initial_debt * DEBT_DECAY_FACTOR) < 1e-6

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_event_prefers_the_dwelling(
        self, mock_random, shelter_drive, mock_actor
    ):
        """An event with both tiers is served by the prefab, not the brick."""
        # Event roll, then wear roll: the wear roll misses, so nothing is
        # consumed at all.
        mock_random.side_effect = [0.001, 0.9]
        mock_actor.inventory.get_available_quantity.return_value = 5
        mock_actor.inventory.remove_commodity.side_effect = lambda c, q: True
        initial_debt = 0.4
        shelter_drive.metrics.debt = initial_debt

        result = shelter_drive.tick(mock_actor)

        assert result.health == 1.0
        assert abs(result.debt - initial_debt * QUALITY_DEBT_DECAY_FACTOR) < 1e-6
        mock_actor.inventory.remove_commodity.assert_not_called()

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_event_wears_out_the_dwelling(
        self, mock_random, shelter_drive, mock_actor
    ):
        """A wear roll below PREFAB_WEAR_PROBABILITY consumes the prefab."""
        mock_random.side_effect = [0.001, PREFAB_WEAR_PROBABILITY / 2]
        mock_actor.inventory.get_available_quantity.return_value = 5
        mock_actor.inventory.remove_commodity.side_effect = lambda c, q: True

        shelter_drive.tick(mock_actor)

        first_call = mock_actor.inventory.remove_commodity.call_args_list[0]
        assert first_call[0][0].id == PREFAB_HOUSING_NAME

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_event_falls_back_to_basic_material(
        self, mock_random, shelter_drive, mock_actor
    ):
        """With no prefab, the event consumes a building material."""
        mock_random.return_value = 0.001
        mock_actor.inventory.get_available_quantity.side_effect = (
            lambda c: 5 if c.id == BUILDING_MATERIALS_NAME else 0
        )
        mock_actor.inventory.remove_commodity.side_effect = (
            lambda c, q: c.id == BUILDING_MATERIALS_NAME
        )
        initial_debt = 0.4
        shelter_drive.metrics.debt = initial_debt

        result = shelter_drive.tick(mock_actor)

        assert result.health == 1.0
        assert abs(result.debt - initial_debt * DEBT_DECAY_FACTOR) < 1e-6

    @patch("spacesim2.core.drives.shelter_drive.random.random")
    def test_tick_event_failed_maintenance(
        self, mock_random, shelter_drive, mock_actor
    ):
        """An event with no inventory penalizes debt."""
        mock_random.return_value = 0.001
        mock_actor.inventory.get_available_quantity.return_value = 0
        initial_debt = 0.2
        shelter_drive.metrics.debt = initial_debt

        result = shelter_drive.tick(mock_actor)

        assert result.health == 0.0
        expected_debt = min(1.0, initial_debt * DEBT_DECAY_FACTOR + DEBT_MISS_PENALTY)
        assert abs(result.debt - expected_debt) < 1e-6

    def test_buffer_calculation(self, shelter_drive, mock_actor):
        """Buffer counts a prefab as the events it serves, not as one unit."""

        def get_qty(commodity):
            return 50  # both tiers

        mock_actor.inventory.get_available_quantity.side_effect = get_qty

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.9
        ):
            result = shelter_drive.tick(mock_actor)

        from spacesim2.core.drives.actor_drive import log_norm_ratio

        total_units = 50 + 50 * PREFAB_SERVINGS
        expected_coverage_days = total_units / BASE_EVENT_PROB
        expected_buffer = log_norm_ratio(
            expected_coverage_days, BUFFER_TARGET_DAYS, BUFFER_MAX_DAYS
        )
        assert abs(result.buffer - expected_buffer) < 1e-6

    def test_buffer_with_zero_inventory(self, shelter_drive, mock_actor):
        mock_actor.inventory.get_available_quantity.return_value = 0

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.9
        ):
            result = shelter_drive.tick(mock_actor)

        assert result.buffer == 0.0

    def test_debt_no_decay_without_inventory(self, shelter_drive, mock_actor):
        """Debt does not decay with no materials and no event."""
        mock_actor.inventory.get_available_quantity.return_value = 0
        initial_debt = 0.6
        shelter_drive.metrics.debt = initial_debt

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.9
        ):
            result = shelter_drive.tick(mock_actor)

        assert result.debt == initial_debt

    def test_score_from_metrics(self, shelter_drive):
        shelter_drive.metrics.debt = 0.3
        assert shelter_drive.metrics.get_score() == pytest.approx(0.7)


class TestShelterDriveIntegration:
    """ShelterDrive with a real registry and inventory."""

    @pytest.fixture
    def real_registry(self):
        registry = CommodityRegistry()
        registry.add_commodity(
            CommodityDefinition(
                id=BUILDING_MATERIALS_NAME,
                name="Simple Building Materials",
                transportable=True,
                description="Basic shelter materials",
            )
        )
        registry.add_commodity(
            CommodityDefinition(
                id=PREFAB_HOUSING_NAME,
                name="Prefab Housing",
                transportable=True,
                description="Quality prefab shelter modules",
            )
        )
        return registry

    def test_with_real_commodity_registry(self, real_registry):
        drive = ShelterDrive(real_registry)
        assert drive.building_materials.id == BUILDING_MATERIALS_NAME
        assert drive.quality_materials.id == PREFAB_HOUSING_NAME

    def test_multiple_ticks_debt_accumulation(self, real_registry):
        """Debt grows over time when maintenance always fails."""
        drive = ShelterDrive(real_registry)
        actor = get_actor("TestActor")
        actor.inventory.get_available_quantity = Mock(return_value=0)
        actor.inventory.remove_commodity = Mock(return_value=False)

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.001
        ):
            debt_progression = [drive.tick(actor).debt for _ in range(5)]

        assert debt_progression[-1] > 0
        assert all(0 <= d <= 1 for d in debt_progression)

    def test_mixed_inventory_consumption(self, real_registry):
        """An event served by the prefab consumes it only on a wear hit."""
        drive = ShelterDrive(real_registry)
        actor = get_actor("TestActor")
        basic = real_registry.get_commodity(BUILDING_MATERIALS_NAME)
        quality = real_registry.get_commodity(PREFAB_HOUSING_NAME)
        actor.inventory.add_commodity(basic, 10)
        actor.inventory.add_commodity(quality, 10)

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.001
        ):
            result = drive.tick(actor)

        assert result.health == 1.0
        # The wear roll hits at 0.001, so one prefab and no brick is spent.
        assert actor.inventory.get_available_quantity(basic) == 10
        assert actor.inventory.get_available_quantity(quality) == 9

    def test_dwelling_preferred_over_basic(self, real_registry):
        """The prefab serves the event while a brick is also on hand."""
        drive = ShelterDrive(real_registry)
        actor = get_actor("TestActor")
        basic = real_registry.get_commodity(BUILDING_MATERIALS_NAME)
        quality = real_registry.get_commodity(PREFAB_HOUSING_NAME)
        actor.inventory.add_commodity(basic, 5)
        actor.inventory.add_commodity(quality, 5)

        with patch(
            "spacesim2.core.drives.shelter_drive.random.random", return_value=0.001
        ):
            drive.tick(actor)

        assert actor.inventory.get_available_quantity(quality) == 4
        assert actor.inventory.get_available_quantity(basic) == 5


class TestShelterDriveStochastic:
    """ShelterDrive stochastic behavior."""

    @pytest.fixture
    def setup_drive_and_actor(self):
        registry = CommodityRegistry()
        registry.add_commodity(
            CommodityDefinition(
                id=BUILDING_MATERIALS_NAME,
                name="Simple Building Materials",
                transportable=True,
                description="Basic shelter materials",
            )
        )
        registry.add_commodity(
            CommodityDefinition(
                id=PREFAB_HOUSING_NAME,
                name="Prefab Housing",
                transportable=True,
                description="Quality shelter modules",
            )
        )
        drive = ShelterDrive(registry)
        actor = get_actor("TestActor")
        return drive, actor

    def test_event_probability_distribution(self, setup_drive_and_actor):
        """Event frequency matches BASE_EVENT_PROB within 3 standard deviations."""
        drive, actor = setup_drive_and_actor
        # Bricks only: a prefab would serve events without consuming a unit,
        # so the consumption count would no longer measure the event rate.
        actor.inventory.get_available_quantity = Mock(
            side_effect=lambda c: 100 if c.id == BUILDING_MATERIALS_NAME else 0
        )
        actor.inventory.remove_commodity = Mock(return_value=True)

        num_trials = 1000
        events_occurred = 0
        random.seed(42)

        for _ in range(num_trials):
            drive.tick(actor)
            if actor.inventory.remove_commodity.called:
                events_occurred += 1
                actor.inventory.remove_commodity.reset_mock()

        p = BASE_EVENT_PROB
        std_dev = (num_trials * p * (1 - p)) ** 0.5
        assert abs(events_occurred - num_trials * p) < 3 * std_dev

    def test_metrics_bounds_over_time(self, setup_drive_and_actor):
        """All metrics stay within [0, 1] over many ticks."""
        drive, actor = setup_drive_and_actor

        random.seed(123)
        for _ in range(100):
            inventory_level = random.randint(0, 50)
            actor.inventory.get_available_quantity = Mock(return_value=inventory_level)
            actor.inventory.remove_commodity = Mock(return_value=inventory_level > 0)

            result = drive.tick(actor)

            assert 0 <= result.health <= 1
            assert 0 <= result.debt <= 1
            assert 0 <= result.buffer <= 1
            assert result.urgency == URGENCY
