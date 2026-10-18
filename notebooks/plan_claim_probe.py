"""How much do ship trade plans overclaim?

Tier-1b in-process probe. ``TraderBrain._best_plan_from`` ranks candidate
plans on absolute ``expected_profit``. This probe follows every executed
trade plan from departure to disposal and compares the claim against what the
lot actually fetched.

Per departure it records the plan (origin, destination, commodity, planned
quantity, ``expected_profit``, ``expected_revenue``, ``total_purchase_cost``,
``total_fuel_cost``), the units actually loaded, and then the realized
outcome of that lot: units and revenue sold at the destination during the
docked stay, units carried onward, units sold elsewhere, units sold at the
liquidation floor, and units still unsold at the end of the run.

Lots are followed by tagging hold units with the departure that carried them.
``Market._execute_transaction`` appends a priced unit lot on every ship buy
and consumes lots FIFO on every ship sale, which is the order the cost-basis
ladder in ``TraderBrain._sell_floor_price`` assumes.

Alongside each departure it stores a compact snapshot of the plan search that
produced the plan: per commodity, the best acceptable candidate under three
ranking metrics (absolute profit, profit per hold unit, profit per trip
turn). That is enough to re-rank the same search after the fact, including
against a calibration factor measured from the realized outcomes.

All wrappers call through unchanged; their call counts are asserted non-zero.

Run:

    PYTHONPATH=/home/timg/code/spacesim2 \
    /home/timg/code/spacesim2/.venv-ft/bin/python \
        /home/timg/code/spacesim2/notebooks/plan_claim_probe.py \
        --turns 400 --planets 50 --warmup 100 \
        --workers 12 --out /home/timg/code/spacesim2/tmp/plan_claim.json
"""

import argparse
import json
import math
import statistics
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.core.market import Market
from spacesim2.core.ship import (
    DISTANCE_PER_TURN,
    LIQUIDATION_FLOOR,
    Ship,
    TradePlan,
    TraderBrain,
)

from spacesim2.cli.common import create_and_setup_simulation  # isort: skip

FOOD_IDS = ("food", "processed_food")

_LOCK = threading.Lock()
patch_calls: Counter = Counter()

# ship name -> commodity id -> FIFO list of {qty, price, dep}
hold: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
    lambda: defaultdict(list)
)
# departure id -> record
departures: Dict[int, Dict[str, Any]] = {}
# ship name -> list of open departure ids
open_deps: Dict[str, List[int]] = defaultdict(list)
_next_dep_id = [0]
# id(market) -> planet name; filled once the galaxy exists
market_planets: Dict[int, str] = {}


def _turn(ship: Ship) -> int:
    return ship.simulation.current_turn


def _travel_turns(distance: float) -> int:
    return max(1, math.ceil(distance / DISTANCE_PER_TURN))


def _record_buy(ship: Ship, commodity_id: str, quantity: int, price: int) -> None:
    with _LOCK:
        hold[ship.name][commodity_id].append(
            {"qty": quantity, "price": price, "dep": None}
        )


def _record_sale(ship: Ship, commodity, quantity: int, price: int, here: str) -> None:
    """Attribute a ship sale to the departure whose lot it came from.

    ``here`` is the planet of the market the trade cleared on, not the ship's
    own planet: matching runs after the ship phase, so a ship that departed
    this turn is already in transit when its last sale executes.
    """
    brain = ship.brain
    basis = brain.basis_per_unit(commodity) if isinstance(brain, TraderBrain) else None
    at_floor = basis is not None and price <= int(basis * LIQUIDATION_FLOOR) + 1
    with _LOCK:
        queue = hold[ship.name][commodity.id]
        remaining = quantity
        untagged = 0
        while remaining > 0 and queue:
            lot = queue[0]
            used = min(remaining, lot["qty"])
            lot["qty"] -= used
            remaining -= used
            dep_id = lot["dep"]
            if dep_id is None:
                untagged += used
            else:
                dep = departures[dep_id]
                bucket = "dest" if here == dep["destination"] else "other"
                dep[f"{bucket}_units"] += used
                dep[f"{bucket}_revenue"] += used * price
                if at_floor:
                    dep[f"{bucket}_floor_units"] += used
            if lot["qty"] <= 0:
                queue.pop(0)
        if remaining > 0:
            patch_calls["sale_units_untracked"] += remaining


