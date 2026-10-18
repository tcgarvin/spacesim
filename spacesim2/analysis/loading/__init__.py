"""Loading functionality for reading Parquet files into DataFrames."""

from pathlib import Path
from typing import Optional

from spacesim2.analysis.loading.loader import SimulationData
from spacesim2.analysis.loading.utils import (
    NoRunsFoundError,
    find_most_recent_run,
    get_run_path_with_fallback,
)


def load_run(run_path: Optional[Path | str] = None) -> SimulationData:
    """Load a simulation run for a Tier-1 analysis script.

    Resolves the run path in this order: explicit ``run_path``, the
    ``SPACESIM_RUN_PATH`` env var, then the most recent run. Returns a
    lazily-loading ``SimulationData`` exposing ``.actor_turns``,
    ``.actor_drives``, ``.market_transactions``, ``.market_snapshots``, and
    ``.ship_turns`` as Polars frames.
    """
    if run_path is None:
        run_path = get_run_path_with_fallback()
    return SimulationData(run_path)


__all__ = [
    "SimulationData",
    "NoRunsFoundError",
    "find_most_recent_run",
    "get_run_path_with_fallback",
    "load_run",
]
