# CLAUDE.md - Agent Instructions

## Project: SpaceSim2
A turn-based economic simulation of interplanetary trade with actors, markets, and ships.

## Commands

```bash
# Build & Run
uv sync                              # Install dependencies
uv sync --extra analysis             # Install with analysis features
uv run spacesim2 ui                  # Interactive UI (Pygame)
# Every `run` flag below works the same with `.venv-ft/bin/python -m spacesim2.cli.main run --workers 12`, the preferred form.
uv run spacesim2 run                 # Headless sim with progress bar (default)
uv run spacesim2 run --quiet         # Suppress all output
uv run spacesim2 run --no-export     # Quick run without data export
uv run spacesim2 run --log-actors all  # Detailed per-actor logging (also: N, or an actor name)
uv run spacesim2 run --planets 12    # Smaller galaxy (defaults: 100 planets, 100 actors, 2 makers, 2 operators, 1 ship, 1000 turns)
uv run spacesim2 run --operators 2   # Spaceport operators per planet (default 2)
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12  # Preferred headless run: free-threaded 3.14t, threaded actor phase (setup in docs/performance.md)
uv run spacesim2 run --arms 4 --lane-density 0.3  # Spiral arm count / extra star lanes beyond the spanning tree
uv run spacesim2 run --lands-per-planet 200  # Land pool per planet (default 200); must exceed --actors so migrants can settle

# Development
uv run pytest tests/                           # Run all tests
uv run pytest tests/test_file.py::test_fn -v   # Run single test
uv run mypy .                                  # Type check (correctness)
uv run ruff format .                           # Format
uv run ruff check .                            # Lint

# Dev Tools
uv run spacesim2 dev graph             # Commodity/process dependency graph (outputs to tmp/)
uv run spacesim2 dev graph --out foo   # Custom output path (creates foo.svg and foo.mmd)
uv run spacesim2 dev graph -f png      # Alternative formats: svg (default), png, pdf
uv run spacesim2 dev analyze FILE.py   # Run a Tier-1 analysis script against latest run
uv run spacesim2 dev check             # Umbrella: format+lint+types+pytest+short sim run
uv run spacesim2 dev check --fast      # Skip the slower types and sim stages
uv run spacesim2 dev ab --base <ref> --python .venv-ft/bin/python --workers 12  # A/B against a baseline ref, threaded (see sim-ab-testing skill)
```

## The Dev Loop

Change, then verify:

```bash
uv run pytest -q                                                  # 1. unit tests (~2s)
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 200 --planets 12 --no-export --quiet --summary   # 2. macro behavior (~16 s)
# 3. read JSON between ===SUMMARY_BEGIN=== / ===SUMMARY_END=== (verdict + KPIs)
```

Run sims on the free-threaded side venv with `--workers 12`, from the repo
root. 12 is the dev laptop's physical core count; never set `--workers` above
the physical core count (see `docs/performance.md`). The threaded actor phase gives the same KPIs as a serial stock run
within noise (`docs/performance.md`, "Drift check") and is about 3x faster
at 100 planets and 4x at 12. `.venv-ft` is gitignored; create it once with
the commands in `docs/performance.md`. Fall back to `uv run spacesim2 run`
(stock 3.13, serial) when `.venv-ft` is missing. pytest, mypy, ruff, the UI,
`dev analyze` and `dev check` stay on `uv run`.

Use `--planets 12` for the routine loop: 16 s threaded with verdict PASS on
2026-09-13, and the summary has the same structure as a full run. Drop
`--planets` (100-planet default) only when the question is about the galaxy:
fleet mobility, interplanetary trade, fuel geography. Those KPIs mean nothing
at 12 planets.

### What a run costs

Defaults are 100 planets x 100 actors x 2 makers x 2 operators x 1 ship, and
`--turns` defaults to 1000. Measured on the dev laptop with nothing else
running:

| Config | `uv run spacesim2 run` (3.13, serial) | `.venv-ft`, `--workers 12` |
|--------|------|------|
| `--planets 12`, 200 turns | 70 s | 16 s |
| `--planets 12`, 450 turns | | 66 s |
| 100 planets, 60 turns | 204 s | 68 s |
| 100 planets, 300 turns | | 340 s |

Measured 2026-09-13, `--no-export --quiet`. A threaded 100-planet run
costs about 1.1 s/turn over 300 turns, so 450 turns take about 9 minutes.
Export and `--log-actors all` add to these figures and were not
re-measured.

