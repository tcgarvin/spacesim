"""Tests for the `dev ab` aggregation, verdict, and run-launch logic.

Pure functions against hand-written summary dicts; no simulation runs.
"""

import subprocess
from pathlib import Path

from spacesim.cli.commands.dev.ab import (
    Direction,
    Kpi,
    build_row,
    build_rows,
    default_kpis,
    discover_drive_kpis,
    extract_summary,
    format_table,
    judge,
    known_direction,
    lookup,
    run_env,
    run_one,
    spacesim_command,
)
from spacesim.cli.main import create_parser


def _summary(idle: int, status: str = "PASS", health: float = 0.9) -> dict:
    return {
        "money": {"mean": 100.0},
        "drives": {"food": {"mean_health": health, "pct_deprived": 0.01}},
        "trade": {"idle_ships": idle, "departures_window": 20},
        "verdict": {"status": status, "flags": []},
    }


class TestLookup:
    def test_resolves_nested_path(self):
        assert lookup(_summary(3), "trade.idle_ships") == 3

    def test_missing_segment_raises_key_error(self):
        try:
            lookup(_summary(3), "trade.nope")
        except KeyError as exc:
            assert "trade.nope" in str(exc)
        else:
            raise AssertionError("expected KeyError")

    def test_indexing_into_scalar_raises_key_error(self):
        try:
            lookup(_summary(3), "trade.idle_ships.deeper")
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")


class TestJudge:
    def test_large_drop_on_lower_is_better_improves(self):
        assert judge([30, 32, 31], [20, 21, 22], Direction.LOWER) == "IMPROVE"

    def test_large_drop_on_higher_is_better_regresses(self):
        assert judge([30, 32, 31], [20, 21, 22], Direction.HIGHER) == "REGRESS"

    def test_delta_inside_two_pooled_sd_is_neutral(self):
        assert judge([30, 40, 20], [28, 38, 18], Direction.LOWER) == "neutral"

    def test_unknown_direction_gives_no_verdict(self):
        assert judge([30, 32, 31], [20, 21, 22], Direction.UNKNOWN) == ""

    def test_single_rep_per_arm_is_neutral(self):
        assert judge([30], [10], Direction.LOWER) == "neutral"

    def test_identical_reps_with_a_delta_are_called(self):
        assert judge([30, 30], [10, 10], Direction.LOWER) == "IMPROVE"


class TestDirections:
    def test_default_paths_carry_directions(self):
        assert known_direction("trade.idle_ships") is Direction.LOWER
        assert known_direction("trade.departures_window") is Direction.HIGHER

    def test_drive_paths_are_resolved_by_key(self):
        assert known_direction("drives.shelter.mean_health") is Direction.HIGHER
        assert known_direction("drives.shelter.pct_deprived") is Direction.LOWER

    def test_unknown_path_has_unknown_direction(self):
        assert known_direction("prices.food") is Direction.UNKNOWN

    def test_discover_drive_kpis_unions_drive_names(self):
        summaries = [
            {"drives": {"food": {}, "shelter": {}}},
            {"drives": {"food": {}, "health": {}}},
        ]
        paths = [kpi.path for kpi in discover_drive_kpis(summaries)]
        assert paths == [
            "drives.food.mean_health",
            "drives.food.pct_deprived",
            "drives.health.mean_health",
            "drives.health.pct_deprived",
            "drives.shelter.mean_health",
            "drives.shelter.pct_deprived",
        ]

    def test_default_kpis_start_with_verdict_status(self):
        kpis = default_kpis([_summary(1)])
        assert kpis[0].path == "verdict.status"
        assert "drives.food.mean_health" in [kpi.path for kpi in kpis]