def _record_departure(ship: Ship, destination, plan: TradePlan) -> None:
    """Tag the hold units this plan is flying and open a departure record."""
    commodity = plan.commodity
    loaded = ship.cargo.get_quantity(commodity)
    brain = ship.brain
    snapshot = getattr(brain, "_probe_snapshot", None)
    search = None
    if snapshot is not None and snapshot[0] == id(plan):
        search = snapshot[1]
    with _LOCK:
        dep_id = _next_dep_id[0]
        _next_dep_id[0] += 1
        tagged = 0
        basis_cost = 0
        for lot in hold[ship.name][commodity.id]:
            if lot["dep"] is not None or lot["qty"] <= 0:
                continue
            lot["dep"] = dep_id
            tagged += lot["qty"]
            basis_cost += lot["qty"] * lot["price"]
        departures[dep_id] = {
            "id": dep_id,
            "turn": _turn(ship),
            "ship": ship.name,
            "origin": plan.origin.name,
            "destination": plan.destination.name,
            "commodity": commodity.id,
            "planned_qty": plan.quantity,
            "loaded_qty": loaded,
            "tagged_qty": tagged,
            "paid_cost": basis_cost,
            "expected_profit": plan.expected_profit,
            "expected_revenue": plan.expected_revenue,
            "total_purchase_cost": plan.total_purchase_cost,
            "total_fuel_cost": plan.total_fuel_cost,
            "expected_sell_px": plan.expected_sell_price_per_unit,
            "purchase_px": plan.purchase_price_per_unit,
            "distance": round(plan.distance, 2),
            "travel_turns": _travel_turns(plan.distance),
            "arrived": False,
            "left_dest": False,
            "carried_onward": None,
            "dest_units": 0,
            "dest_revenue": 0,
            "dest_floor_units": 0,
            "other_units": 0,
            "other_revenue": 0,
            "other_floor_units": 0,
            "search": search,
        }
        open_deps[ship.name].append(dep_id)


def _remaining_units(ship_name: str, commodity_id: str, dep_id: int) -> int:
    return sum(
        lot["qty"] for lot in hold[ship_name][commodity_id] if lot["dep"] == dep_id
    )


def _note_arrival(ship: Ship) -> None:
    here = ship.planet.name if ship.planet is not None else "?"
    with _LOCK:
        for dep_id in open_deps[ship.name]:
            dep = departures[dep_id]
            if not dep["arrived"] and dep["destination"] == here:
                dep["arrived"] = True


def _summarize_search(
    candidates: List[Dict[str, Any]], winner: Optional[TradePlan]
) -> Dict[str, Any]:
    """Per commodity, the best acceptable candidate under each metric."""
    best: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        entry = best.setdefault(
            cand["commodity"],
            {
                "profit": -1,
                "profit_qty": 0,
                "profit_turns": 1,
                "per_unit": -1.0,
                "per_unit_profit": 0,
                "per_turn": -1.0,
                "per_turn_profit": 0,
            },
        )
        if cand["profit"] > entry["profit"]:
            entry["profit"] = cand["profit"]
            entry["profit_qty"] = cand["qty"]
            entry["profit_turns"] = cand["trip_turns"]
        if cand["per_unit"] > entry["per_unit"]:
            entry["per_unit"] = round(cand["per_unit"], 3)
            entry["per_unit_profit"] = cand["profit"]
        if cand["per_turn"] > entry["per_turn"]:
            entry["per_turn"] = round(cand["per_turn"], 3)
            entry["per_turn_profit"] = cand["profit"]
    return {
        "winner": winner.commodity.id if winner is not None else None,
        "n_candidates": len(candidates),
        "by_commodity": best,
    }


