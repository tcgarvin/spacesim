"""The Tier-1b probe template must keep running against the real setup helper.

notebooks/ is not a package, so the template is loaded from its file path. A
signature change in ``create_and_setup_simulation`` or in the ship API the
default classifier reads shows up here instead of in a future session.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "notebooks" / "probe_template.py"
)


@pytest.fixture(scope="module")
def probe_template() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_template", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_probe_samples_every_n_turns(probe_template: ModuleType) -> None:
    result = probe_template.run_probe(
        turns=5, planets=3, actors=6, sample_every=2, warmup=1
    )
    assert result["params"]["ships"] == 3
    assert sorted(result["samples"]) == ["2", "4"]
    for counts in result["samples"].values():
        assert sum(counts.values()) == 3
    assert sum(result["totals"].values()) == 6


def test_run_probe_rejects_bad_parameters(probe_template: ModuleType) -> None:
    with pytest.raises(ValueError):
        probe_template.run_probe(turns=0, planets=3)


def test_main_writes_json_and_table(
    probe_template: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "probe.json"
    code = probe_template.main(
        [
            "--turns",
            "4",
            "--planets",
            "3",
            "--actors",
            "6",
            "--sample-every",
            "2",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    written = json.loads(out.read_text())
    assert written["params"]["warmup"] == 1  # a quarter of --turns by default
    assert written["samples"]
    assert "ship-observations" in capsys.readouterr().out