class TestBuildRow:
    def test_numeric_row_has_stats_delta_and_verdict(self):
        before = [_summary(30), _summary(32), _summary(31)]
        after = [_summary(20), _summary(21), _summary(22)]
        row = build_row(Kpi("trade.idle_ships", Direction.LOWER), before, after)
        assert row.before.startswith("31 ± ")
        assert row.before.endswith("(3)")
        assert row.after.startswith("21 ± ")
        assert row.delta == "-10"
        assert row.verdict == "IMPROVE"

    def test_status_row_lists_values_per_arm(self):
        before = [_summary(1, "PASS"), _summary(1, "PASS")]
        after = [_summary(1, "PASS"), _summary(1, "WARN")]
        row = build_row(Kpi("verdict.status", Direction.UNKNOWN), before, after)
        assert row.before == "PASS,PASS"
        assert row.after == "PASS,WARN"
        assert row.delta == ""
        assert row.verdict == ""

    def test_missing_key_in_one_arm_reads_n_a(self):
        before = [_summary(1)]
        after = [{"trade": {}}]
        row = build_row(Kpi("trade.idle_ships", Direction.LOWER), before, after)
        assert row.before.endswith("(1)")
        assert row.after == "n/a"
        assert row.delta == ""
        assert row.verdict == ""

    def test_unknown_direction_gets_delta_but_no_verdict(self):
        before = [_summary(1), _summary(1)]
        after = [_summary(1), _summary(1)]
        row = build_row(Kpi("money.mean", Direction.UNKNOWN), before, after)
        assert row.delta == "+0"
        assert row.verdict == ""


class TestFormatTable:
    def test_table_has_header_and_one_line_per_kpi(self):
        before = [_summary(30), _summary(32)]
        after = [_summary(20), _summary(21)]
        kpis = [
            Kpi("verdict.status", Direction.UNKNOWN),
            Kpi("trade.idle_ships", Direction.LOWER),
        ]
        table = format_table(build_rows(kpis, before, after))
        lines = table.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("kpi")
        assert lines[1].startswith("verdict.status")
        assert lines[2].startswith("trade.idle_ships")
        assert lines[2].endswith("IMPROVE")


class TestExtractSummary:
    def test_slices_json_between_delimiters(self):
        stdout = 'noise\n===SUMMARY_BEGIN===\n{"a": 1}\n===SUMMARY_END===\nmore'
        assert extract_summary(stdout) == '{"a": 1}'


def test_dev_ab_is_registered():
    args = create_parser().parse_args(
        ["dev", "ab", "--base", "HEAD", "--kpi", "a.b", "--kpi", "c.d"]
    )
    assert args.base == "HEAD"
    assert args.reps == 3
    assert args.kpi == ["a.b", "c.d"]


class TestRunLaunch:
    def test_default_runs_through_uv_in_the_arm_project(self):
        project = Path("/repo/base")
        assert spacesim_command(project, None) == [
            "uv",
            "run",
            "--project",
            "/repo/base",
            "spacesim",
        ]
        assert run_env(project, None).get("PYTHONPATH") != "/repo/base"

    def test_explicit_python_imports_the_arm_source_via_pythonpath(self):
        project = Path("/repo/base")
        python = Path("/repo/.venv-ft/bin/python")
        assert spacesim_command(project, python) == [
            "/repo/.venv-ft/bin/python",
            "-m",
            "spacesim.cli.main",
        ]
        assert run_env(project, python)["PYTHONPATH"] == "/repo/base"

    def test_run_one_passes_workers_python_and_cwd(self, tmp_path, monkeypatch):
        calls: list[dict[str, object]] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        python = Path("/venv/bin/python")
        run_one(
            tmp_path,
            turns=10,
            planets=12,
            json_path=tmp_path / "after_1.json",
            log_path=tmp_path / "after_1.log",
            use_summary_json=True,
            python=python,
            workers=12,
        )
        (call,) = calls
        cmd = call["cmd"]
        assert isinstance(cmd, list)
        assert cmd[:3] == ["/venv/bin/python", "-m", "spacesim.cli.main"]
        assert cmd[cmd.index("--workers") + 1] == "12"
        assert call["cwd"] == tmp_path
        env = call["env"]
        assert isinstance(env, dict)
        assert env["PYTHONPATH"] == str(tmp_path)

    def test_serial_run_omits_workers_flag(self, tmp_path, monkeypatch):
        commands: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        run_one(
            tmp_path,
            turns=10,
            planets=12,
            json_path=tmp_path / "before_1.json",
            log_path=tmp_path / "before_1.log",
            use_summary_json=True,
        )
        assert "--workers" not in commands[0]


def test_dev_ab_accepts_python_and_workers():
    args = create_parser().parse_args(
        [
            "dev",
            "ab",
            "--base",
            "HEAD",
            "--python",
            ".venv-ft/bin/python",
            "--workers",
            "12",
        ]
    )
    assert args.python == ".venv-ft/bin/python"
    assert args.workers == 12
