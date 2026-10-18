"""Run a Tier-1 analysis script against a simulation run.

An analysis script uses ``load_run()`` to get Polars frames, prints compact
aggregates, and saves any figures to a directory. This command runs it with
the run path injected, relays its stdout, and lists any new figure files, so
the agent reads numbers and points the human at charts without rendering
pixels into its context.

See the ``sim-evaluation`` skill and ``notebooks/scratch_template.py``.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from spacesim2.cli.output import print_error, print_success


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'analyze' dev subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "analyze",
        help="Run a Tier-1 analysis script against a run (prints its output)",
        description=(
            "Run an analysis script with SPACESIM_RUN_PATH injected, relay its "
            "stdout, and report any figures it saved. Contract: scripts print "
            "compact aggregates and save figures to the figure dir."
        ),
    )
    parser.add_argument("script", type=str, help="Path to the analysis .py script")
    parser.add_argument(
        "--run",
        type=str,
        default=None,
        help="Run directory to analyze (default: most recent in data/runs).",
    )
    parser.set_defaults(func=execute)
    return parser


def _snapshot(figdir: Path) -> set[Path]:
    """Return the set of files directly in figdir."""
    if not figdir.exists():
        return set()
    return {p for p in figdir.iterdir() if p.is_file()}


def execute(args: argparse.Namespace) -> int:
    """Execute the analyze command."""
    script = Path(args.script)
    if not script.exists():
        print_error(f"Analysis script not found: {script}")
        return 1

    # Imported lazily: the analysis stack is an optional extra, and importing
    # it at module top would break the whole CLI, including plain `run`, on
    # environments without it such as the free-threaded side venv. See
    # docs/performance.md.
    from spacesim2.analysis.loading import (
        NoRunsFoundError,
        get_run_path_with_fallback,
    )

    if args.run is not None:
        run_path = Path(args.run)
    else:
        try:
            run_path = get_run_path_with_fallback()
        except NoRunsFoundError as exc:
            print_error(str(exc))
            return 1
    if not run_path.exists():
        print_error(f"Run directory not found: {run_path}")
        return 1

    figdir = Path("tmp")
    before = _snapshot(figdir)

    env = os.environ.copy()
    env["SPACESIM_RUN_PATH"] = str(run_path)

    print(f"Analyzing run: {run_path.name}  (script: {script})")
    print("=" * 60)
    sys.stdout.flush()  # keep the banner ahead of the subprocess output
    # Use the current interpreter so the venv and deps match.
    result = subprocess.run([sys.executable, str(script)], env=env)
    print("=" * 60)

    new_figures = sorted(_snapshot(figdir) - before)
    if new_figures:
        print("Figures written (open these to view):")
        for fig in new_figures:
            print(f"  {fig}")

    if result.returncode == 0:
        print_success("Analysis complete.")
    else:
        print_error(f"Analysis script exited with code {result.returncode}")
    return result.returncode