def install_patches() -> None:
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey
    original_start = Ship.start_journey
    original_best = TraderBrain._best_plan_from
    original_acceptable = TraderBrain._plan_acceptable

    def wrapped_execute(
        self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
    ):  # type: ignore[no-untyped-def]
        result = original_execute(
            self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
        )
        if isinstance(buyer, Ship):
            patch_calls["ship_buy"] += 1
            _record_buy(buyer, commodity_type.id, quantity, price)
        if isinstance(seller, Ship):
            patch_calls["ship_sale"] += 1
            _record_sale(
                seller,
                commodity_type,
                quantity,
                price,
                market_planets.get(id(self), "?"),
            )
        return result

    def wrapped_update(self):  # type: ignore[no-untyped-def]
        patch_calls["update_journey"] += 1
        arrived = original_update(self)
        if arrived and self.planet is not None:
            _note_arrival(self)
        return arrived

    def wrapped_start(self, destination, resuming: bool = False):  # type: ignore[no-untyped-def]
        brain = self.brain
        plan = getattr(brain, "_current_plan", None)
        loaded = bool(getattr(brain, "_plan_loaded", False))
        is_plan_departure = (
            isinstance(plan, TradePlan)
            and loaded
            and self.planet is plan.origin
            and destination is plan.destination
        )
        started = original_start(self, destination, resuming=resuming)
        if started:
            patch_calls["journey_started"] += 1
            if is_plan_departure and not resuming:
                patch_calls["plan_departure"] += 1
                _record_departure(self, destination, plan)
        return started

    def wrapped_acceptable(self, plan):  # type: ignore[no-untyped-def]
        ok = original_acceptable(self, plan)
        if ok:
            bucket = getattr(self, "_probe_candidates", None)
            if bucket is not None:
                trip_turns = 2 * _travel_turns(plan.distance) + 1
                qty = max(1, plan.quantity)
                profit = plan.expected_profit
                bucket.append(
                    {
                        "commodity": plan.commodity.id,
                        "qty": plan.quantity,
                        "profit": profit,
                        "trip_turns": trip_turns,
                        "per_unit": profit / qty,
                        "per_turn": profit / trip_turns,
                    }
                )
        return ok

    def wrapped_best(self, origin):  # type: ignore[no-untyped-def]
        patch_calls["best_plan_from"] += 1
        self._probe_candidates = []
        try:
            plan = original_best(self, origin)
        finally:
            candidates = self._probe_candidates
            self._probe_candidates = None
        if plan is not None:
            self._probe_snapshot = (id(plan), _summarize_search(candidates, plan))
        return plan

    Market._execute_transaction = wrapped_execute  # type: ignore[assignment]
    Ship.update_journey = wrapped_update  # type: ignore[assignment]
    Ship.start_journey = wrapped_start  # type: ignore[assignment]
    TraderBrain._best_plan_from = wrapped_best  # type: ignore[assignment]
    TraderBrain._plan_acceptable = wrapped_acceptable  # type: ignore[assignment]


def finalize(sim) -> None:
    """Fill carried_onward and unsold counts for lots still open at run end."""
    with _LOCK:
        for ship in sim.ships:
            for dep_id in open_deps[ship.name]:
                dep = departures[dep_id]
                dep["unsold_at_end"] = _remaining_units(
                    ship.name, dep["commodity"], dep_id
                )
        for dep in departures.values():
            dep.setdefault("unsold_at_end", 0)
            # Units that left the destination market without selling there.
            dep["carried_onward"] = dep["tagged_qty"] - dep["dest_units"]


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    warmup: int,
    workers: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or warmup < 0:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    if workers > 1:
        sim.parallel_workers = workers
    market_planets.update({id(planet.market): planet.name for planet in sim.planets})

    for _ in range(turns):
        sim.run_turn()
        if sim.current_turn % 25 == 0:
            with _LOCK:
                n_dep = len(departures)
                sold = sum(
                    d["dest_units"] + d["other_units"] for d in departures.values()
                )
            print(
                f"turn {sim.current_turn}: {n_dep} plan departures, {sold} lot units sold",
                flush=True,
            )

    finalize(sim)
    if patch_calls["plan_departure"] == 0 or patch_calls["ship_sale"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    rows = [dep for dep in departures.values() if dep["turn"] > warmup]
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "ships": len(sim.ships),
            "warmup": warmup,
            "workers": workers,
            "liquidation_floor": LIQUIDATION_FLOOR,
        },
        "departures": rows,
        "patch_calls": dict(patch_calls),
    }


def _median(values: List[Any]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 2) if clean else None


def _realized_profit(dep: Dict[str, Any]) -> float:
    revenue = dep["dest_revenue"] + dep["other_revenue"]
    return revenue - dep["paid_cost"] - dep["total_fuel_cost"]


def _expected_scaled(dep: Dict[str, Any]) -> float:
    """expected_profit scaled to the units actually loaded."""
    planned = max(1, dep["planned_qty"])
    share = min(1.0, dep["tagged_qty"] / planned)
    margin = dep["expected_revenue"] - dep["total_purchase_cost"]
    return margin * share - dep["total_fuel_cost"]