The Bash tool's default timeout is 120 s (max 600 s). A threaded 12-planet
run of up to 450 turns fits in the foreground with an explicit `timeout`.
Any 100-planet run needs detaching from the harness's task tracker (see
"Long runs" below). Never use a bare `run_in_background: true` for a long
run. `--workers` only speeds
runs up on the free-threaded interpreter; under `uv run` it runs correctly
at serial speed.

`dev check`'s sim stage is separate and cheap: 5 planets x 100 actors x 200
turns, run in-process.

### Long runs and A/B: detach and delegate

`run_in_background: true` alone gets killed by the harness on long sim runs
even with memory to spare (observed 2026-09-13: 100 planets/400 turns, and a
120-turn galaxy run, both killed; 12-planet runs are unaffected). Anything
past the 12-planet routine loop runs detached instead:

```bash
setsid nohup bash -c '/home/timg/code/spacesim2/.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 400 --quiet --summary > OUT.txt 2>&1; echo EXIT $? >> OUT.txt' > /dev/null 2>&1 < /dev/null &
```

Watch `OUT.txt` for the `EXIT` line (Monitor, or a bounded polling loop in a
background Bash), then read the `===SUMMARY_BEGIN===`/`===SUMMARY_END===`
JSON. `$!` is the `setsid` wrapper's PID, not the sim; use
`pgrep -f "spacesim2.cli.main run"` to find the process. The `data/runs/`
export directory appears at start, so its presence is not completion — only
`EXIT` is. Send A/B runs (`dev ab`, which is 100-planet-scale by default) and any
galaxy-scale probe to a subagent (`simulation-analyst` or general-purpose) so
the lead session's context and foreground stay free and a harness kill does
not cost the lead's work; the subagent uses the same detached pattern.

### Shell rules

The shell builtin `cd` is forbidden in Bash commands. A PreToolUse hook
(`hooks/bash-cd-guard.sh`) enforces it: a leading `cd <repo root> &&` is
rewritten away, everything else is denied. Use absolute paths, `git -C DIR`,
`uv run --project DIR`. Never chain `sleep N; command`; give the call an
explicit timeout or `run_in_background: true`. Exclude `.claude/worktrees/`
from searches; it holds stale detached copies of the repo.

`uv run spacesim2 dev check` runs format, lint, types, pytest, and a short
`--summary` sim as one pass/fail gate. It only checks formatting, it does not
rewrite files. Use the three steps above when you need the KPI JSON itself.

- `--summary` prints compact KPI JSON and a `PASS/WARN/FAIL` verdict. When
  exporting it is also written to `summary.json`. Prefer it over a notebook.
- The sim is stochastic and not bit-reproducible. Population means are stable
  to about ±0.05. Assert with tolerances, never exact values. There is no seed
  knob; most randomness comes from `uuid4` and set iteration.
- For open-ended questions write a Tier-1 script and run it with
  `dev analyze`. For human dashboards write a Tier-2 marimo notebook. Durable
  checks go in `tests/test_simulation_smoke.py`. See the `sim-evaluation`
  skill for the tiers and output contract.

The graph command renders with `npx @mermaid-js/mermaid-cli`, so Node.js with
`npx` must be on PATH. Output defaults to `tmp/commodity-graph.svg` (gitignored).

## Code Style
- Python 3.11+ with type annotations
- `ruff format`, 88 char lines
- `snake_case` functions and variables, `PascalCase` classes, `UPPER_CASE` constants
- Domain-driven design; prefer pure functions over stateful classes where it fits

### Tooling
`ruff` handles style (lint and format). `mypy` handles correctness (types).
Do not add a separate formatter.

`hooks/pre-commit` runs `ruff format` and `ruff check` on staged Python files
and `uv run mypy .` over the whole project. mypy is blocking and the project is
clean; keep it that way. A fresh clone must wire the hook once:

```bash
git config core.hooksPath hooks
```

Bypass a single commit with `git commit --no-verify`.

Ruff config: `E501` is ignored because the formatter owns line length.
`notebooks/**` is exempt from lint (still formatted) because marimo's
cross-cell variables trigger false `F401`/`F821`/`I001`.

## Writing

Mannered prose substitutes metaphor and flourish for direct statement. Instead of
"a parameter worth varying", the mannered writer produces "a dial worth turning".
Instead of "this point still matters", they write "this point earns its keep".
The phrases exist to display the writer, not to convey the idea, and readers can
tell. Metaphors drag in connotations the writer did not choose and cannot
control. The fix is to say what you mean. When a literal phrase is available,
use it. Also: state what is true now; do not narrate history or hedge; verify
every flag, path, function, and constant you cite by grepping for it before
you keep the reference.

