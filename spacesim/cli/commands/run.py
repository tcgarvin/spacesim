"""Run simulation command implementation."""

import argparse
import io
import json
import os
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from spacesim.cli.common import configure_actor_logging, create_and_setup_simulation
from spacesim.cli.output import print_success, print_warning
from spacesim.core.galaxy import DEFAULT_ARMS, DEFAULT_LANE_DENSITY
from spacesim.core.simulation import DEFAULT_LANDS_PER_PLANET

try:
    from spacesim.analysis.export.exporter import SimulationExporter

    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False

OUTPUT_DIR = Path("data/runs")

# Printed when --workers > 1 runs with the GIL on; `dev ab` scans run logs for it.
GIL_ENABLED_WARNING = "GIL is enabled"
NOTEBOOK_PATH = Path("notebooks/analysis_template.py")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'run' subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "run",
        help="Run headless simulation with data export",
        description="Run simulation in headless mode with Parquet export for analysis",
    )

    parser.add_argument(
        "--turns", type=int, default=1000, help="Number of turns to simulate"
    )
    parser.add_argument(
        "--planets", type=int, default=100, help="Number of planets (default: 100)"
    )
    parser.add_argument(
        "--arms",
        type=int,
        default=DEFAULT_ARMS,
        help=f"Spiral arms in the galaxy layout (default: {DEFAULT_ARMS})",
    )
    parser.add_argument(
        "--lane-density",
        type=float,
        default=DEFAULT_LANE_DENSITY,
        help="Fraction of optional local star lanes kept beyond the spanning "
        f"tree, 0..1 (default: {DEFAULT_LANE_DENSITY})",
    )
    parser.add_argument(
        "--actors", type=int, default=100, help="Number of regular actors per planet"
    )
    parser.add_argument(
        "--makers", type=int, default=2, help="Number of market makers per planet"
    )
    parser.add_argument(
        "--operators",
        type=int,
        default=2,
        help="Number of spaceport operators per planet",
    )
    parser.add_argument(
        "--ships", type=int, default=1, help="Number of ships per planet"
    )
    parser.add_argument(
        "--lands-per-planet",
        type=int,
        default=DEFAULT_LANDS_PER_PLANET,
        help=(
            "Size of each planet's land pool; must exceed --actors so "
            f"migrants have somewhere to settle (default: "
            f"{DEFAULT_LANDS_PER_PLANET})"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Actor-phase threads (1 = serial). Real speedup needs a "
            "free-threaded interpreter; see docs/performance.md"
        ),
    )

    parser.add_argument(
        "--log-actors",
        type=str,
        default="1",
        metavar="SPEC",
        help="Actors to log in per-actor detail: 'all', a sample size N "
        "(default: 1 random non-market-maker), or an actor name",
    )

    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Skip Parquet export (for quick test runs)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all output including progress bar",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a compact JSON behavioral KPI summary + verdict at the end "
        "(also written to summary.json when exporting). Token-efficient readout "
        "for agents evaluating whether the economy behaves as intended.",
    )
    parser.add_argument(
        "--summary-json",
        type=str,
        default="",
        metavar="PATH",
        help="Write the KPI summary JSON to PATH (implies --summary), so a "
        "caller reads a file instead of slicing it out of stdout.",
    )
    parser.add_argument(
        "--notebook", action="store_true", help="Open marimo notebook after simulation"
    )

    parser.set_defaults(func=execute)
    return parser


class _TailCapture(io.TextIOBase):
    """A write-only text stream that retains only the last ``max_chars``.

    Swallows simulation stdout during runs with bounded memory. Only rare
    warnings such as failed market settlements write to stdout during runs;
    the retained tail is echoed after the run so they are not lost.
    """

    def __init__(self, max_chars: int = 64_000) -> None:
        super().__init__()
        self._max_chars = max_chars
        self._chunks: deque[str] = deque()
        self._size = 0
        self.truncated = False

    def write(self, s: str) -> int:
        """Append text, evicting the oldest chunks beyond the size cap."""
        self._chunks.append(s)
        self._size += len(s)
        while self._size > self._max_chars and len(self._chunks) > 1:
            self._size -= len(self._chunks.popleft())
            self.truncated = True
        return len(s)

    def writable(self) -> bool:
        return True

    def getvalue(self) -> str:
        """Return the retained tail of everything written."""
        return "".join(self._chunks)