def commodity_table(
    rows: List[Dict[str, Any]], title: str, limit: int
) -> Dict[str, float]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for dep in rows:
        groups[dep["commodity"]].append(dep)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]
    print(f"\n{title}")
    header = (
        f"{'commodity':>22} {'plans':>6} {'plan_q':>7} {'load_q':>7} {'exp_pft':>8} "
        f"{'dest_gross':>10} {'real_pft':>9} {'r/e':>6} {'sold@dest':>9} "
        f"{'floor/unsold':>12}"
    )
    print(header)
    ratios: Dict[str, float] = {}
    for commodity, deps in ordered:
        loaded = sum(d["tagged_qty"] for d in deps)
        dest_units = sum(d["dest_units"] for d in deps)
        floor_units = sum(d["dest_floor_units"] + d["other_floor_units"] for d in deps)
        unsold = sum(d["unsold_at_end"] for d in deps)
        exp = [d["expected_profit"] for d in deps]
        real = [_realized_profit(d) for d in deps]
        exp_scaled = [_expected_scaled(d) for d in deps]
        pooled_ratio = (
            sum(real) / sum(exp_scaled) if sum(exp_scaled) > 0 else float("nan")
        )
        ratios[commodity] = pooled_ratio
        print(
            f"{commodity:>22} {len(deps):>6} {_median([d['planned_qty'] for d in deps]):>7} "
            f"{_median([d['tagged_qty'] for d in deps]):>7} {_median(exp):>8} "
            f"{_median([d['dest_revenue'] for d in deps]):>10} {_median(real):>9} "
            f"{pooled_ratio:>6.2f} "
            f"{(100.0 * dest_units / loaded if loaded else 0):>8.0f}% "
            f"{(100.0 * floor_units / loaded if loaded else 0):>5.0f}%/"
            f"{(100.0 * unsold / loaded if loaded else 0):<5.0f}%"
        )
    return ratios


def reranking(rows: List[Dict[str, Any]], ratios: Dict[str, float]) -> None:
    """Which commodity each alternative metric would have picked."""
    with_search = [d for d in rows if d.get("search")]
    print(
        f"\n3. alternative rankings over {len(with_search)} departures "
        f"with a recorded search (of {len(rows)})"
    )
    metrics = {
        "absolute profit (current)": lambda e, c: e["profit"],
        "calibrated profit": lambda e, c: e["profit"] * max(0.0, ratios.get(c, 0.0)),
        "profit per hold unit": lambda e, c: e["per_unit"],
        "profit per trip turn": lambda e, c: e["per_turn"],
    }
    profit_key = {
        "absolute profit (current)": lambda e: e["profit"],
        "calibrated profit": lambda e: e["profit"],
        "profit per hold unit": lambda e: e["per_unit_profit"],
        "profit per trip turn": lambda e: e["per_turn_profit"],
    }
    print(
        f"{'metric':>28} {'food wins':>10} {'changed':>9} {'loss-making pick':>17} "
        f"{'top commodity':>16}"
    )
    for name, score in metrics.items():
        food = 0
        changed = 0
        lossy = 0
        picks: Counter = Counter()
        for dep in with_search:
            best_commodity = None
            best_score = None
            best_entry = None
            for commodity, entry in dep["search"]["by_commodity"].items():
                value = score(entry, commodity)
                if best_score is None or value > best_score:
                    best_score = value
                    best_commodity = commodity
                    best_entry = entry
            if best_commodity is None:
                continue
            picks[best_commodity] += 1
            if best_commodity in FOOD_IDS:
                food += 1
            if best_commodity != dep["commodity"]:
                changed += 1
            if best_entry is not None and profit_key[name](best_entry) <= 0:
                lossy += 1
        n = max(1, len(with_search))
        top = picks.most_common(1)[0][0] if picks else "-"
        print(
            f"{name:>28} {100.0 * food / n:>9.1f}% {100.0 * changed / n:>8.1f}% "
            f"{100.0 * lossy / n:>16.1f}% {top:>16}"
        )

    has_food = sum(
        1
        for d in with_search
        if any(c in FOOD_IDS for c in d["search"]["by_commodity"])
    )
    print(
        f"departures whose search had any acceptable food plan: "
        f"{has_food}/{len(with_search)}"
    )


def print_tables(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["departures"]
    if not rows:
        print("no plan departures recorded")
        return
    print(f"\n{len(rows)} plan departures after warmup")
    total_exp = sum(d["expected_profit"] for d in rows)
    total_real = sum(_realized_profit(d) for d in rows)
    print(
        f"pooled expected profit {total_exp}, realized profit {round(total_real)}, "
        f"ratio {total_real / total_exp:.2f}"
        if total_exp
        else "no expected profit"
    )
    ratios = commodity_table(rows, "1. top commodities by plan count", 10)
    food_rows = [d for d in rows if d["commodity"] in FOOD_IDS]
    if food_rows:
        commodity_table(food_rows, "2. food plans that executed", 5)
    else:
        print("\n2. food plans that executed: none")
    reranking(rows, ratios)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--out", type=str, default="tmp/plan_claim.json")
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        warmup=args.warmup,
        workers=args.workers,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    print(f"\nwrote {out} ({len(result['departures'])} departures)")
    print_tables(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
