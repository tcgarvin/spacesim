---
name: sim-evaluation
description: Run the simulation and evaluate whether it behaves as intended, token-efficiently. Use after changing economy data, drives, brains, or market logic to check macro behavior; for "did my change break the economy", "is the economy healthy", running a quick behavioral check, or writing ad-hoc analysis over a run. Covers the Tier-0 KPI summary, Tier-1 analysis scripts, and Tier-2 human notebooks.
---

# Simulation Evaluation

How to evaluate simulation behavior with the fewest tokens. Two costs
matter: constructing commands and reading output. Use the lowest tier that
answers the question.

## The dev loop

```bash
uv run pytest -q                                                              # 1. unit tests (~2s)
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 200 --planets 12 --no-export --quiet --summary   # 2. behavior (~16 s)
# 3. read the JSON between ===SUMMARY_BEGIN=== / ===SUMMARY_END===
```

Run sims on the free-threaded side venv with `--workers 12`, from the repo
root. It gives the same KPIs as a serial `uv run spacesim2 run` within noise
(`docs/performance.md`, "Drift check") and is about 3x faster at 100 planets
and 4x at 12. Setup is in `docs/performance.md`; when `.venv-ft` is missing,
`uv run spacesim2 run` takes the same flags and runs serially.

Drop `--planets 12` (100-planet default, ~4 min threaded) only when the
question is about fleet geography. See "Run cost and waiting" below before
starting anything longer than two minutes.

## Stochasticity

The simulation is stochastic and not bit-reproducible. Population means over
hundreds of actors (drive health, mean prices) are stable to about ±0.05,
enough to detect real changes. Count-style trade KPIs (`departures_window`,
`stranded_ships`, `ship_delivered_total`, `traded_recent`) are noisier: treat
a difference under 20% on a single run as neutral. There is no run-level seed
knob: most randomness flows through `uuid4` and set iteration, so seeding the
module RNG gave false determinism. Assert with tolerances, never exact
values. For a before/after comparison that needs replicates, use the
`sim-ab-testing` skill (`uv run spacesim2 dev ab --base <ref>`).

## Tier 0: built-in KPI summary

For "is it broken" and "is the economy healthy". One command, one JSON
object, a `PASS/WARN/FAIL` verdict.

```bash
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 200 --no-export --quiet --summary
```

The JSON has these sections; the keys inside each are self-describing, and
`spacesim2/analysis/summary.py` is the authority on what each one measures:

- `turns`, `planets`, `regular_actors`, `service_actors`, `ships`: run size.
- `actors_aboard`: regular actors currently riding a ship as a loaded
  passenger (in transit between planets). `population` is
  `regular_actors + actors_aboard`, the conserved total `sim.population()`
  checks every turn.
- `money`: the regular-actor money distribution.
- `drives`: per-drive mean health, debt, and deprivation share.
- `inventory_totals`, `prices`: population stock totals and mean prices.
- `markets`: volume per planet per turn over the last `window_turns`
  (default 50) plus `traded_recent` / `traded_ever` commodity counts. A
  commodity that once traded and then stopped stays listed at 0.0, which is
  the point: drives can look fine on stock while trade has stopped.
- `trade`: interplanetary movement over the same window (ship-delivered
  units and shares), fleet fuel KPIs (`fuel_ask_planets`, `stranded_ships`,
  `idle_ships`, `departures_window`, service fuel sales), and fuel held
  off-market by service and industrialist actors.
- `verdict`: `status` plus `flags` naming each failed check.

The verdict is a catastrophe floor, not a target: `PASS` means not obviously
broken. It checks per-drive health floors, that the food market is alive, and
on long runs that no drive material's market has frozen. Each drive's floor is
gated on run length, because the tiers ramp at different rates:

| Drive | Judged from turn | warn / fail |
|-------|------------------|-------------|
| food | 0 | 0.70 / 0.50 |
| clothing, shelter | 200 | 0.70 / 0.40 |
| health | 400 | 0.30 / 0.10 |

So a 200-turn run judges food, clothing and shelter but not health, whose
chemistry tier only bootstraps after roughly 300-400 turns. The market
liveness check also waits for turn 400.

Thresholds live in `spacesim2/analysis/summary.py`; mirror any change in
`tests/test_simulation_smoke.py`.

## Tier 1a: export analysis

For questions over logged history: prices by planet and turn, drive trends,
distributions, correlations. The export records what happened each turn.

```bash
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 300   # exports Parquet to data/runs/
cp notebooks/scratch_template.py notebooks/my_question.py
# edit my_question.py: load_run() -> Polars frames
uv run spacesim2 dev analyze notebooks/my_question.py  # runs it, relays stdout
```

`dev analyze` uses the most recent run under `data/runs/`; pass `--run DIR`
for another. `load_run()` (from `spacesim2.analysis.loading`) exposes
`.actor_turns`, `.actor_drives`, `.market_transactions`, `.market_snapshots`
as Polars frames.

**Output contract, which keeps tokens bounded:**
- PRINT small aggregates only: `.describe()`, grouped means, `.head(N)`.
  Never print a raw full DataFrame.