Prefer a table of numbers to a paragraph about numbers. Lead a report with the
verdict and the 2-5 load-bearing figures. This applies to comments, docstrings,
docs, commit messages, and reports to the user.

## Branching

Work on `main` only. Commit directly to `main`. Do not create feature branches
unless the user asks for one. This overrides the default "branch before
committing on the default branch" behavior.

## Documentation Index

| Topic | Document | When to Read |
|-------|----------|--------------|
| Simulation design | `docs/sim-design.md` | Game mechanics and rules |
| Turn flow & testing | `docs/dev-guide-simulation.md` | Debugging AI, market mechanics, testing |
| Ship trading AI | `docs/dev-guide-ships.md` | Ship brains, fuel and trade logic |
| Spaceport operators | `docs/spaceport-design.md` | Service actors; phase 1 landed, phases 2+ open |
| Migration | `docs/migration-design.md` | Actors moving between planets by ship under passage contracts (v2 landed) |
| Contracts | `docs/contracts-design.md` | Transport jobs: passage, government freight, ship-brain riders and contract trips |
| Prosperity drives | `docs/prosperity-design.md` | Tier 2/3 consumption, tastes, prosperity index; drives landed, UI tiers open |
| Notebook analysis | `notebooks/README.md` | Marimo notebooks |
| Needs/drives system | `docs/needs.md` | Actor consumption |
| Skills system | `docs/skills.md` | Actor skill levels, production |
| Commodities | `docs/commodities.md` | Adding or modifying goods |
| Planet attributes | "Planet Attributes" below | Per-planet resource availability |
| Live galaxy UI | `docs/live-view.md` | Pygame UI development |
| Performance | `docs/performance.md` | Threaded actor phase, caches, open levers |
| Decision log | `docs/decision-log.md` | Why past changes were made |
| Commodity/process editing | `.claude/skills/commodity-process-design/` | Commodities, recipes, production chains |
| Evaluating sim behavior | `.claude/skills/sim-evaluation/` | KPI summary, analysis scripts, notebooks |
| A/B comparison | `.claude/skills/sim-ab-testing/` | Did a change move a KPI beyond noise |
| Open work | `TODO.md` | What is unfinished |
| Prose style | `docs/style.md` | Writing or rewriting comments, docstrings, docs |

## Key Architecture Facts

- **Deferred market matching**: orders are matched at the end of the turn, not when placed.
- **Brain pattern**: actors and ships delegate decisions to pluggable `Brain` classes.
- **Core files**: `core/simulation.py` (main loop), `core/actor.py`, `core/ship.py`, `core/market.py`.
- **Star-lane galaxy**: `core/galaxy.py` builds a spiral layout and a connected planar lane graph. `core/navigation.py` routes along lanes, so distance is always the shortest lane route. Ships fuel a whole route at departure and fly past intermediate planets, except for an opportunistic refuel stop where fuel is cheap (`docs/dev-guide-ships.md`). Default is 100 planets (`--planets`, `--arms`, `--lane-density`). Ship fuel is tracked as integer millifuel, thousandths of a market unit of `nova_fuel` (`Ship.fuel`, `FUEL_MILLI` in `core/ship.py`); market purchases stay in whole units.

## Common Implementation Patterns

### Commodities
Defined in `data/commodities.yaml`:
```yaml
- id: commodity_name
  name: Display Name
  transportable: true/false
  description: Text description
```

Commodities have no planet-specific attributes. Per-planet availability comes
from Planet Attributes below. Use the `commodity-process-design` skill to
modify commodities or processes.

### Planet Attributes and Land

Every planet gets random attributes at setup. A directly constructed `Planet`
defaults to all availabilities 1.0.

Core files: `core/planet_attributes.py` (`PlanetAttributes` dataclass) and
`core/land.py` (`Land`, the per-actor coefficients).

How it works:
1. Each planet gets a random attribute (0.0-1.0) per extractable resource.
   That value is the mean of the planet's land curve for the resource, and
   `land_concentration[resource]` shapes the spread (Beta distribution;
   below 2 U-shaped, near 2 flat, large values tight around the mean, unset
   means every land equals the mean).
2. The planet generates `num_lands` lands at construction, each a draw per
   resource from that curve. Draws below `BARREN_FLOOR` are 0.0. A directly
   constructed `Planet` defaults to `LANDS_PER_PLANET` (100); `setup_simple`
   and the CLI default to `DEFAULT_LANDS_PER_PLANET` (200, `--lands-per-planet`),
   which leaves headroom above the 100-actor default for migrants to settle.
