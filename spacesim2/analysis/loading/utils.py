"""Utilities for discovering and working with simulation runs."""

from datetime import datetime
from pathlib import Path
from typing import Optional


class NoRunsFoundError(Exception):
    """Raised when no valid simulation runs are found."""

    pass


def get_runs_directory(base_path: Optional[Path | str] = None) -> Path:
    """The runs directory; defaults to ``data/runs``."""
    if base_path is None:
        return Path("data/runs")
    return Path(base_path)


def parse_run_timestamp(run_dir: Path) -> Optional[datetime]:
    """Parse the timestamp from a ``run_YYYYMMDD_HHMMSS`` directory name.

    Returns None when the name does not match.
    """
    name = run_dir.name
    if not name.startswith("run_"):
        return None

    timestamp_str = name[4:]
    try:
        return datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def find_most_recent_run(base_path: Optional[Path | str] = None) -> Path:
    """Find the most recent run directory under ``base_path``.

    Only directories named ``run_YYYYMMDD_HHMMSS`` are considered; the newest
    timestamp wins. ``base_path`` defaults to ``data/runs``.

    Raises:
        NoRunsFoundError: If no valid runs are found.
    """
    runs_dir = get_runs_directory(base_path)

    if not runs_dir.exists():
        raise NoRunsFoundError(
            f"Runs directory not found: {runs_dir}\n"
            f"Run 'spacesim2 run' (with export enabled) to create simulation data."
        )

    runs_with_times = []
    for item in runs_dir.iterdir():
        if not item.is_dir():
            continue

        timestamp = parse_run_timestamp(item)
        if timestamp is not None:
            runs_with_times.append((item, timestamp))

    if not runs_with_times:
        raise NoRunsFoundError(
            f"No valid runs found in: {runs_dir}\n"
            f"Run 'spacesim2 run' (with export enabled) to create simulation data.\n"
            f"Expected directory pattern: run_YYYYMMDD_HHMMSS"
        )

    runs_with_times.sort(key=lambda x: x[1], reverse=True)
    return runs_with_times[0][0]


def get_run_path_with_fallback(
    env_var: str = "SPACESIM_RUN_PATH", base_path: Optional[Path | str] = None
) -> Path:
    """Run path from ``env_var`` if set, else the most recent run.

    Raises:
        NoRunsFoundError: If the env var is unset and no runs are found.
    """
    import os

    env_path = os.getenv(env_var)
    if env_path:
        return Path(env_path)

    return find_most_recent_run(base_path)