def execute(args: argparse.Namespace) -> int:
    """Execute the run command and return the exit code."""
    should_export = not args.no_export
    if should_export and not ANALYSIS_AVAILABLE:
        print_warning(
            "Analysis dependencies not available. Skipping export. "
            "Install with: uv sync --extra analysis"
        )
        should_export = False

    print("Initializing simulation...")
    sim = create_and_setup_simulation(
        planets=args.planets,
        actors=args.actors,
        makers=args.makers,
        operators=args.operators,
        ships=args.ships,
        arms=args.arms,
        lane_density=args.lane_density,
        lands_per_planet=args.lands_per_planet,
    )
    if args.workers > 1:
        sim.parallel_workers = args.workers
        print(f"  Actor phase: {args.workers} threads")
        if sys._is_gil_enabled():
            print_warning(
                f"{GIL_ENABLED_WARNING}: --workers runs correctly but gives no "
                "speedup. Use a free-threaded interpreter (e.g. 3.14t); "
                "see docs/performance.md"
            )

    num_logged = configure_actor_logging(sim, args.log_actors)
    print(f"  Logging {num_logged} actor(s)")

    exporter: Any = None
    output_path: Path | None = None

    if should_export:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_path = OUTPUT_DIR / run_id

        print(f"\nSetting up export to: {output_path}")
        exporter = SimulationExporter(output_path, run_id)
        exporter.setup(sim)
        sim.exporter = exporter

    print(f"\nRunning {args.turns} turns...")
    print("=" * 60)

    # Suppress simulation stdout, keeping a bounded tail of any warnings.
    captured = _TailCapture()
    old_stdout = sys.stdout
    sys.stdout = captured

    try:
        iterator = range(args.turns)
        if not args.quiet:
            from tqdm import tqdm

            # tqdm writes to stderr, so stdout stays suppressed.
            iterator = tqdm(iterator, desc="Simulating turns", file=sys.stderr)

        for _ in iterator:
            sim.run_turn()
    finally:
        sys.stdout = old_stdout

    tail = captured.getvalue().strip()
    if tail and not args.quiet:
        header = "Simulation warnings"
        if captured.truncated:
            header += " (truncated to last 64KB)"
        print(f"\n{header}:\n{tail}")

    # Tier-0 readout.
    summary_json = Path(args.summary_json) if args.summary_json else None
    if args.summary or summary_json is not None:
        _emit_summary(sim, output_path if should_export else None, summary_json)

    if should_export and exporter is not None:
        print("\n" + "=" * 60)
        print("Finalizing export...")
        exporter.finalize(sim)

        print_success("Simulation complete!")
        print(f"  Data exported to: {output_path}")

        if args.notebook and output_path is not None:
            _open_notebook(output_path)
        else:
            print("\nTo analyze this run:")
            print(
                f"  SPACESIM_RUN_PATH='{output_path}' marimo edit --no-token {NOTEBOOK_PATH}"
            )
    else:
        print("\n" + "=" * 60)
        print_success("Simulation complete!")

    return 0


def _emit_summary(
    sim: Any, output_path: Path | None, summary_json: Path | None = None
) -> None:
    """Print the compact KPI summary, delimited for parsing.

    Args:
        output_path: If exporting, the run directory to also write
            summary.json.
        summary_json: Explicit file to write the same payload to
            (``--summary-json``), independent of exporting.
    """
    from spacesim.analysis.summary import compute_summary

    summary = compute_summary(sim)
    payload = json.dumps(summary, indent=2)

    # Delimiters let an agent slice the JSON out of mixed stdout.
    print("\n===SUMMARY_BEGIN===")
    print(payload)
    print("===SUMMARY_END===")

    if output_path is not None:
        summary_file = output_path / "summary.json"
        summary_file.write_text(payload)
        print(f"Summary written to: {summary_file}")

    if summary_json is not None:
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(payload)
        print(f"Summary written to: {summary_json}")


def _open_notebook(output_path: Path) -> None:
    """Open the analysis notebook with the run path set."""
    print("\nOpening marimo notebook...")

    env = os.environ.copy()
    env["SPACESIM_RUN_PATH"] = str(output_path)

    try:
        subprocess.run(
            ["marimo", "edit", "--no-token", str(NOTEBOOK_PATH)],
            env=env,
            check=False,
        )
    except FileNotFoundError:
        print_warning("marimo not found. Install with: uv sync --extra analysis")
        print("\nTo analyze manually:")
        print(
            f"  SPACESIM_RUN_PATH='{output_path}' marimo edit --no-token {NOTEBOOK_PATH}"
        )
