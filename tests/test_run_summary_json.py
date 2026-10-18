"""Test for `run --summary-json PATH`."""

import contextlib
import io
import json

from spacesim2.cli.main import main


def test_summary_json_writes_parseable_summary(tmp_path):
    target = tmp_path / "nested" / "summary.json"
    argv = [
        "run",
        "--turns",
        "2",
        "--planets",
        "2",
        "--actors",
        "4",
        "--makers",
        "1",
        "--no-export",
        "--quiet",
        "--summary-json",
        str(target),
    ]
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(argv)

    assert code == 0
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["turns"] == 2
    assert payload["verdict"]["status"] in ("PASS", "WARN", "FAIL")
    # --summary-json implies --summary: the delimited block is still printed.
    assert "===SUMMARY_BEGIN===" in stdout.getvalue()
