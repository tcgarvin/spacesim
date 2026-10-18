"""Data loader for reading Parquet files into DataFrames."""

from pathlib import Path
from typing import Optional

import polars as pl


class SimulationData:
    """Lazily loads a run's Parquet tables as Polars frames."""

    def __init__(self, run_path: Path | str):
        """Point the loader at a run directory containing the Parquet files."""
        self.run_path = Path(run_path)
        self.simulation_id = self.run_path.name

        self._actor_turns: Optional[pl.DataFrame] = None
        self._actor_drives: Optional[pl.DataFrame] = None
        self._market_transactions: Optional[pl.DataFrame] = None
        self._market_snapshots: Optional[pl.DataFrame] = None
        self._ship_turns: Optional[pl.DataFrame] = None

    @property
    def actor_turns(self) -> pl.DataFrame:
        """Actor state per turn."""
        if self._actor_turns is None:
            self._actor_turns = pl.read_parquet(self.run_path / "actor_turns.parquet")
        return self._actor_turns

    @property
    def actor_drives(self) -> pl.DataFrame:
        """Actor drive metrics per turn."""
        if self._actor_drives is None:
            self._actor_drives = pl.read_parquet(self.run_path / "actor_drives.parquet")
        return self._actor_drives

    @property
    def market_transactions(self) -> pl.DataFrame:
        """Market transactions."""
        if self._market_transactions is None:
            self._market_transactions = pl.read_parquet(
                self.run_path / "market_transactions.parquet"
            )
        return self._market_transactions

    @property
    def market_snapshots(self) -> pl.DataFrame:
        """Aggregated market state per turn."""
        if self._market_snapshots is None:
            self._market_snapshots = pl.read_parquet(
                self.run_path / "market_snapshots.parquet"
            )
        return self._market_snapshots

    @property
    def ship_turns(self) -> pl.DataFrame:
        """Ship state per turn."""
        if self._ship_turns is None:
            self._ship_turns = pl.read_parquet(self.run_path / "ship_turns.parquet")
        return self._ship_turns

    def __repr__(self) -> str:
        return f"SimulationData(run_path='{self.run_path}', simulation_id='{self.simulation_id}')"
