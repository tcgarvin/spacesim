"""Tier-1b probe: does migration help the actors who move?

Builds a simulation, runs it, and follows every migrant from its departure
turn. For each departure the probe also picks a matched stayer: a regular
actor on the same origin planet whose pressure (max need-drive debt) at that
turn is closest to the migrant's and who never moves. Both cohorts are
re-measured ``FOLLOW_UP_TURNS`` after the departure turn.

    uv run python notebooks/migration_probe.py --turns 400 --planets 12 --out tmp/migration_probe.json

Prints one table: cohort sizes, mean pressure at departure, mean pressure
and prosperity at follow-up, plus flow and pool figures.
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.actor import Actor
from spacesim2.core.drives.prosperity_drive import prosperity_index
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 400
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
FOLLOW_UP_TURNS = (100, 200)


def pressure(actor: Actor) -> float:
    """Max need-drive debt; the sustained-hardship signal brains read."""
    debts = [d.metrics.debt for d in actor.drives if d.WELLBEING]
    return max(debts) if debts else 0.0


def run_probe(turns: int, planets: int, actors: int) -> dict[str, Any]:
    """Run the sim and follow migrants and matched stayers."""
    if turns < 1 or planets < 1:
        raise ValueError(f"invalid probe parameters: turns={turns} planets={planets}")
    sim: Simulation = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2
    )
    actors_by_name = {a.name: a for a in sim.actors}
    # Pressure of every regular actor, sampled each turn, so a stayer can be
    # matched on the migrant's departure turn.
    followed: list[dict[str, Any]] = []
    seen_departures = 0
    departed_names: set[str] = set()
    pressure_by_turn: dict[int, dict[str, float]] = {}

    for _ in range(turns):
        sim.run_turn()
        turn = sim.current_turn
        pressure_by_turn[turn] = {
            a.name: pressure(a) for a in sim.actors if a.claims_land
        }
        new_events = sim.migration_log[seen_departures:]
        seen_departures = len(sim.migration_log)
        for event in new_events:
            departed_names.add(event.actor_name)
            migrant = actors_by_name[event.actor_name]
            p0 = pressure(migrant)
            # Closest stayer on the origin planet at the previous turn's
            # sample (the migrant is already off the planet this turn).
            origin_sample = pressure_by_turn.get(turn - 1, {})
            origin = next(p for p in sim.planets if p.name == event.origin_name)
            candidates = [
                a
                for a in origin.actors
                if a.claims_land and a.name not in departed_names
            ]
            stayer = min(
                candidates,
                key=lambda a: abs(origin_sample.get(a.name, 0.0) - p0),
                default=migrant,
            )
            followed.append(
                {
                    "turn": turn,
                    "migrant": migrant.name,
                    "stayer": stayer.name,
                    "origin": event.origin_name,
                    "destination": event.destination_name,
                    "fare": event.fare,
                    "p0_migrant": p0,
                    "p0_stayer": origin_sample.get(stayer.name, 0.0),
                    "follow": {},
                }
            )
        for record in followed:
            for gap in FOLLOW_UP_TURNS:
                if turn == record["turn"] + gap:
                    m = actors_by_name[record["migrant"]]
                    s = actors_by_name[record["stayer"]]
                    record["follow"][str(gap)] = {
                        "p_migrant": pressure(m),
                        "p_stayer": pressure(s),
                        "pros_migrant": prosperity_index(m),
                        "pros_stayer": prosperity_index(s),
                        "money_migrant": m.money,
                        "money_stayer": s.money,
                        "in_transit": m.in_transit,
                    }
        # Drop the per-turn sample once no follow-up can need it.
        stale = turn - max(FOLLOW_UP_TURNS) - 1
        pressure_by_turn.pop(stale, None)

    populations = [sum(1 for a in p.actors if a.claims_land) for p in sim.planets]
    pools = [len(p.free_lands) for p in sim.planets]
    destinations = Counter(e.destination_name for e in sim.migration_log)
    return {
        "params": {"turns": turns, "planets": planets, "actors": actors},
        "departures": len(sim.migration_log),
        "arrivals": sim.migration_arrivals,
        "in_transit_at_end": sim.migration_departures - sim.migration_arrivals,
        "population_min_max": [min(populations), max(populations)],
        "pool_min_max": [min(pools), max(pools)],
        "top_destinations": destinations.most_common(5),
        "mean_fare": statistics.fmean([e.fare for e in sim.migration_log])
        if sim.migration_log
        else 0.0,
        "followed": followed,
    }


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def print_table(result: dict[str, Any]) -> None:
    """One compact table: flow, pools, and cohort outcomes."""
    print(
        f"\ndepartures {result['departures']}  arrivals {result['arrivals']}  "
        f"in transit {result['in_transit_at_end']}  mean fare {result['mean_fare']:.0f}"
    )
    print(
        f"population min/max {result['population_min_max']}  "
        f"free land min/max {result['pool_min_max']}"
    )
    print(f"top destinations {result['top_destinations']}")
    followed = result["followed"]
    print(
        f"\n{'cohort':>10} {'n':>5} {'p0':>6} {'p@100':>6} {'p@200':>6} "
        f"{'pros@100':>8} {'pros@200':>8} {'money@200':>9}"
    )
    for who in ("migrant", "stayer"):
        row = [
            who,
            str(len(followed)),
            f"{_mean([r[f'p0_{who}'] for r in followed]):.2f}",
        ]
        for key in ("p_", "pros_"):
            for gap in FOLLOW_UP_TURNS:
                vals = [
                    r["follow"][str(gap)][f"{key}{who}"]
                    for r in followed
                    if str(gap) in r["follow"]
                ]
                row.append(f"{_mean(vals):.2f}")
        money = [
            r["follow"]["200"][f"money_{who}"] for r in followed if "200" in r["follow"]
        ]
        row.append(f"{_mean(money):.0f}")
        print(
            f"{row[0]:>10} {row[1]:>5} {row[2]:>6} {row[3]:>6} {row[4]:>6} "
            f"{row[5]:>8} {row[6]:>8} {row[7]:>9}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    parser.add_argument("--actors", type=int, default=DEFAULT_ACTORS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_probe(args.turns, args.planets, args.actors)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