- SAVE figures to `tmp/` with `savefig()` (Agg backend); never display them.
  `dev analyze` reports the saved paths for the human to open. Reason over
  the printed numbers, not the pixels.

## Tier 1b: in-process probe

For any question of the form "why did X not happen": why a ship did not
depart, why no industrialist entered a recipe, why a bid was never placed.
The export records outcomes, not refusals. An in-process probe builds a
`Simulation` inside the script, runs it, and reads the live objects the
export never contains: order books, brain internals, the branch a decision
took, every actor's inventory.

```bash
cp notebooks/probe_template.py notebooks/my_probe.py
# edit classify(); smoke test first:
uv run python notebooks/my_probe.py --turns 100 --planets 12 --out tmp/my_probe.json
```

Run probes with `uv run python notebooks/<file>.py`, not `dev analyze`: they
build their own run. The template takes `--turns`, `--planets`, `--actors`,
`--sample-every`, `--warmup`, `--out`, builds the sim through
`create_and_setup_simulation` in `spacesim2/cli/common.py`, samples every N
turns, writes one JSON file, and prints one table. Committed examples:
`notebooks/medicine_probe.py` (holdings, order books, and WTP decomposition
for one commodity), `notebooks/chem_stall_ab.py` (monkeypatch A/B arm),
`notebooks/starved_medicine_probe.py` (recipe-score decomposition per
planet).

Two shapes cover most probes:

- **Gate classifier.** For every actor or ship that did not act, record the
  first gate that blocked it, in the order the brain applies the gates, then
  print a histogram of gate counts. Each subject lands in one bucket, so the
  table reads as "how many were stopped here". The template's docstring has
  a worked ship-departure example.
- **Monkeypatch A/B arm.** Run the same source twice. In the "before" arm,
  patch the new behavior off at module import (`chem_stall_ab.py` replaces
  two `IndustrialistBrain` methods with no-ops). When wrapping instead of
  replacing, keep the original behavior: call through, record, return.
  Assert the patch landed by counting calls through the wrapper; a wrapper
  that never fires after a refactor produces a silent non-result.

The output contract is the same as Tier 1a: sample every 25 turns or so,
print aggregates, never a per-actor trace.

## Tier 2: marimo notebook (human-facing)

Optional human dashboard, not part of the agent loop. The maintained
dashboard is `notebooks/analysis_template.py`. Extend it, or a copy, only
when a human wants interactive charts, then hand off the open command. Do
not read its HTML export back into context.

```bash
uv run marimo edit --no-token notebooks/analysis_template.py
```

Use the `simulation-analyst` subagent for substantial notebook work.

## Run cost and waiting

Threaded on `.venv-ft` with `--workers 12` and `--no-export --quiet`,
measured 2026-09-13. A serial `uv run` takes about 3-4x as long.

| Run | Wall time |
|-----|-----------|
| 12 planets, 200 turns | ~16 s |
| 12 planets, 450 turns | ~66 s |
| 100 planets, 300 turns | ~6 min (~1.1 s/turn) |
| 100 planets, 450 turns | ~9 min |
| `dev check` sim stage (5 planets, 200 turns, stock serial) | ~25 s |

Rules:
- Up to about 2 minutes, use an explicit Bash `timeout` in the foreground.
  Past that, do not rely on plain `run_in_background: true`: the harness has
  killed long sim runs ("low on memory") even with memory to spare (observed
  2026-09-13 on a 100-planet/400-turn run and a 120-turn galaxy run; 12-planet
  runs are unaffected). Detach instead:
  ```bash
  setsid nohup bash -c '/home/timg/code/spacesim2/.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 400 --quiet --summary > OUT.txt 2>&1; echo EXIT $? >> OUT.txt' > /dev/null 2>&1 < /dev/null &
  ```
  Watch `OUT.txt` for the `EXIT` line (Monitor, or a bounded polling loop),
  then read the summary JSON. `$!` is the `setsid` wrapper, not the sim; use
  `pgrep -f "spacesim2.cli.main run"` if you need the real PID. A
  `data/runs/` export directory appearing is not completion, only `EXIT` is.
- Any run past the 12-planet routine loop, and any A/B (`sim-ab-testing`),
  should go to a subagent (`simulation-analyst` or general-purpose) so the
  lead session's foreground and context are not tied up and a harness kill
  does not lose the lead's work. The subagent uses the detached pattern above
  for galaxy-scale runs.
- Never write `sleep N; <command>`; the harness blocks it. Never poll with
  repeated one-second sleeps.
- Smoke test a probe at `--planets 12` for 100 turns before the real run.
- Use `--planets 12` unless the question is about fleet geography or lane
  structure, which need the 100-planet default.

## Picking a tier

| Question | Tier |
|----------|------|
| Did my change break the economy? | 0 |
| Is food/shelter/money in a healthy range? | 0 |
| Did my fix move KPI Y, or is that noise? | `sim-ab-testing` skill |
| Why is commodity X's price doing Y, by planet/turn? | 1a |
| One-off correlation / breakdown / distribution | 1a |
| Why did ships or actors not do X? | 1b |
| Interactive dashboard for a human to explore | 2 |

Encode any check worth keeping as an assertion in
`tests/test_simulation_smoke.py` so it runs free in the unit loop.
