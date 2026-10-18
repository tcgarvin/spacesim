"""Unit tests for the ActorDrive module."""

import math
from unittest.mock import Mock

import pytest

from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    get_zero_metrics,
    log_norm_ratio,
)
from tests.helpers import get_actor


class TestClamp01:
    """clamp01."""

    def test_clamp01_within_range(self):
        """Values within [0, 1] are unchanged."""
        assert clamp01(0.0) == 0.0
        assert clamp01(0.5) == 0.5
        assert clamp01(1.0) == 1.0

    def test_clamp01_below_zero(self):
        """Negative values clamp to 0."""
        assert clamp01(-0.1) == 0.0
        assert clamp01(-10.0) == 0.0
        assert clamp01(-math.inf) == 0.0

    def test_clamp01_above_one(self):
        """Values above 1 clamp to 1."""
        assert clamp01(1.1) == 1.0
        assert clamp01(10.0) == 1.0
        assert clamp01(math.inf) == 1.0


class TestLogNormRatio:
    """log_norm_ratio."""

    def test_log_norm_ratio_basic_cases(self):
        """x = 0 gives 0, x = target lies in [0, 1], x = cap gives 1."""
        assert log_norm_ratio(0.0, 10.0, 20.0) == 0.0

        result = log_norm_ratio(10.0, 10.0, 20.0)
        assert 0.0 <= result <= 1.0

        result = log_norm_ratio(20.0, 10.0, 20.0)
        assert result == 1.0

    def test_log_norm_ratio_negative_input(self):
        """Negative x is treated as 0."""
        result = log_norm_ratio(-5.0, 10.0, 20.0)
        assert result == 0.0

    def test_log_norm_ratio_above_cap(self):
        """x above cap is treated as x = cap."""
        result1 = log_norm_ratio(20.0, 10.0, 20.0)
        result2 = log_norm_ratio(30.0, 10.0, 20.0)
        assert result1 == result2 == 1.0

    def test_log_norm_ratio_diminishing_returns(self):
        """The function is monotonically increasing below the target."""
        target, cap = 10.0, 20.0

        result_quarter = log_norm_ratio(2.5, target, cap)
        result_half = log_norm_ratio(5.0, target, cap)
        result_target = log_norm_ratio(10.0, target, cap)

        assert result_quarter < result_half < result_target

        increase1 = result_half - result_quarter
        increase2 = result_target - result_half
        assert increase1 > 0
        assert increase2 > 0

    def test_log_norm_ratio_edge_cases(self):
        """cap == target stays in range; x above cap equals x == cap."""
        result = log_norm_ratio(5.0, 10.0, 10.0)
        assert 0.0 <= result <= 1.0

        result1 = log_norm_ratio(1.0, 0.01, 1.0)
        result2 = log_norm_ratio(2.0, 0.01, 1.0)
        assert result1 == result2


class TestDriveMetrics:
    """DriveMetrics."""

    def test_drive_metrics_creation(self):
        """Constructor stores all four fields."""
        metrics = DriveMetrics(health=0.8, debt=0.2, buffer=0.6, urgency=0.9)

        assert metrics.health == 0.8
        assert metrics.debt == 0.2
        assert metrics.buffer == 0.6
        assert metrics.urgency == 0.9

    def test_get_zero_metrics(self):
        """get_zero_metrics returns a DriveMetrics with all fields at 0."""
        metrics = get_zero_metrics()

        assert metrics.health == 0.0
        assert metrics.debt == 0.0
        assert metrics.buffer == 0.0
        assert metrics.urgency == 0.0
        assert isinstance(metrics, DriveMetrics)


