"""Can barren planets earn enough to import food?

Tier-1b in-process probe. One row per ``(turn, planet)``. It asks what the
residents of a biomass-0.0 planet do for a living, what they earn, what ships
buy from them, and whether the money they hold tracks the food they import.

Per planet and sample turn (``--window`` turns of history behind it):

- ``attributes``: every mean in ``planet_attributes.RESOURCE_ATTRIBUTES``.
- ``population``, ``money_median``, ``money_total``: regular residents only.
- ``income``: total credits residents received as sellers on the local
  market, split by commodity, and the share paid by a ``Ship`` buyer.
- ``spend_food_ship`` / ``spend_food_local``: credits residents paid for
  food and processed food, split by whether a ``Ship`` was the seller.
- ``runs``: process runs by process id, counted through a patched
  ``ProcessCommand.execute`` (successes and failures separately) plus
  ``government_work`` from ``GovernmentWorkCommand``.
- ``food_health`` / ``food_debt``: median ``FoodDrive`` metrics.
- ``departures_cum`` / ``arrivals_cum``: as in ``food_security_probe.py``.

The actor phase runs on threads, so both patched counters take a lock.

Run:

    PYTHONPATH=/home/timg/code/spacesim2 \
    /home/timg/code/spacesim2/.venv-ft/bin/python \
        /home/timg/code/spacesim2/notebooks/barren_income_probe.py \
        --turns 600 --planets 50 --warmup 100 --sample-every 50 \
        --workers 6 --out /home/timg/code/spacesim2/tmp/barren_income.json
"""

import argparse
import json
import statistics
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core import migration as migration_module
from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.commands import GovernmentWorkCommand, ProcessCommand
from spacesim2.core.market import Market
from spacesim2.core.planet_attributes import RESOURCE_ATTRIBUTES
from spacesim2.core.ship import Ship
from spacesim2.core.simulation import Simulation

FOOD_IDS = ("food", "processed_food")
FOOD_DRIVE_NAME = "food"

