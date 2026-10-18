"""Analysis package for exporting and analyzing simulation data."""

from typing import Any

__all__ = ["SimulationData"]


def __getattr__(name: str) -> Any:
    # Lazy per PEP 562 so modules that do not need the optional analysis
    # extra, such as analysis.summary on the CLI's --summary path, work
    # without polars installed. See docs/performance.md.
    if name == "SimulationData":
        from spacesim2.analysis.loading.loader import SimulationData

        return SimulationData
    raise AttributeError(name)
