"""`dev ab`: compare the working tree against a baseline git ref with replicates.

The baseline is a detached ``git worktree`` at ``--base``, never a stash, so
it is safe while other agents edit the tree. Runs alternate between the two
arms (before_1, after_1, before_2, ...) so machine drift lands on both. Each
run writes its KPI summary JSON to ``--out``; a rep whose JSON already exists
is skipped unless ``--fresh``, so an interrupted batch resumes. ``--python``
runs both arms under one interpreter, such as the free-threaded side venv,
and ``--workers`` threads each run's actor phase. When every run is in, the
command prints one table of per-KPI means with a variance-aware verdict and
writes ``manifest.json``, ``table.txt`` and ``done`` (holding the exit code)
to ``--out`` so a background caller can wait on the sentinel.

The aggregation and verdict functions are pure and unit-tested against
hand-written summary dicts; the KPI list is data-driven (dotted paths into the
summary dict), so a summary schema change means editing a table, not code.
"""

from __future__ import annotations

import argparse
import enum
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from spacesim2.cli.commands.run import GIL_ENABLED_WARNING
from spacesim2.cli.output import print_error, print_success, print_warning

ARMS = ("before", "after")

# Multiplier on the pooled standard deviation a delta must exceed to be
# called IMPROVE or REGRESS rather than neutral.
VERDICT_SD_MULTIPLE = 2.0

# Written to <out>/ when every run has finished; holds the exit code.
DONE_FILE = "done"

# Printed for a KPI that a run's summary does not contain.
MISSING = "n/a"


class Direction(enum.Enum):
    """Which way a KPI should move for a change to count as an improvement."""

    HIGHER = "higher"
    LOWER = "lower"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Kpi:
    """A dotted path into the summary dict and the direction that is better."""

    path: str
    direction: Direction


# Trade KPIs the hand-built harness tracked, money, and the prosperity block.
# Drive KPIs are added per batch from the drive names the summaries contain.
DEFAULT_KPIS: tuple[Kpi, ...] = (
    Kpi("verdict.status", Direction.UNKNOWN),
    Kpi("money.mean", Direction.UNKNOWN),
    Kpi("trade.idle_ships", Direction.LOWER),
    Kpi("trade.stranded_ships", Direction.LOWER),
    Kpi("trade.departures_window", Direction.HIGHER),
    Kpi("trade.ship_delivered_total", Direction.HIGHER),
    Kpi("trade.fuel_ask_planets", Direction.HIGHER),
    Kpi("trade.fuel_sold_by_service_window", Direction.HIGHER),
    Kpi("trade.ship_money_median", Direction.HIGHER),
    Kpi("trade.ships_solvent_share", Direction.HIGHER),
    Kpi("trade.ship_fuel_price", Direction.LOWER),
    Kpi("prosperity.index_mean", Direction.HIGHER),
    Kpi("prosperity.gate_pass_share", Direction.HIGHER),
    Kpi("prosperity.coverage.food", Direction.HIGHER),
    Kpi("prosperity.coverage.clothing", Direction.HIGHER),
    Kpi("prosperity.coverage.shelter", Direction.HIGHER),
    Kpi("prosperity.coverage.health", Direction.HIGHER),
    Kpi("prosperity.coverage.luxury", Direction.HIGHER),
    Kpi("prosperity.coverage.computing", Direction.HIGHER),
)

# Directions for the per-drive keys under ``drives.<name>``.
DRIVE_KPI_DIRECTIONS: tuple[tuple[str, Direction], ...] = (
    ("mean_health", Direction.HIGHER),
    ("pct_deprived", Direction.LOWER),
)