_lock = threading.Lock()
# runs[turn][planet][f"{process_id}:{ok|fail}"] -> count
runs_log: Dict[int, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
arrivals_by_planet: Counter = Counter()
patch_calls: Counter = Counter()


def _record_run(actor: Actor, label: str) -> None:
    turn = actor.sim.current_turn
    planet = actor.planet.name if actor.planet is not None else "none"
    with _lock:
        runs_log[turn][planet][label] += 1
        patch_calls["run"] += 1


def install_patches() -> None:
    """Count process runs, government work, and arrivals. Behavior unchanged."""
    original_process = ProcessCommand.execute
    original_government = GovernmentWorkCommand.execute
    original_relocate = migration_module.relocate_actor

    def process_execute(self, actor):  # type: ignore[no-untyped-def]
        ok = original_process(self, actor)
        _record_run(actor, f"{self.process_id}:{'ok' if ok else 'fail'}")
        return ok

    def government_execute(self, actor):  # type: ignore[no-untyped-def]
        ok = original_government(self, actor)
        _record_run(actor, f"government_work:{'ok' if ok else 'fail'}")
        return ok

    def relocate(actor, destination, land):  # type: ignore[no-untyped-def]
        with _lock:
            patch_calls["relocate"] += 1
            arrivals_by_planet[destination.name] += 1
        return original_relocate(actor, destination, land)

    ProcessCommand.execute = process_execute  # type: ignore[method-assign]
    GovernmentWorkCommand.execute = government_execute  # type: ignore[method-assign]
    migration_module.relocate_actor = relocate  # type: ignore[assignment]


def window_runs(planet_name: str, low: int, high: int) -> Dict[str, int]:
    """Process-run counts on a planet for turns in ``(low, high]``."""
    total: Counter = Counter()
    for turn in range(low + 1, high + 1):
        by_planet = runs_log.get(turn)
        if by_planet is None:
            continue
        counts = by_planet.get(planet_name)
        if counts is not None:
            total.update(counts)
    return dict(total)


def window_flows(market: Market, planet_name: str, cutoff: int) -> Dict[str, Any]:
    """Resident sales, ship purchases, and food spending since ``cutoff``."""
    income_by_commodity: Counter = Counter()
    income_units: Counter = Counter()
    income_from_ships = 0
    ship_buy_units: Counter = Counter()
    ship_buy_value = 0
    food_units_from_ships = 0
    food_units_local = 0
    food_spend_ships = 0
    food_spend_local = 0
    food_prices: Dict[str, List[int]] = {cid: [] for cid in FOOD_IDS}

    for tx in market.transaction_history:
        if tx.turn <= cutoff:
            continue
        cid = tx.commodity_type.id
        seller_resident = (
            isinstance(tx.seller, Actor)
            and tx.seller.actor_type == ActorType.REGULAR
            and tx.seller.planet is not None
            and tx.seller.planet.name == planet_name
        )
        buyer_resident = (
            isinstance(tx.buyer, Actor)
            and tx.buyer.actor_type == ActorType.REGULAR
            and tx.buyer.planet is not None
            and tx.buyer.planet.name == planet_name
        )
        if seller_resident:
            income_by_commodity[cid] += tx.total_amount
            income_units[cid] += tx.quantity
            if isinstance(tx.buyer, Ship):
                income_from_ships += tx.total_amount
        if isinstance(tx.buyer, Ship):
            ship_buy_units[cid] += tx.quantity
            ship_buy_value += tx.total_amount
        if cid in FOOD_IDS:
            food_prices[cid].append(tx.price)
            if buyer_resident:
                if isinstance(tx.seller, Ship):
                    food_units_from_ships += tx.quantity
                    food_spend_ships += tx.total_amount
                else:
                    food_units_local += tx.quantity
                    food_spend_local += tx.total_amount

    return {
        "income_by_commodity": dict(income_by_commodity),
        "income_units": dict(income_units),
        "income_total": sum(income_by_commodity.values()),
        "income_from_ships": income_from_ships,
        "ship_buy_units": dict(ship_buy_units),
        "ship_buy_value": ship_buy_value,
        "food_units_from_ships": food_units_from_ships,
        "food_units_local": food_units_local,
        "food_spend_ships": food_spend_ships,
        "food_spend_local": food_spend_local,
        "food_price": (
            round(statistics.fmean(food_prices["food"]), 2)
            if food_prices["food"]
            else None
        ),
        "processed_food_price": (
            round(statistics.fmean(food_prices["processed_food"]), 2)
            if food_prices["processed_food"]
            else None
        ),
    }


def food_metrics(actors: Sequence[Actor]) -> Dict[str, Optional[float]]:
    health: List[float] = []
    debt: List[float] = []
    for actor in actors:
        for drive in actor.drives:
            if drive.metrics.get_name() != FOOD_DRIVE_NAME:
                continue
            health.append(drive.metrics.health)
            debt.append(drive.metrics.debt)
    if not health:
        return {"food_health": None, "food_debt": None}
    return {
        "food_health": round(statistics.median(health), 3),
        "food_debt": round(statistics.median(debt), 3),
    }


def sample(sim: Simulation, window: int) -> List[Dict[str, Any]]:
    cutoff = sim.current_turn - window
    departures = Counter(event.origin_name for event in sim.migration_log)
    rows: List[Dict[str, Any]] = []
    for planet in sim.planets:
        residents = [a for a in planet.actors if a.actor_type == ActorType.REGULAR]
        money = [a.money for a in residents]
        row: Dict[str, Any] = {
            "turn": sim.current_turn,
            "planet": planet.name,
            "population": len(residents),
            "money_median": statistics.median(money) if money else None,
            "money_total": sum(money),
            "departures_cum": departures.get(planet.name, 0),
            "arrivals_cum": arrivals_by_planet.get(planet.name, 0),
        }
        for attr in RESOURCE_ATTRIBUTES:
            row[f"attr_{attr}"] = round(getattr(planet.attributes, attr), 4)
        row.update(food_metrics(residents))
        row.update(window_flows(planet.market, planet.name, cutoff))
        row["runs"] = window_runs(planet.name, cutoff, sim.current_turn)
        rows.append(row)
    return rows


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    warmup: int,
    sample_every: int,
    window: int,
    workers: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0 or window < 1:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    if workers > 1:
        sim.parallel_workers = workers

    rows: List[Dict[str, Any]] = []
    for index in range(turns):
        sim.run_turn()
        due = sim.current_turn > warmup and sim.current_turn % sample_every == 0
        if not (due or index == turns - 1):
            continue
        sampled = sample(sim, window)
        rows.extend(sampled)
        print(
            f"turn {sim.current_turn}: pop {sum(r['population'] for r in sampled)}, "
            f"resident income {sum(r['income_total'] for r in sampled)}, "
            f"ship food units {sum(r['food_units_from_ships'] for r in sampled)}",
            flush=True,
        )
        # Old turn buckets are never read again once the window has passed.
        for stale in [t for t in runs_log if t <= sim.current_turn - window]:
            del runs_log[stale]

    if patch_calls.get("run", 0) == 0:
        raise RuntimeError("ProcessCommand.execute patch never fired")

    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "ships": len(sim.ships),
            "warmup": warmup,
            "sample_every": sample_every,
            "window": window,
            "workers": workers,
        },
        "rows": rows,
        "migration_departures": sim.migration_departures,
        "migration_arrivals": sim.migration_arrivals,
        "patch_calls": dict(patch_calls),
    }


