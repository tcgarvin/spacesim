# Performance

Target scale is 500 planets x 100 actors/planet x 1000 ships. On the dev
laptop it runs at about 2.7 s/turn on a free-threaded interpreter with
`--workers 12` and about 7.1 s/turn serial. History and rejected
alternatives are in `docs/decision-log.md`.

## Threaded actor phase (`--workers N`)

`spacesim2 run --workers N` runs each turn's actor phase on a shared-memory
thread pool, sharding planets round-robin (`core/parallel.py`). Ships,
market matching, and export stay serial.

It needs a free-threaded interpreter. The actor phase is pure-Python
bytecode, so under stock CPython the threads serialize and `--workers`
gives no speedup. The CLI warns when the GIL is enabled. Measured at target
scale on a 16-core host with CPython 3.14.7t, before the exact-caching
passes below:

| Config | Actor phase s | Total s/turn |
|---|---|---|
| stock 3.13, serial | 7.8 | 8.1 |
| 3.14t, serial | 7.3 | 7.9 |
| 3.14t, `--workers 6` | 3.4 | 4.1 |
| 3.14t, `--workers 12` | 2.65 | 3.2 |

The free-threaded build has no measurable single-thread penalty on this
workload.

Threads are safe because an actor turn touches only its own state, its own
planet's market (place and cancel only; matching runs serially after the
ship phase), read-only registries, the data logger (one writer per actor
key), and the lock-wrapped global order-id counter. Planets are disjoint
across shards and each planet's actors shuffle independently, so threading
changes only the random stream. The sim is already non-reproducible.

## Running under 3.14t

`uv sync` cannot build the full project on a free-threaded interpreter
because pygame has no free-threaded build. Use a side venv for headless
runs; the UI stays on the stock interpreter:

```bash
uv python install 3.14t
uv venv --python 3.14t .venv-ft
uv pip install --python .venv-ft/bin/python --only-binary :all: pyyaml tqdm numpy typing-extensions pyarrow
.venv-ft/bin/python -m spacesim2.cli.main run --turns 200 --no-export --workers 12
```

Run it from the repo root: `data/` loads relative to the working directory
and `-m` imports the package from there. `.venv-ft` is gitignored.

pyarrow is the only export dependency and has a free-threaded wheel, so
export runs work here too. polars has no free-threaded wheel and would build
from Rust source, so `dev analyze` scripts and notebooks stay on `uv run`
and read the exported Parquet. `--only-binary :all:` makes a missing wheel
fail instead of starting that build. Importing any C extension without
free-threaded support silently re-enables the GIL; the CLI checks
`sys._is_gil_enabled()` and warns, and `dev ab` repeats the warning per run.

This is the default way to run sims (see `CLAUDE.md`). The KPI comparison
against stock serial runs is under "Drift check" below.

Set `--workers` to the machine's physical core count, never the logical CPU
count, so a sim leaves headroom for other work on the box. The dev laptop has
12 physical cores (4 P-cores with hyperthreading, 8 E-cores) and 16 logical
CPUs, hence `--workers 12`. Ships and matching run serially on the main
thread while the pool is idle, so a run never has more than 12 busy
threads. On another machine:

```bash
lscpu -p=CORE,SOCKET | grep -v '^#' | sort -u | wc -l
```

## Drift check

Measured 2026-09-13 at 62db50d plus the warm-up fix below, `--no-export`,
the default KPI set of `dev ab` (24 rows), verdict rule
`|delta| > 2 x pooled sd`.

| Comparison | Config | Reps per arm | Flagged rows |
|---|---|---|---|
| stock 3.13 serial vs 3.14t serial | 12 planets, 450 turns | 4 | 0 |
| stock 3.13 serial vs 3.14t `--workers 12` | 12 planets, 450 turns | 4 | 0 |
| 3.14t serial vs 3.14t `--workers 12` | 12 planets, 450 turns | 4 | 0 |
| stock 3.13 serial vs 3.14t `--workers 12` | 100 planets, 300 turns | 6 | 0 |

At 3 reps per arm the 100-planet comparison flagged `trade.ship_fuel_price`
(37.6 vs 34.3) because the threaded arm's three runs happened to agree
closely. Three more reps per arm put it at 36.4 ± 1.6 vs 34.7 ± 1.4, which
is neutral. Every run in both arms passed the verdict.

Wall time, machine otherwise idle:

| Config | stock 3.13 serial | 3.14t serial | 3.14t `--workers 12` |
|---|---|---|---|
| 12 planets, 200 turns | 70 s | | 16 s |
| 100 planets, 60 turns | 204 s | 185 s | 68 s |
| 100 planets, 300 turns | | | 340 s |