class AbSetupError(Exception):
    """Raised when the baseline worktree or a sim run cannot be set up."""


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'ab' dev subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "ab",
        help="A/B the working tree against a baseline git ref with replicate runs",
        description=(
            "Run N interleaved replicate sims of a baseline ref (in a detached "
            "worktree) and of the working tree, then print a per-KPI table with "
            "mean, sd, delta and an IMPROVE/REGRESS/neutral verdict. Resumable: "
            "existing per-run JSON files are reused unless --fresh."
        ),
    )
    parser.add_argument(
        "--base", type=str, required=True, help="Baseline git ref (commit, tag, branch)"
    )
    parser.add_argument(
        "--reps", type=int, default=3, help="Replicate runs per arm (default 3)"
    )
    parser.add_argument(
        "--turns", type=int, default=450, help="Turns per run (default 450)"
    )
    parser.add_argument(
        "--planets", type=int, default=100, help="Planets per run (default 100)"
    )
    parser.add_argument(
        "--python",
        type=str,
        default="",
        metavar="PATH",
        help="Interpreter for both arms, run as `PATH -m spacesim2.cli.main` "
        "with PYTHONPATH set to each arm's source, e.g. .venv-ft/bin/python "
        "for free-threaded runs. Default: `uv run` in each arm's own venv.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Actor-phase threads per run, passed to `spacesim2 run --workers` "
        "(default 1, serial). Speeds runs up only on a free-threaded --python.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="tmp/ab_out",
        help="Output directory for per-run JSON, table.txt, manifest.json, done",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Re-run reps whose JSON already exists instead of reusing them",
    )
    parser.add_argument(
        "--kpi",
        action="append",
        default=[],
        metavar="DOTTED.PATH",
        help="KPI to report (repeatable), replacing the default set. A path "
        "with no known direction gets a delta but no verdict.",
    )
    parser.set_defaults(func=execute)
    return parser


# --- pure aggregation -------------------------------------------------------


def lookup(summary: dict[str, object], path: str) -> object:
    """Resolve a dotted path in a summary dict.

    Raises:
        KeyError: if any segment is missing or a non-dict is indexed.
    """
    node: object = summary
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            raise KeyError(path)
        node = node[segment]
    return node


def known_direction(path: str) -> Direction:
    """Direction for a path, from the default set or the drive key table."""
    for kpi in DEFAULT_KPIS:
        if kpi.path == path:
            return kpi.direction
    segments = path.split(".")
    if len(segments) == 3 and segments[0] == "drives":
        for key, direction in DRIVE_KPI_DIRECTIONS:
            if segments[2] == key:
                return direction
    return Direction.UNKNOWN


def discover_drive_kpis(summaries: Sequence[dict[str, object]]) -> list[Kpi]:
    """Per-drive KPIs for every drive name any summary reports."""
    names: set[str] = set()
    for summary in summaries:
        drives = summary.get("drives")
        if isinstance(drives, dict):
            names.update(str(name) for name in drives)
    return [
        Kpi(f"drives.{name}.{key}", direction)
        for name in sorted(names)
        for key, direction in DRIVE_KPI_DIRECTIONS
    ]


def default_kpis(summaries: Sequence[dict[str, object]]) -> list[Kpi]:
    """The default KPI set: the fixed table plus drives found in ``summaries``."""
    return list(DEFAULT_KPIS) + discover_drive_kpis(summaries)


def _pooled_sd(before: Sequence[float], after: Sequence[float]) -> float:
    """Pooled standard deviation of two samples.

    Raises:
        statistics.StatisticsError: if neither arm has two or more values.
    """
    degrees = (len(before) - 1) + (len(after) - 1)
    if degrees < 1:
        raise statistics.StatisticsError("pooled sd needs at least two values")
    total = 0.0
    if len(before) > 1:
        total += (len(before) - 1) * statistics.variance(before)
    if len(after) > 1:
        total += (len(after) - 1) * statistics.variance(after)
    return math.sqrt(total / degrees)


def judge(before: Sequence[float], after: Sequence[float], direction: Direction) -> str:
    """Classify a delta as IMPROVE, REGRESS, or neutral.

    IMPROVE/REGRESS need a known direction, at least two values across the
    arms, and ``|delta| > VERDICT_SD_MULTIPLE * pooled sd``. Everything else,
    including an unknown direction, is an empty string (no verdict).
    """
    if direction is Direction.UNKNOWN or not before or not after:
        return ""
    try:
        sd = _pooled_sd(before, after)
    except statistics.StatisticsError:
        return "neutral"
    delta = statistics.fmean(after) - statistics.fmean(before)
    if abs(delta) <= VERDICT_SD_MULTIPLE * sd:
        return "neutral"
    better = delta > 0 if direction is Direction.HIGHER else delta < 0
    return "IMPROVE" if better else "REGRESS"