def _fmt(value: Optional[float], width: int, places: int = 2) -> str:
    return f"{'-':>{width}}" if value is None else f"{value:>{width}.{places}f}"


def print_tables(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["rows"]
    window = result["params"]["window"]
    turns = sorted({r["turn"] for r in rows})
    first, last = turns[0], turns[-1]
    final = [r for r in rows if r["turn"] == last]
    barren = sorted(
        [r for r in final if r["attr_biomass"] == 0.0],
        key=lambda r: -r["population"],
    )
    others = [attr for attr in RESOURCE_ATTRIBUTES if attr != "biomass"]

    print(f"\n=== barren planets (biomass 0.0) at turn {last}, {window}-turn window")
    print(
        f"{'planet':>12} {'pop':>4} {'best other':>22} {'inc/res/t':>10} "
        f"{'shipbuy/t':>10} {'foodimp/t':>10} {'foodloc/t':>10} "
        f"{'money_med':>10} {'debt':>6} {'dep':>5} {'arr':>5}"
    )
    for r in barren:
        best = max(others, key=lambda a: r[f"attr_{a}"])
        pop = max(r["population"], 1)
        print(
            f"{r['planet']:>12} {r['population']:>4} "
            f"{best + ' ' + format(r['attr_' + best], '.2f'):>22} "
            f"{_fmt(r['income_total'] / pop / window, 10, 2)} "
            f"{_fmt(r['ship_buy_value'] / window, 10, 1)} "
            f"{_fmt(r['food_units_from_ships'] / window, 10, 2)} "
            f"{_fmt(r['food_units_local'] / window, 10, 2)} "
            f"{_fmt(r['money_median'], 10, 1)} {_fmt(r['food_debt'], 6, 3)} "
            f"{r['departures_cum']:>5} {r['arrivals_cum']:>5}"
        )

    print("\n=== barren labor: top process runs per sample turn (all barren planets)")
    barren_names = {r["planet"] for r in final if r["attr_biomass"] == 0.0}
    for turn in turns:
        agg: Counter = Counter()
        for r in rows:
            if r["turn"] != turn or r["planet"] not in barren_names:
                continue
            agg.update(r["runs"])
        top = ", ".join(f"{k}={v}" for k, v in agg.most_common(8))
        print(f"turn {turn:>4}: {top}")

    print("\n=== barren money and food over time (summed over barren planets)")
    print(
        f"{'turn':>6} {'pop':>6} {'money':>10} {'income/t':>10} {'shipbuy/t':>10} "
        f"{'foodimp/t':>10} {'foodspend/t':>11} {'debt':>6}"
    )
    for turn in turns:
        group = [r for r in rows if r["turn"] == turn and r["planet"] in barren_names]
        if not group:
            continue
        debts = [r["food_debt"] for r in group if r["food_debt"] is not None]
        print(
            f"{turn:>6} {sum(r['population'] for r in group):>6} "
            f"{sum(r['money_total'] for r in group):>10} "
            f"{sum(r['income_total'] for r in group) / window:>10.1f} "
            f"{sum(r['ship_buy_value'] for r in group) / window:>10.1f} "
            f"{sum(r['food_units_from_ships'] for r in group) / window:>10.2f} "
            f"{sum(r['food_spend_ships'] + r['food_spend_local'] for r in group) / window:>11.1f} "
            f"{_fmt(statistics.fmean(debts) if debts else None, 6, 3)}"
        )

    print(
        f"\nfirst sample {first}, last {last}, "
        f"process runs observed {result['patch_calls'].get('run', 0)}, "
        f"migration departures {result['migration_departures']}"
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=600)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("tmp/barren_income.json"))
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        warmup=args.warmup,
        sample_every=args.sample_every,
        window=args.window,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_tables(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