3. `Planet.add_actor` gives every regular actor one land from the pool, at
   random. Service actors never extract and keep the penalty-free default.
   An exhausted pool raises `NoFreeLandError`. `Planet.remove_actor` returns
   a land to the pool and `Planet.add_actor_with_land` attaches one the
   migration phase reserved in advance; see `core/migration.py`.
4. Gathering processes in `data/processes.yaml` set a `resource_attribute` field.
5. `ProcessCommand.execute()` and the brain valuation sites read
   `actor.land.get_availability(...)`, never the planet mean.

Land is fixed for as long as an actor stays on a planet, which is what the
never-reset `yield_modifier` group of `BrainCache` requires. A migrant draws
a new land on arrival, and `relocate_actor` drops the whole cache for that
reason. Tests that need a specific
coefficient set `actor.land = Land({...})` or `Land.at_mean(attributes)`.

Attributes: `biomass`, `fiber`, `wood` (organic), `common_metal_ore`,
`nova_fuel_ore` (mineral).

Effect types:
```yaml
resource_attribute:
  commodity: biomass      # which planet attribute to check
  effect: output          # "output" or "success"
```
- `output`: output quantity scales with availability (0.25 gives 25% of base output).
- `success`: the process fails with probability 1 - availability.

Gathering processes (biomass, fiber, wood) use `output`. Mining processes
(nova_fuel_ore, common_metal_ore) use `success`.

Distributions in `PlanetAttributes.generate_random()`: `biomass` is
uniform(`BIOMASS_DRAW_LOW`, 1.0) with negative draws clamped to exactly 0.0,
`nova_fuel_ore` is bimodal (0.0-0.3 or 0.7-1.0), the rest are
uniform(0.0, 1.0). `BIOMASS_DRAW_LOW` is -0.2, so 1/6 of planets get biomass
0.0; the share is `-low / (1 - low)`. Hand-made food breaks even at biomass
0.167, so those planets cannot feed themselves and must import food or lose
their residents to migration.

Adding a new extractable resource:
1. Add the attribute to `PlanetAttributes` with default 1.0.
2. Add it to `RESOURCE_ATTRIBUTES`, which drives validation, `to_dict()`,
   concentration rolls, and land draws.
3. Add its distribution to `generate_random()`.
4. Add `resource_attribute` to the gathering process in `processes.yaml`.