@dataclass(frozen=True)
class Row:
    """One formatted table row."""

    kpi: str
    before: str
    after: str
    delta: str
    verdict: str


def _numeric(values: Sequence[object]) -> list[float]:
    """Values as floats when every one is a number (bool excluded), else []."""
    if not values or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
    ):
        return []
    return [float(v) for v in values]  # type: ignore[arg-type]


def _arm_text(values: Sequence[float]) -> str:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return f"{mean:.3g} ± {sd:.2g} ({len(values)})"


def _collect(summaries: Sequence[dict[str, object]], path: str) -> list[object]:
    """Values at ``path`` in each summary; raises KeyError on the first miss."""
    return [lookup(summary, path) for summary in summaries]


def build_row(
    kpi: Kpi,
    before: Sequence[dict[str, object]],
    after: Sequence[dict[str, object]],
) -> Row:
    """Aggregate one KPI over both arms into a table row.

    Numeric KPIs get mean ± sd (n), delta and verdict. Non-numeric KPIs, such
    as ``verdict.status``, list their values per arm. A KPI missing from any
    run of an arm reads ``n/a`` for that arm.
    """
    try:
        before_values = _collect(before, kpi.path)
    except KeyError:
        before_values = []
    try:
        after_values = _collect(after, kpi.path)
    except KeyError:
        after_values = []

    before_nums = _numeric(before_values)
    after_nums = _numeric(after_values)
    if before_nums and after_nums:
        delta = statistics.fmean(after_nums) - statistics.fmean(before_nums)
        return Row(
            kpi.path,
            _arm_text(before_nums),
            _arm_text(after_nums),
            f"{delta:+.3g}",
            judge(before_nums, after_nums, kpi.direction),
        )

    def arm_text(values: Sequence[object], nums: Sequence[float]) -> str:
        if not values:
            return MISSING
        if nums:
            return _arm_text(nums)
        return ",".join(str(v) for v in values)

    return Row(
        kpi.path,
        arm_text(before_values, before_nums),
        arm_text(after_values, after_nums),
        "",
        "",
    )


def build_rows(
    kpis: Sequence[Kpi],
    before: Sequence[dict[str, object]],
    after: Sequence[dict[str, object]],
) -> list[Row]:
    """One row per KPI, in the order given."""
    return [build_row(kpi, before, after) for kpi in kpis]


def format_table(rows: Sequence[Row]) -> str:
    """Render rows as an aligned text table with a header line."""
    header = Row("kpi", "before", "after", "delta", "verdict")
    columns = [header, *rows]
    width_kpi = max(len(r.kpi) for r in columns)
    width_before = max(len(r.before) for r in columns)
    width_after = max(len(r.after) for r in columns)
    width_delta = max(len(r.delta) for r in columns)
    lines = [
        f"{r.kpi:<{width_kpi}}  {r.before:>{width_before}}  "
        f"{r.after:>{width_after}}  {r.delta:>{width_delta}}  {r.verdict}".rstrip()
        for r in columns
    ]
    return "\n".join(lines)


# --- git worktree and run orchestration ------------------------------------


