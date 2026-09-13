---
name: sim-ab-testing
description: Compare the simulation's behavior before and after a change with replicate runs and a variance-aware verdict. Use when asking "did this fix actually move the KPI", when a single --summary run is ambiguous, when comparing against a baseline commit, or when a change is meant to be behavior-preserving. Covers baseline-by-worktree, replicate counts, and reading the delta table.
---

# Simulation A/B Testing

How to tell a real KPI shift from run-to-run noise. The simulation is
stochastic (see `sim-evaluation`): one `--summary` run answers "is it
broken", not "did my change move idle ships from 31 to 22". For the second
question, run both code states several times and compare means.

## When to A/B instead of a single run

- The claim is a number: a fix should lower stranded ships, raise a drive's
  health, or leave every KPI where it was.
- A single `--summary` run moved in the expected direction, but by less than
  the noise band (population means move about ±0.05 run to run; count-style
  trade KPIs move far more).
- The change is meant to be behavior-preserving (a refactor or an
  optimization) and the table should read neutral on every row.

When the question is "why" rather than "how much", use `sim-evaluation`
Tier 1 instead; the A/B table only says whether a number moved.

## The one command

```bash
uv run spacesim2 dev ab --base <ref> --reps 3 --turns 450 --planets 100 --out tmp/ab_out
```

- `--base` is a git ref (commit, tag, branch). The command checks it out as a
  detached worktree under `${TMPDIR:-/tmp}/spacesim2_base_<sha8>`, copies
  the gitignored `.python-version` in, and runs `uv sync` there once. The
  worktree is reused on later calls. Never `git stash` to build a baseline:
  another agent may be editing the tree, and an interrupted stash loses work.
- The working tree is the "after" arm as it is, uncommitted edits included;
  `manifest.json` records `head_sha` and `head_dirty`.
- Runs alternate before/after so machine drift lands on both arms.
- Resumable: a rep whose JSON exists in `--out` is reused. Pass `--fresh` to
  re-run everything, or use a new `--out` per experiment.
- `--kpi a.b --kpi c.d` replaces the default KPI set with dotted paths into
  the summary dict. A path with no known direction gets a delta but no
  verdict.

Cost: about 31 s per 12-planet 200-turn run, and several minutes per
100-planet 450-turn run, so a default batch is 6 runs of several minutes
each. Run it detached, in a subagent — see below.

## Running detached, and delegating to a subagent

At 100 planets (the default), `dev ab` runs several multi-minute sims back
to back, and plain Bash `run_in_background: true` has been killed by the
harness on runs of this length ("low on memory" with memory to spare,
observed 2026-09-13). Do not background `dev ab` directly. Instead:

1. Delegate the whole `dev ab` invocation to a subagent
   (`simulation-analyst` or general-purpose) — this keeps the lead session's
   foreground and context free, and a harness kill costs only the subagent's
   turn, not the lead's work.
2. Inside the subagent, start `dev ab` detached from the harness's task
   tracker:
   ```bash
   setsid nohup bash -c 'uv run --project /home/timg/code/spacesim2 spacesim2 dev ab --base <ref> --reps 3 --turns 450 --planets 100 --out tmp/ab_out > tmp/ab_out.log 2>&1; echo EXIT $? >> tmp/ab_out.log' > /dev/null 2>&1 < /dev/null &
   ```
3. `dev ab` prints one line per completed run to stderr (`[ab] before_1 done
   in 312s (1/6)`), captured in `tmp/ab_out.log` above. Watch for completion
   by polling the `done` sentinel `dev ab` itself writes to `<out>/done`
   (holding the exit code) — via Monitor, or a bounded loop in a background
   Bash, not a chain of `sleep` calls:
   ```bash
   until [ -f tmp/ab_out/done ]; do sleep 15; done; cat tmp/ab_out/table.txt
   ```
   `$!` after `setsid ... &` is the wrapper's PID, not `dev ab`; use
   `pgrep -f "spacesim2 dev ab"` if you need the real one.

A `--reps 2 --turns 200 --planets 12` A/B (gross-breakage check only, see
"How many replicates" below) finishes inside the foreground timeout and does
not need detaching or delegation.

Then read `<out>/table.txt`, not the per-run JSON files. The raw
`before_<i>.json` / `after_<i>.json` stay there for a follow-up question.

## How many replicates

Decide before running, not after.

| Reps per arm | Detects |
|--------------|---------|
| 2 | gross breakage only (a drive collapsing, a market freezing) |
| 4 | moderate shifts in population means |
| 8 | a 0.21 → 0.14 drive-health shift (t ≈ 2.8 at n=8) |

Count-style trade KPIs (`idle_ships`, `departures_window`,
`stranded_ships`, `ship_delivered_total`) are noisier than means: treat a
difference under 20% as neutral whatever the verdict column says.

## Reading the table

```
kpi                        before          after           delta  verdict
verdict.status             PASS,PASS,PASS  PASS,PASS,WARN
trade.idle_ships           31 ± 2.6 (3)    22.3 ± 1.5 (3)  -8.67  IMPROVE
drives.health.mean_health  0.21 ± 0.03 (3) 0.14 ± 0.02 (3) -0.07  REGRESS
```

- `IMPROVE` / `REGRESS` appear only when `|delta| > 2 × pooled sd`; every
  other row with a known direction reads `neutral`. Directions: idle and
  stranded ships lower is better; departures and delivered units higher;
  drive `mean_health` higher; `pct_deprived` lower. `money.mean` has no
  direction and gets a delta only.
- `verdict.status` lists each run's PASS/WARN/FAIL; a single FAIL in the
  after arm is a catastrophe regardless of the other rows.
- `n/a` means the KPI was missing from a run in that arm (an older baseline
  without that summary key, for example), not a value of zero.

Paste the table into the commit message when the change lands, so the
decision log carries the numbers. Encode any check worth keeping as a
tolerance assertion in `tests/test_simulation_smoke.py`.