The audit behind this check found one thread-safety gap added after dccfa12,
now fixed. The navigator's galaxy-wide fuel scan
(`Navigator._fuel_market_scan`) is built lazily and read by migrating actors
for passage fares and by spaceport operators for fuel delivery bids. When no
ship had built it in the previous ship phase, a worker built it inside the
actor phase: it read other planets' live books and wrote their
`Market._quote_cache` from a thread that does not own the market.
`_warm_shared_caches` now builds the scan, and the no-producer fallback ask
depths, before the shards start.
`test_fuel_scan_is_never_built_on_a_worker_thread` covers it.

## What is cached and why it is exact

Every cache below is behavior-exact: it returns what a fresh computation
would. Equivalence tests live in `tests/test_registry_and_brain_cache.py`
and `tests/test_market_lazy_cancel.py`.

| Cache | Where | Why it is exact |
|---|---|---|
| Best bid/ask per commodity | `Market._quote_cache` | Maintained incrementally at placement, cancel, and matching. |
| `Market.quote_version` | `core/market.py` | Bumps at every site where a best quote could change. A spurious bump loses sharing, never exactness. |
| Shared process quote table | `Market.shared_quote_table`, keyed on `(turn, quote_version)` | The actor-independent `(input_cost, output_value, process)` table is shared across actors on one market. Two readers at the same key see identical quotes, and trade history moves only in end-of-turn matching. Skill and yield discounting stay per actor. No lock: one thread per market. |
| `BrainCache` groups | `core/actor_brain.py` | Skill factors key on `Actor.skills_version`, which every successful process bumps. The quote-derived per-process cost/value table is independent of inventory and skills, so the mid-turn re-rank is a multiply and sort, not a second scan. |
| `replacement_quote_parts` | `BrainCache`, turn-scoped per market | Per-process base input cost and per-tool amortized price. Prices are cached for all required tools, owned or not, so ownership never bakes in; the post-process recompute redoes only facility gating, unowned-tool sums, and the cached factors. |
| Trade-history memos | `Market.get_avg_price`, `has_price_signal`, `get_30_day_*` | Trade history is written only inside `match_orders`, so per-turn memos cannot go stale within a turn. |
| Lazy order cancel | `Order.cancelled`, `Market.cancel_order` | Every book reader skips dead orders. Books compact when dead orders outnumber live ones and once per commodity at the top of `match_orders`, so staleness never crosses a turn. Live content and relative order match an eager delete. |
| Order-event recording | `Market.order_event_filter` | Events are recorded only for actors the data logger reads back (`--log-actors`). |
| Process definition tuples | `ProcessDefinition.inputs_items`, `outputs_items`, `requirements` | Built in `__post_init__`; definitions are immutable after load. `Actor.can_execute` loops over the flattened requirements in the same check order. |
| Ranked colonist scan | `_best_process_and_raw_profit` | Entries at or below the government-work bar are dropped before the stable sort, so the registry-order tie-break survives and the walk could never have selected them. |

Sharing quote-derived valuations across actors within a turn is not exact
and is not done: order posting is immediate and only matching is deferred,
so the book each actor sees differs.

## Open levers and ruled-out hypotheses

Open:

- Thread scaling at 12 workers is about 3x. The dev host is an i5-1340P
  laptop (4 P-cores, 8 E-cores, HT, heavy thermal downclock under all-core
  load), which explains most of the CPU-seconds inflation. Re-test on
  server hardware before chasing contention theories.
- Matching still does an O(book) quantity sum per commodity for scarcity
  pressure, and iterates dead books through the `volume_history` key union.
  Matching does not re-sort uncrossed books; the sort is guarded.
- Per-turn cost grows 28-33% over the first 150 turns and then plateaus.
  This is economy warm-up, not a leak.

Ruled out at target scale:

- The global `random` lock. Per-actor RNG streams changed nothing.
- Refcount traffic on shared commodity and process definitions. Per-planet
  registry copies with id-based hashing (branch `per-planet-registries`)
  were noise under threads and 8-17% slower serial.
- Fork-per-turn worker pool. See the decision log.

Profiling on the dev box: `perf` is blocked (`perf_event_paranoid=4`) and
py-spy cannot read 3.14t. Use CPU accounting (`/proc/<pid>/stat` deltas,
pidstat) for threaded runs and stock-interpreter py-spy for serial
attribution.

## Reference bench numbers (single-thread, 50-turn driver)

| Config | ms/turn (pre-campaign) | ms/turn (current) |
|---|---|---|
| P=5, A=80, S=2/planet | 89 | 54 |
| P=10, A=20, S=2/planet | 86 | 33 |
| P=40, A=20, S=2/planet | 937 | 149 |
