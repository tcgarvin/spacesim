"""`dev check` umbrella command: the canonical change->verify sequence.

Runs the gates the dev loop documents, format, lint, types, tests, and a short
macro-behavior run, and prints a single pass/fail block. Non-mutating: the
format stage only checks, it does not rewrite files.
"""

import argparse
import contextlib
import io
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, cast

from spacesim2.cli.output import print_error, print_section, print_success


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'check' dev subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "check",
        help="Run the canonical verify sequence (format, lint, types, tests, sim)",
        description=(
            "Run format -> lint -> types -> pytest -> a short --summary sim run "
            "and print a single pass/fail block. Non-mutating: the format stage "
            "checks only."
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip the slower types and sim stages (format, lint, tests only)",
    )
    parser.set_defaults(func=execute)
    return parser


def _run_subprocess(cmd: list[str]) -> tuple[bool, str]:
    """Run a command, returning (passed, combined stdout and stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


def _stage_format() -> tuple[bool, str]:
    return _run_subprocess([sys.executable, "-m", "ruff", "format", "--check", "."])


def _stage_lint() -> tuple[bool, str]:
    return _run_subprocess([sys.executable, "-m", "ruff", "check", "."])


def _stage_types() -> tuple[bool, str]:
    return _run_subprocess([sys.executable, "-m", "mypy", "."])


def _stage_tests() -> tuple[bool, str]:
    return _run_subprocess([sys.executable, "-m", "pytest", "-q"])


SIM_STAGE_TURNS = 200


def _stage_sim() -> tuple[bool, str]:
    """Macro-behavior stage: run the sim and check the KPI verdict.

    Runs in-process so it reuses the imported simulation code and avoids a
    second interpreter startup.
    """
    # Imported lazily so `dev check --fast` does not pay the import cost.
    from spacesim2.analysis.summary import compute_summary
    from spacesim2.cli.common import create_and_setup_simulation

    sim = create_and_setup_simulation(planets=5, actors=100, makers=2, ships=1)
    # run_turn() prints a per-turn summary; suppress it like `run --quiet`.
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(SIM_STAGE_TURNS):
            sim.run_turn()
        summary = compute_summary(sim)
    verdict = cast(dict[str, Any], summary["verdict"])
    status = verdict["status"]
    detail = f"verdict={status}"
    if verdict["flags"]:
        detail += f" flags={verdict['flags']}"
    # FAIL is the catastrophe floor; WARN is reported but does not fail the gate.
    return status != "FAIL", detail


def execute(args: argparse.Namespace) -> int:
    """Execute the check command. Returns 0 if every stage passed, else 1."""
    stages: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("format", _stage_format),
        ("lint", _stage_lint),
    ]
    if not args.fast:
        stages.append(("types", _stage_types))
    stages.append(("tests", _stage_tests))
    if not args.fast:
        stages.append(("sim", _stage_sim))

    print_section("dev check")
    results: list[tuple[str, bool, float]] = []
    for name, fn in stages:
        start = time.monotonic()
        passed, output = fn()
        elapsed = time.monotonic() - start
        results.append((name, passed, elapsed))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name} ({elapsed:.1f}s)")
        if not passed and output.strip():
            # Indent so the output reads as belonging to the stage.
            for line in output.rstrip().splitlines():
                print(f"        {line}")

    all_passed = all(passed for _, passed, _ in results)
    total = sum(elapsed for _, _, elapsed in results)
    print()
    if all_passed:
        print_success(f"all {len(results)} stages passed ({total:.1f}s)")
        return 0
    failed = [name for name, passed, _ in results if not passed]
    print_error(f"{len(failed)} stage(s) failed: {', '.join(failed)} ({total:.1f}s)")
    return 1