`planet_attributes.json` (planet means) and `lands.json` (concentration,
free pool, and each actor's claimed coefficients) are written with the other
export files.

### Tool and Facility Requirements

Processes in `data/processes.yaml` specify `tools_required` and
`facilities_required`. The economy has a wood-first bootstrap path, so actors
can start with nothing:

1. Harvest wood (no tools)
2. Make simple tools from wood (no facility)
3. Make building materials from wood (needs tools)
4. Build smelting facility (needs building materials and tools)
5. Mine common metal ore (needs tools)
6. Refine metal (needs smelting facility)
7. Build metalworking facility (needs building materials and tools)
8. Make simple tools from metal (more efficient, needs metalworking facility)
9. Make heavy machinery from metal (needs metalworking facility; consumed as
   upkeep by industrial facilities, for example the chemical plant)
10. Build farm and farm biomass at scale (needs heavy machinery to build,
    then draws on heavy machinery as upkeep, like the chemical plant)

Tools break with 1% probability per use, which keeps tool demand alive.
`ColonistBrain` acquires tools before profitable work. `IndustrialistBrain`
builds the facilities and acquires the tools its chosen recipe needs.

When changing requirements, use the `commodity-process-design` skill and keep
the bootstrap path viable.

#### Capital

A process may carry a `capital` mapping of commodity id to an output bonus
fraction:

```yaml
capital:
  computers: 0.25
```

Holding at least 1 unit multiplies the run's output by `1 + bonus`; bonuses
from several capital goods sum and holding more than one unit adds nothing.
Capital is not a precondition and is not consumed by a run; it breaks at
`CAPITAL_BREAK_PROBABILITY` (0.005) after a successful run, a mean life of
200 runs. Every process with a non-empty `facilities_required` lists
`computers: 0.25`. `IndustrialistBrain` keeps 1 unit of each capital good its
recipe lists, values it at the extra output over its expected life, and never
sells it. See `docs/commodities.md`.

#### Upkeep

A process may also carry an `upkeep` mapping of commodity id to a per-run
probability:

```yaml
upkeep:
  heavy_machinery: 0.01
```

Each entry is rolled once per run, after the input, tool, and facility checks
and before anything is consumed. A hit consumes 1 unit of that commodity; if
the actor does not hold it, the run fails with no side effects, the same way a
missing tool fails. The skill multiplier never doubles an upkeep draw.

Upkeep is not a precondition, so `can_execute_process` ignores it. Recipe
valuation charges the expected cost, probability times the imputed unit cost,
in both `_impute_recipe_cost` and the replacement-cost path. The industrialist
keeps 1 unit of each upkeep good its chosen recipe needs and does not sell that
unit off.

### Drives (Needs)

Drives live in `core/drives/` and inherit from `ActorDrive`.

- `FoodDrive` consumes 1 food every turn.
- `ClothingDrive`, `ShelterDrive`, and `HealthDrive` consume on random events
  with probability `BASE_EVENT_PROB` per turn (1/60, 1/120, and 1/90).

Every drive tracks four 0-1 metrics: `health` (immediate status), `debt`
(accumulated neglect), `buffer` (log-normalized days of supply), and `urgency`
(priority multiplier).

Materials: food uses `food`; clothing uses `clothing`; shelter uses
`simple_building_materials` and the durable `prefab_housing`; health uses
`medicine`. The upgraded goods (`processed_food`, `quality_clothing`,
`advanced_medicine`, `luxury_goods`) belong to `ProsperityDrive` instances,
one per category, which bid only while every need is met and consume at a
rate set by the actor's fixed taste vector. See `docs/prosperity-design.md`.

Durables are held and used, not consumed per event. A drive declares how
many events one unit covers with `material_servings()`, and the brain counts
stock, compares asks, prices bids and sizes orders per serving. A prefab
serves every shelter event and wears out on a 0.1 roll;
`computers` are productive capital, not a drive good. See `docs/needs.md`.

Adding a drive needs the whole supply chain:
1. Raw material and finished good in `data/commodities.yaml`.
2. Gathering and production processes in `data/processes.yaml`.
3. A drive class in `core/drives/`.
4. `colonist.py` and `industrialist.py` considering the need in `decide_economic_action()`.
5. Brains trading the new commodities (they iterate the registry, so no hardcoded list).

Drives fail silently if their commodity is missing from the registry
(`get_commodity()` returns `None`). Check the commodity exists first.

### Dynamic Commodity Handling

Market makers and actor brains iterate the registry rather than a hardcoded list:

```python
# GOOD
all_commodities = [c for c in actor.sim.commodity_registry.all_commodities() if c.transportable]
for commodity in all_commodities:
    ...

# BAD
for commodity in (food, fuel, wood):
    ...
```

### Shared Brain Logic

Algorithms shared between brains go in a module of plain functions, never a
shared base class. `core/brains/dealer.py` is the pattern: `market_maker_2.py`
and `spaceport_operator.py` both import it and call its functions; neither
inherits from anything in it.

### Performance Changes

An optimization must not change what any actor decides (exact semantics).
Per-actor valuation stays per-actor. A shared cache is allowed only when every
actor at the same cache version would compute identical values. Write an
equivalence test that compares the optimized path against a brute-force
reference; `TestBestProcessRankedWalkEquivalence` and
`TestReplacementCostSplitEquivalence` in
`tests/test_registry_and_brain_cache.py` are the existing examples.

## Analysis

The default loop is the Dev Loop above: `--summary` for the verdict, a
`dev analyze` script for open-ended questions. Marimo notebooks are an optional
human-facing dashboard.

```bash
uv run spacesim2 run --notebook          # export and open notebooks/analysis_template.py
# or export on the faster threaded path, then open the notebook
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12
uv run marimo edit --no-token notebooks/analysis_template.py   # finds the latest run via SPACESIM_RUN_PATH
```

Before handing a notebook to the user, run `uv run marimo check` on it and
start the server. Marimo pitfalls: prefix cell-local variables with `_`,
assign conditional outputs to a named variable before displaying, and do not
return unused variables.

When to use what:
- `--summary`: did my change break the economy?
- `dev analyze` script: open-ended behavioral questions, debugging
- `dev ab`: did my change move the KPI, or is that noise?
- Marimo notebook: interactive charts for a human
- pytest: durable assertions, for example:
```python
def test_specific_behavior():
    sim = Simulation()
    sim.setup_simple(num_planets=2, num_regular_actors=10, num_market_makers=1, num_ships=1)
    sim.run_turn()
    # ... assertions
```
