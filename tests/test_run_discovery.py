"""Tests for run discovery utilities."""

from datetime import datetime

import pytest

from spacesim2.analysis.loading.utils import (
    NoRunsFoundError,
    find_most_recent_run,
    get_run_path_with_fallback,
    parse_run_timestamp,
)


class TestParseRunTimestamp:
    """Tests for parse_run_timestamp."""

    def test_valid_timestamp(self, tmp_path):
        """Parse valid run directory name."""
        run_dir = tmp_path / "run_20251130_182530"
        expected = datetime(2025, 11, 30, 18, 25, 30)
        assert parse_run_timestamp(run_dir) == expected

    def test_invalid_prefix(self, tmp_path):
        """Return None for directories without run_ prefix."""
        run_dir = tmp_path / "not_a_run_20251130_182530"
        assert parse_run_timestamp(run_dir) is None

    def test_invalid_timestamp_format(self, tmp_path):
        """Return None for invalid timestamp format."""
        run_dir = tmp_path / "run_invalid_timestamp"
        assert parse_run_timestamp(run_dir) is None

    def test_partial_timestamp(self, tmp_path):
        """Return None for incomplete timestamp."""
        run_dir = tmp_path / "run_20251130"
        assert parse_run_timestamp(run_dir) is None


class TestFindMostRecentRun:
    """Tests for find_most_recent_run."""

    def test_single_run(self, tmp_path):
        """Find single run."""
        run1 = tmp_path / "run_20251130_120000"
        run1.mkdir()

        result = find_most_recent_run(tmp_path)
        assert result == run1

    def test_multiple_runs_sorted(self, tmp_path):
        """Find most recent among multiple runs."""
        run1 = tmp_path / "run_20251130_120000"
        run2 = tmp_path / "run_20251130_150000"
        run3 = tmp_path / "run_20251130_100000"

        for run in [run1, run2, run3]:
            run.mkdir()

        result = find_most_recent_run(tmp_path)
        assert result == run2  # 15:00:00 is most recent

    def test_ignores_invalid_directories(self, tmp_path):
        """Ignore directories that don't match pattern."""
        valid = tmp_path / "run_20251130_120000"
        valid.mkdir()

        (tmp_path / "not_a_run").mkdir()
        (tmp_path / "run_invalid").mkdir()
        (tmp_path / "data").mkdir()

        result = find_most_recent_run(tmp_path)
        assert result == valid

    def test_ignores_files(self, tmp_path):
        """Ignore files, only consider directories."""
        run1 = tmp_path / "run_20251130_120000"
        run1.mkdir()

        (tmp_path / "run_20251130_130000").touch()

        result = find_most_recent_run(tmp_path)
        assert result == run1

    def test_no_runs_raises_exception(self, tmp_path):
        """Raise NoRunsFoundError when no valid runs exist."""
        with pytest.raises(NoRunsFoundError) as exc_info:
            find_most_recent_run(tmp_path)

        assert "No valid runs found" in str(exc_info.value)
        assert "spacesim2 run" in str(exc_info.value)

    def test_nonexistent_directory_raises_exception(self, tmp_path):
        """Raise NoRunsFoundError when runs directory doesn't exist."""
        nonexistent = tmp_path / "nonexistent"

        with pytest.raises(NoRunsFoundError) as exc_info:
            find_most_recent_run(nonexistent)

        assert "Runs directory not found" in str(exc_info.value)


class TestGetRunPathWithFallback:
    """Tests for get_run_path_with_fallback."""

    def test_uses_env_var_when_set(self, tmp_path, monkeypatch):
        """Use environment variable when set."""
        env_path = tmp_path / "custom_run"
        monkeypatch.setenv("SPACESIM_RUN_PATH", str(env_path))

        result = get_run_path_with_fallback()
        assert result == env_path

    def test_auto_detects_when_no_env_var(self, tmp_path, monkeypatch):
        """Auto-detect most recent run when env var not set."""
        monkeypatch.delenv("SPACESIM_RUN_PATH", raising=False)

        run1 = tmp_path / "run_20251130_120000"
        run1.mkdir()

        result = get_run_path_with_fallback(base_path=tmp_path)
        assert result == run1

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        """Environment variable takes precedence over auto-detection."""
        auto_run = tmp_path / "run_20251130_120000"
        auto_run.mkdir()

        env_run = tmp_path / "custom_run"
        monkeypatch.setenv("SPACESIM_RUN_PATH", str(env_run))

        result = get_run_path_with_fallback(base_path=tmp_path)
        assert result == env_run
