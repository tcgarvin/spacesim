# Analysis Notebooks

This directory holds Tier-1 analysis scripts (plain Python) and one marimo
dashboard. The `sim-evaluation` skill explains the tiers.

## Templates

- `scratch_template.py`: Tier 1a. Loads the latest exported run with
  `load_run()` and prints Polars aggregates. Run with
  `uv run spacesim2 dev analyze notebooks/my_question.py`.
- `probe_template.py`: Tier 1b. Builds its own simulation, samples a
  classifier every N turns, writes one JSON file. Run with
  `uv run python notebooks/my_probe.py --turns 200 --planets 12 --out tmp/my_probe.json`.

## Kept probes

Each probe states its question in its docstring. Probes marked "own sim"
build a `Simulation` in-process and run with `uv run python notebooks/<file>.py`;
the others read the latest export through `dev analyze`.

| File | Question | Own sim |
|------|----------|---------|
| `healthcheck_probe.py` | How do drive health, prices and volume trend over a run, beyond the end-state summary? | no |
| `chem_score_probe.py` | How does the industrialist score chemistry-lab recipes against the ones it picks, term by term? | yes |
| `chem_bootstrap_ab.py` | Does the procurement-bid premium gate the medicine chain? (monkeypatch A/B) | yes |
| `chem_stall_ab.py` | Do the stalled-procurement premium and stuck-recipe abandonment help? (monkeypatch A/B, `before`/`after` arms) | yes |
| `medicine_probe.py` | Why is medicine stockpiled while actors go without it? | yes |
| `ship_medicine_probe.py` | Why do ships never haul medicine between planets? | yes |
| `starved_medicine_probe.py` | Why does no local industrialist enter `make_medicine` on medicine-starved planets? | yes |
| `health_latch_probe.py` | Actors with health debt and no medicine: no local supply, priced out, broke, or stale metric? | yes |
| `medicine_stage_probe.py` | Where do actors who chose `make_medicine` spend their turns, and where does the output go? | yes |
| `tier3_probe.py` | Why are the prefab_housing, luxury_goods, computers and advanced_medicine recipes never selected? Score decomposition. | yes |
| `chain_propagation_probe.py` | Does demand at the top of the medicine chain propagate down to its inputs (refined_chemicals, glass, chemicals, silica)? Fixed config for before/after comparison. | yes |

## Marimo dashboard

`analysis_template.py` is the maintained dashboard (money, prices, volumes,
drive metrics). Open it with:

```bash
uv run spacesim2 run --notebook              # run + auto-open
# or against an existing run:
uv run marimo edit --no-token notebooks/analysis_template.py
```

Requires `uv sync --extra analysis`. Always use `uv run marimo` so the right
environment is used.

### Run path resolution

1. `SPACESIM_RUN_PATH` env var, if set.
2. Otherwise the most recent `data/runs/run_YYYYMMDD_HHMMSS` directory by
   parsed timestamp. Missing runs raise a clear error.
3. The dashboard's "Run Path" text field overrides both.

### Debugging a notebook

`marimo export html` runs the notebook headlessly and prints errors to the
terminal, which `marimo run` does not:

```bash
SPACESIM_RUN_PATH=data/runs/test_run uv run marimo export html \
    notebooks/analysis_template.py -o /tmp/test.html
```

Lint with `uv run marimo check notebooks/file.py`.

### Marimo cell rules

Cell output must be a top-level expression, never nested inside a
conditional. Branch first, assign to a variable, and put the bare variable on
the last line:

```python
@app.cell
def _(data, mo, px):
    if data is None:
        output = mo.md("No data available")
    else:
        output = px.bar(data.to_pandas(), x="name", y="value")
    output
    return (output,)
```

## Exported data

Each run under `data/runs/run_TIMESTAMP/` contains Parquet files:

- `actor_turns.parquet`: actor state per turn (money, inventory, location)
- `actor_drives.parquet`: drive metrics (health, debt, buffer, urgency)
- `market_transactions.parquet`: individual trades
- `market_snapshots.parquet`: market state per turn (prices, volumes, orders)
- `ship_turns.parquet`: ship state per turn, every ship: `turn`, `ship_name`,
  `planet_name` (`"TRAVELING"` in flight), `destination_name`, `status`,
  `money`, `reserved_money`, `fuel` (millifuel), `cargo_units`,
  `cargo_capacity`, `contract_hold_units`, and the cumulative counters
  `departures`, `refuel_stops`, `pickup_stops`, `last_departure_turn`

plus `metadata.json`, `planet_attributes.json` and `galaxy.json`. Load the
Parquet files with `from spacesim2.analysis.loading import load_run`.
