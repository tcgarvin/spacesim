"""Tier-1b in-process probe template.

Copy this file, replace ``classify``, and run it directly (it builds its own
simulation, so ``dev analyze`` does not apply):

    uv run python notebooks/my_probe.py --turns 200 --planets 12 --out tmp/my_probe.json

Smoke test a new probe at ``--planets 12`` for 100 turns before a long run.

Output contract, which keeps agent token cost bounded:
  * Print small aggregates only: one compact table at the end, never a
    per-actor or per-turn dump.
  * Write the full aggregates as one JSON file to ``--out``; the caller reads
    the printed table and opens the JSON only when a number needs checking.
  * Sample every N turns, not every turn. Per-turn sampling multiplies the
    output without changing the histogram.

Worked example, a gate classifier for ships that did not depart. The export
records outcomes (the ship stayed docked); it does not record which check
refused the trip. Read the live objects instead and record the first gate
that blocked each ship::

    def classify(ship: Ship, sim: Simulation) -> str:
        if ship.status is ShipStatus.TRAVELING:
            return "acted"
        if ship.status is ShipStatus.NEEDS_MAINTENANCE:
            return "gate:maintenance"
        if ship.brain.is_stranded():
            return "gate:fuel_no_local_ask"
        if ship.money <= 0:
            return "gate:no_money"
        return "gate:no_profitable_plan"

The order of the checks is the order the brain applies them, so each ship
lands in exactly one bucket and the histogram reads as "how many were stopped
here". For an actor-side probe replace the ``Ship`` loop in ``sample`` with
``sim.actors`` and classify on ``actor.brain`` state the same way.

To A/B a behavior against the same source, monkeypatch it off in a "before"
arm: keep the original, call through, record, return. See
``notebooks/chem_stall_ab.py`` for a committed example, and assert the patch
landed by comparing the wrapped call count to zero.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.ship import Ship, ShipStatus
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 200
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
DEFAULT_SAMPLE_EVERY = 25
WARMUP_DIVISOR = 4  # default warmup is turns // WARMUP_DIVISOR


def classify(ship: Ship, sim: Simulation) -> str:
    """Return the bucket this ship falls into at the moment of sampling.

    Replace the body. The default is a coarse status histogram so the template
    runs as-is; see the module docstring for a gate-classifier version.
    """
    if ship.status is ShipStatus.TRAVELING:
        return "traveling"
    if ship.status is ShipStatus.NEEDS_MAINTENANCE:
        return "needs_maintenance"
    if ship.brain.is_stranded():
        return "docked_stranded"
    return "docked"


def sample(sim: Simulation) -> Counter[str]:
    """Classify every ship once and return the bucket counts."""
    counts: Counter[str] = Counter()
    for ship in sim.ships:
        counts[classify(ship, sim)] += 1
    return counts


def run_probe(
    turns: int,
    planets: int,
    actors: int = DEFAULT_ACTORS,
    sample_every: int = DEFAULT_SAMPLE_EVERY,
    warmup: int = 0,
) -> dict[str, Any]:
    """Build a simulation, run it, and sample the classifier every N turns.

    Returns the aggregates that ``main`` writes to ``--out``: the run
    parameters, per-sample bucket counts keyed by turn, and the bucket totals
    summed over all samples.
    """
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0:
        raise ValueError(
            f"invalid probe parameters: turns={turns} planets={planets} "
            f"sample_every={sample_every} warmup={warmup}"
        )
    sim = create_and_setup_simulation(planets=planets, actors=actors, makers=2)

    for _ in range(min(warmup, turns)):
        sim.run_turn()

    samples: dict[int, Counter[str]] = {}
    for _ in range(max(0, turns - warmup)):
        sim.run_turn()
        if sim.current_turn % sample_every == 0:
            counts = sample(sim)
            samples[sim.current_turn] = counts
            print(f"turn {sim.current_turn}: {dict(counts)}", flush=True)

    totals: Counter[str] = Counter()
    for counts in samples.values():
        totals.update(counts)

    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "ships": len(sim.ships),
        },
        "samples": {str(turn): dict(counts) for turn, counts in samples.items()},
        "totals": dict(totals),
    }


def print_table(result: dict[str, Any]) -> None:
    """Print the bucket totals as one compact table."""
    totals: dict[str, int] = result["totals"]
    n_samples = len(result["samples"])
    grand = sum(totals.values())
    print(f"\n{n_samples} samples, {grand} ship-observations")
    print(f"{'bucket':>28} {'count':>7} {'share':>6}")
    for bucket, count in sorted(totals.items(), key=lambda kv: -kv[1]):
        share = 100.0 * count / grand if grand else 0.0
        print(f"{bucket:>28} {count:>7} {share:>5.0f}%")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    parser.add_argument(
        "--actors", type=int, default=DEFAULT_ACTORS, help="Regular actors per planet"
    )
    parser.add_argument("--sample-every", type=int, default=DEFAULT_SAMPLE_EVERY)
    parser.add_argument(
        "--warmup",
        type=int,
        default=-1,
        help="Turns to run before the first sample (default: a quarter of --turns)",
    )
    parser.add_argument("--out", type=Path, required=True, help="JSON output path")
    args = parser.parse_args(argv)
    warmup = args.warmup if args.warmup >= 0 else args.turns // WARMUP_DIVISOR

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        sample_every=args.sample_every,
        warmup=warmup,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