def _git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stripped stdout; raise on failure."""
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AbSetupError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def repo_root() -> Path:
    """The git top level of the current directory."""
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))


def baseline_worktree_path(sha: str) -> Path:
    """Where the baseline worktree for ``sha`` lives: $TMPDIR/spacesim2_base_<sha8>."""
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    return tmp / f"spacesim2_base_{sha[:8]}"


def ensure_baseline_worktree(root: Path, sha: str, sync: bool) -> Path:
    """Materialize a detached worktree at ``sha`` and, if ``sync``, its venv.

    An existing directory is reused as is. ``.python-version`` is gitignored,
    so it is copied in; without it the baseline could run on another
    interpreter than the working tree. ``sync`` is False when an explicit
    ``--python`` runs the arms, since the worktree's own venv is then unused.
    """
    worktree = baseline_worktree_path(sha)
    if worktree.exists():
        return worktree
    _git(root, "worktree", "add", "--detach", str(worktree), sha)
    python_version = root / ".python-version"
    if python_version.exists():
        shutil.copy(python_version, worktree / ".python-version")
    if not sync:
        return worktree
    synced = subprocess.run(
        ["uv", "sync", "--project", str(worktree), "-q"],
        capture_output=True,
        text=True,
    )
    if synced.returncode != 0:
        raise AbSetupError(f"uv sync of baseline failed: {synced.stderr.strip()}")
    return worktree


def spacesim_command(project: Path, python: Path | None) -> list[str]:
    """Argv prefix that runs the spacesim2 CLI from ``project``'s source.

    With no ``python`` the CLI runs through ``uv run`` in the project's own
    venv. With ``python`` it runs as ``python -m spacesim2.cli.main``, and
    ``run_env`` points PYTHONPATH at ``project`` so the arm imports its own
    code rather than whatever that interpreter has installed.
    """
    if python is None:
        return ["uv", "run", "--project", str(project), "spacesim2"]
    return [str(python), "-m", "spacesim2.cli.main"]


def run_env(project: Path, python: Path | None) -> dict[str, str]:
    """Environment for a run in ``project``; sets PYTHONPATH under ``python``."""
    env = os.environ.copy()
    if python is not None:
        env["PYTHONPATH"] = str(project)
    return env


def run_help(project: Path, python: Path | None) -> str:
    """The ``spacesim2 run --help`` text of ``project``'s source.

    Used to detect flags an older baseline predates: without
    ``--summary-json`` a run falls back to slicing the JSON out of stdout,
    and without ``--workers`` a threaded batch cannot run that arm.
    """
    result = subprocess.run(
        [*spacesim_command(project, python), "run", "--help"],
        capture_output=True,
        text=True,
        cwd=project,
        env=run_env(project, python),
    )
    if result.returncode != 0:
        raise AbSetupError(
            f"`spacesim2 run --help` failed in {project}: {result.stderr.strip()}"
        )
    return result.stdout


def extract_summary(stdout: str) -> str:
    """Slice the summary JSON out of stdout between the delimiters."""
    begin = "===SUMMARY_BEGIN==="
    end = "===SUMMARY_END==="
    start = stdout.find(begin)
    stop = stdout.find(end, start)
    if start < 0 or stop < 0:
        raise AbSetupError("no summary delimiters in run output")
    return stdout[start + len(begin) : stop].strip()


def run_one(
    project: Path,
    turns: int,
    planets: int,
    json_path: Path,
    log_path: Path,
    use_summary_json: bool,
    python: Path | None = None,
    workers: int = 1,
) -> float:
    """Run one sim in ``project`` and leave its summary at ``json_path``.

    Returns wall seconds. Combined stdout and stderr go to ``log_path``.
    A threaded run whose interpreter kept the GIL still completes, with a
    warning, because the GIL costs speed and not correctness.

    The sim loads ``data/`` relative to the current directory, so the run
    is launched from ``project``; without that the baseline arm ran its own
    code against the working tree's YAML.
    """
    cmd = [
        *spacesim_command(project, python),
        "run",
        "--turns",
        str(turns),
        "--planets",
        str(planets),
        "--no-export",
        "--quiet",
    ]
    if workers > 1:
        cmd += ["--workers", str(workers)]
    if use_summary_json:
        cmd += ["--summary-json", str(json_path)]
    else:
        cmd.append("--summary")
    start = time.monotonic()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project,
        env=run_env(project, python),
    )
    elapsed = time.monotonic() - start
    output = (result.stdout or "") + (result.stderr or "")
    log_path.write_text(output)
    if workers > 1 and GIL_ENABLED_WARNING in output:
        print_warning(f"GIL was enabled in {log_path.name}; threads gave no speedup")
    if result.returncode != 0:
        raise AbSetupError(f"run failed with exit {result.returncode}; see {log_path}")
    if not use_summary_json:
        json_path.write_text(extract_summary(result.stdout))
    return elapsed


def _load_summaries(out: Path, arm: str, reps: int) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for i in range(1, reps + 1):
        loaded = json.loads((out / f"{arm}_{i}.json").read_text())
        if not isinstance(loaded, dict):
            raise AbSetupError(f"{arm}_{i}.json is not a JSON object")
        summaries.append(loaded)
    return summaries


def _finish(out: Path, manifest: dict[str, object], code: int) -> int:
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / DONE_FILE).write_text(f"{code}\n")
    return code


def execute(args: argparse.Namespace) -> int:
    """Execute the ab command. Returns 0 when every run completed."""
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    done = out / DONE_FILE
    if done.exists():
        done.unlink()

    if args.workers < 1:
        print_error(f"--workers must be at least 1, got {args.workers}")
        return 1
    # absolute(), not resolve(): a venv's bin/python is a symlink to the base
    # interpreter, and following it would drop the venv's site-packages.
    python = Path(args.python).absolute() if args.python else None
    if python is not None and not python.is_file():
        print_error(f"--python interpreter not found: {python}")
        return 1

    manifest: dict[str, object] = {
        "base_ref": args.base,
        "turns": args.turns,
        "planets": args.planets,
        "reps": args.reps,
        "python": str(python) if python is not None else "uv run",
        "workers": args.workers,
        "kpis": list(args.kpi),
        "runs": {},
    }
    runs: dict[str, dict[str, object]] = {}
    manifest["runs"] = runs

    try:
        root = repo_root()
        base_sha = _git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}")
        manifest["base_sha"] = base_sha
        manifest["head_sha"] = _git(root, "rev-parse", "HEAD")
        manifest["head_dirty"] = bool(_git(root, "status", "--porcelain"))
        worktree = ensure_baseline_worktree(root, base_sha, sync=python is None)
        manifest["base_worktree"] = str(worktree)
        projects = {"before": worktree, "after": root}
        helps = {arm: run_help(project, python) for arm, project in projects.items()}
        summary_json_ok = {arm: "--summary-json" in text for arm, text in helps.items()}
        if args.workers > 1:
            for arm, text in helps.items():
                if "--workers" not in text:
                    raise AbSetupError(
                        f"the {arm} arm's `spacesim2 run` has no --workers flag; "
                        "rerun with --workers 1"
                    )
    except AbSetupError as exc:
        print_error(str(exc))
        return _finish(out, manifest, 1)

    total = args.reps * len(ARMS)
    completed = 0
    for i in range(1, args.reps + 1):
        for arm in ARMS:
            name = f"{arm}_{i}"
            json_path = out / f"{name}.json"
            completed += 1
            if json_path.exists() and not args.fresh:
                runs[name] = {"seconds": 0.0, "reused": True}
                print(f"[ab] {name} reused ({completed}/{total})", file=sys.stderr)
                continue
            try:
                seconds = run_one(
                    projects[arm],
                    args.turns,
                    args.planets,
                    json_path,
                    out / f"{name}.log",
                    summary_json_ok[arm],
                    python,
                    args.workers,
                )
            except AbSetupError as exc:
                print_error(f"{name}: {exc}")
                return _finish(out, manifest, 1)
            runs[name] = {"seconds": round(seconds, 1), "reused": False}
            print(
                f"[ab] {name} done in {seconds:.0f}s ({completed}/{total})",
                file=sys.stderr,
            )

    try:
        before = _load_summaries(out, "before", args.reps)
        after = _load_summaries(out, "after", args.reps)
    except (AbSetupError, json.JSONDecodeError, OSError) as exc:
        print_error(f"could not load run summaries: {exc}")
        return _finish(out, manifest, 1)

    if args.kpi:
        kpis = [Kpi(path, known_direction(path)) for path in args.kpi]
    else:
        kpis = default_kpis(before + after)
    manifest["kpis"] = [kpi.path for kpi in kpis]

    table = format_table(build_rows(kpis, before, after))
    (out / "table.txt").write_text(table + "\n")
    print(table)
    print_success(
        f"{args.base} ({base_sha[:8]}) vs HEAD, {args.reps} reps x "
        f"{args.turns} turns x {args.planets} planets, workers {args.workers}; "
        f"table at {out / 'table.txt'}"
    )
    return _finish(out, manifest, 0)