class TestActorDrive:
    """ActorDrive base class."""

    @pytest.fixture
    def commodity_registry(self):
        """Mock commodity registry."""
        return Mock(spec=CommodityRegistry)

    @pytest.fixture
    def actor_drive(self, commodity_registry):
        """ActorDrive instance."""
        return ActorDrive(commodity_registry)

    @pytest.fixture
    def mock_actor(self):
        """Test actor."""
        return get_actor("TestActor")

    def test_actor_drive_initialization(self, actor_drive):
        """A new drive starts with zero metrics."""
        assert isinstance(actor_drive.metrics, DriveMetrics)
        assert actor_drive.metrics.health == 0.0
        assert actor_drive.metrics.debt == 0.0
        assert actor_drive.metrics.buffer == 0.0
        assert actor_drive.metrics.urgency == 0.0

    def test_update_metrics(self, actor_drive):
        """_update_metrics sets all four fields."""
        actor_drive._update_metrics(health=0.8, debt=0.3, buffer=0.7, urgency=0.9)

        assert actor_drive.metrics.health == 0.8
        assert actor_drive.metrics.debt == 0.3
        assert actor_drive.metrics.buffer == 0.7
        assert actor_drive.metrics.urgency == 0.9

    def test_update_metrics_partial(self, actor_drive):
        """A second _update_metrics call overwrites the first."""
        actor_drive._update_metrics(0.1, 0.2, 0.3, 0.4)

        actor_drive._update_metrics(0.9, 0.8, 0.7, 0.6)

        assert actor_drive.metrics.health == 0.9
        assert actor_drive.metrics.debt == 0.8
        assert actor_drive.metrics.buffer == 0.7
        assert actor_drive.metrics.urgency == 0.6

    def test_tick_not_implemented(self, actor_drive, mock_actor):
        """The base class tick raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            actor_drive.tick(mock_actor)

    def test_metrics_bounds(self, actor_drive):
        """Metrics accept the boundary values 0.0 and 1.0."""
        actor_drive._update_metrics(0.0, 0.0, 0.0, 0.0)
        assert all(
            metric == 0.0
            for metric in [
                actor_drive.metrics.health,
                actor_drive.metrics.debt,
                actor_drive.metrics.buffer,
                actor_drive.metrics.urgency,
            ]
        )

        actor_drive._update_metrics(1.0, 1.0, 1.0, 1.0)
        assert all(
            metric == 1.0
            for metric in [
                actor_drive.metrics.health,
                actor_drive.metrics.debt,
                actor_drive.metrics.buffer,
                actor_drive.metrics.urgency,
            ]
        )

    def test_metrics_immutability(self, actor_drive):
        """_update_metrics mutates the existing metrics object in place."""
        original_metrics = actor_drive.metrics

        actor_drive._update_metrics(0.5, 0.6, 0.7, 0.8)

        assert original_metrics.health == 0.5
        assert original_metrics.debt == 0.6
        assert original_metrics.buffer == 0.7
        assert original_metrics.urgency == 0.8


class TestActorDriveIntegration:
    """ActorDrive with real components."""

    def test_with_real_commodity_registry(self):
        """ActorDrive constructs with a real CommodityRegistry."""
        registry = CommodityRegistry()
        drive = ActorDrive(registry)

        assert isinstance(drive.metrics, DriveMetrics)
        assert drive.metrics.health == 0.0

    def test_metrics_persistence_across_updates(self):
        """A held metrics reference sees every later update."""
        registry = CommodityRegistry()
        drive = ActorDrive(registry)

        metrics_ref = drive.metrics

        for i in range(5):
            health = i * 0.2
            drive._update_metrics(health, 0.0, 0.0, 0.0)
            assert metrics_ref.health == health
            assert drive.metrics.health == health

    def test_inheritance_compatibility(self):
        """A subclass overriding tick runs without NotImplementedError."""

        class TestDrive(ActorDrive):
            def tick(self, actor):
                self._update_metrics(0.5, 0.5, 0.5, 0.5)
                return self.metrics

        registry = CommodityRegistry()
        drive = TestDrive(registry)
        mock_actor = get_actor("TestActor")

        result = drive.tick(mock_actor)

        assert isinstance(result, DriveMetrics)
        assert result.health == 0.5
        assert result.debt == 0.5
        assert result.buffer == 0.5
        assert result.urgency == 0.5
