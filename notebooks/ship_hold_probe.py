"""What occupies ship holds.

Tier-1b in-process probe, modeled on ``notebooks/food_pairs_zero_planets.py``.
The live gate probe found planned quantity limited by free hold on 400 of 488
built plans (median quantity 36 of a 100-unit hold), while realized deliveries
are 3-6 units a trip. This probe records, per ship per sample turn, exactly
which units sit in the hold and where they came from:

* total cargo, ``free_hold()``, and ``contract_hold_units()``
* per-commodity units with cost basis, ``_unsold_turns``, ``_cargo_hops``,
  the turn and planet each FIFO lot was bought at, and how many planets the
  ship has visited since
* the active plan (``TradePlan`` or ``ContractPlan``) and its commodity and
  quantity, plus ``_plan_loaded`` / ``_plan_turns_left``
* accepted and loaded contracts with their payload kind and hold units
* the ship's own resting sell orders here, and whether each is priced at the
  ``LIQUIDATION_FLOOR`` markdown

Lots are tracked by wrapping ``Market._execute_transaction`` (buy appends a
lot, sale consumes FIFO and records the realized markdown) and visits by
wrapping ``Ship.update_journey``. Both wrappers call through unchanged and
their call counts are asserted non-zero at the end.

Run:

    PYTHONPATH=/home/timg/code/spacesim2 \
    /home/timg/code/spacesim2/.venv-ft/bin/python \
        /home/timg/code/spacesim2/notebooks/ship_hold_probe.py \
        --turns 400 --planets 50 --warmup 100 --sample-every 25 \
        --workers 6 --out /home/timg/code/spacesim2/tmp/ship_hold.json
"""

import argparse
import json
import statistics
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.core.contracts import (
    ConsignmentPayload,
    ContractStatus,
    PassengerPayload,
)
from spacesim2.core.market import Market
from spacesim2.core.ship import (
    FUEL_MILLI,
    LIQUIDATION_FLOOR,
    MAINTENANCE_KIT_IDS,
    MAX_CARGO_HOPS,
    UNSOLD_DECAY,
    ContractPlan,
    Ship,
    ShipStatus,
    TradePlan,
    TraderBrain,
)

from spacesim2.cli.common import create_and_setup_simulation  # isort: skip

FUEL_ID = "nova_fuel"

_LOCK = threading.Lock()
# ship name -> commodity id -> FIFO list of {turn, planet, qty, price}
lots: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
    lambda: defaultdict(list)
)
# ship name -> list of (turn, planet name) arrivals
visits: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
sale_rows: List[Dict[str, Any]] = []
patch_calls: Counter = Counter()


def _ship_turn(ship: Ship) -> int:
    return ship.simulation.current_turn


def _record_buy(ship: Ship, commodity_id: str, quantity: int, price: int) -> None:
    planet = ship.planet.name if ship.planet is not None else "?"
    with _LOCK:
        lots[ship.name][commodity_id].append(
            {
                "turn": _ship_turn(ship),
                "planet": planet,
                "qty": quantity,
                "price": price,
            }
        )


def _consume_lots(ship_name: str, commodity_id: str, quantity: int) -> List[Dict]:
    """Remove ``quantity`` units FIFO and return the lot fragments consumed."""
    taken: List[Dict[str, Any]] = []
    queue = lots[ship_name][commodity_id]
    remaining = quantity
    while remaining > 0 and queue:
        lot = queue[0]
        used = min(remaining, lot["qty"])
        taken.append({**lot, "qty": used})
        lot["qty"] -= used
        remaining -= used
        if lot["qty"] <= 0:
            queue.pop(0)
    return taken


def _record_sale(ship: Ship, commodity, quantity: int, price: int) -> None:
    brain = ship.brain
    turn = _ship_turn(ship)
    planet = ship.planet.name if ship.planet is not None else "?"
    basis: Optional[float] = None
    unsold = 0
    hops = 0
    floor: Optional[int] = None
    if isinstance(brain, TraderBrain):
        basis = brain.basis_per_unit(commodity)
        unsold = brain._unsold_turns.get(commodity.id, 0)
        hops = brain._cargo_hops.get(commodity.id, 0)
        floor = brain._sell_floor_price(commodity)
    with _LOCK:
        taken = _consume_lots(ship.name, commodity.id, quantity)
        ship_visits = list(visits[ship.name])
        oldest = min((lot["turn"] for lot in taken), default=None)
        origins = sorted({lot["planet"] for lot in taken})
        planets_since = 0
        if oldest is not None:
            planets_since = len({name for t, name in ship_visits if t > oldest})
        sale_rows.append(
            {
                "turn": turn,
                "ship": ship.name,
                "planet": planet,
                "commodity": commodity.id,
                "quantity": quantity,
                "price": price,
                "basis": round(basis, 3) if basis is not None else None,
                "floor": floor,
                "unsold_turns": unsold,
                "cargo_hops": hops,
                "age_turns": (turn - oldest) if oldest is not None else None,
                "buy_planets": origins,
                "sold_where_bought": origins == [planet],
                "planets_visited_since_buy": planets_since,
                "at_liquidation_floor": (
                    basis is not None and price <= int(basis * LIQUIDATION_FLOOR) + 1
                ),
                "below_basis": basis is not None and price < basis,
            }
        )


def install_patches() -> None:
    """Wrap transaction execution and journey arrival, keeping behavior."""
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey

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
            _record_sale(seller, commodity_type, quantity, price)
        return result

    def wrapped_update(self):  # type: ignore[no-untyped-def]
        patch_calls["update_journey"] += 1
        arrived = original_update(self)
        if arrived and self.planet is not None:
            with _LOCK:
                visits[self.name].append((_ship_turn(self), self.planet.name))
        return arrived

    Market._execute_transaction = wrapped_execute  # type: ignore[assignment]
    Ship.update_journey = wrapped_update  # type: ignore[assignment]


def update_docked_streaks(sim, streaks: Dict[str, Tuple[str, int]]) -> None:
    """Consecutive turns each ship has been docked at its current planet."""
    for ship in sim.ships:
        if ship.status is ShipStatus.DOCKED and ship.planet is not None:
            name, count = streaks.get(ship.name, ("", 0))
            if name == ship.planet.name:
                streaks[ship.name] = (name, count + 1)
            else:
                streaks[ship.name] = (ship.planet.name, 1)
        else:
            streaks[ship.name] = ("", 0)


def plan_row(brain: TraderBrain) -> Optional[Dict[str, Any]]:
    plan = brain._current_plan
    if plan is None:
        return None
    if isinstance(plan, TradePlan):
        return {
            "kind": "trade",
            "commodity": plan.commodity.id,
            "quantity": plan.quantity,
            "origin": plan.origin.name,
            "destination": plan.destination.name,
            "loaded": brain._plan_loaded,
            "turns_left": brain._plan_turns_left,
            "expected_profit": plan.expected_profit,
        }
    if isinstance(plan, ContractPlan):
        return {
            "kind": "contract",
            "commodity": None,
            "quantity": None,
            "origin": None,
            "destination": plan.destination.name,
            "loaded": False,
            "turns_left": brain._plan_turns_left,
            "expected_profit": plan.expected_profit,
        }
    return {"kind": type(plan).__name__}


def contract_rows(ship: Ship) -> List[Dict[str, Any]]:
    rows = []
    for contract in ship.contracts:
        if contract.status not in (ContractStatus.ACCEPTED, ContractStatus.LOADED):
            continue
        payload = contract.payload
        if isinstance(payload, PassengerPayload):
            kind = "passenger"
        elif isinstance(payload, ConsignmentPayload):
            kind = "consignment"
        else:
            kind = type(payload).__name__
        rows.append(
            {
                "kind": kind,
                "status": contract.status.value
                if hasattr(contract.status, "value")
                else str(contract.status),
                "hold_units": payload.hold_units,
                "destination": contract.destination.name,
            }
        )
    return rows


def classify(
    commodity_id: str,
    quantity: int,
    reserved_kit: int,
    plan: Optional[Dict[str, Any]],
    here: str,
    lot_planets: List[str],
    hops: int,
    unsold: int,
) -> str:
    """Which occupant category these hold units belong to."""
    if commodity_id == FUEL_ID:
        return "fuel"
    if reserved_kit >= quantity:
        return "repair_kit"
    if (
        plan is not None
        and plan["kind"] == "trade"
        and plan["commodity"] == commodity_id
    ):
        if plan["origin"] == here:
            return "plan_accumulation"
        if plan["destination"] == here:
            return "plan_at_destination"
        return "plan_cargo_in_transit"
    if hops > 0 or unsold > 0:
        return "carried_over"
    if lot_planets and any(name != here for name in lot_planets):
        return "carried_over"
    return "other"


def ship_row(ship: Ship, sim, streaks: Dict[str, Tuple[str, int]]) -> Dict[str, Any]:
    brain = ship.brain
    trader = isinstance(brain, TraderBrain)
    here = ship.planet.name if ship.planet is not None else ""
    plan = plan_row(brain) if trader else None
    turn = sim.current_turn

    listed: Dict[str, Dict[str, Any]] = {}
    if ship.planet is not None:
        for order in ship.planet.market.get_actor_orders(ship)["sell"]:
            entry = listed.setdefault(
                order.commodity_type.id, {"quantity": 0, "prices": []}
            )
            entry["quantity"] += order.quantity
            entry["prices"].append(order.price)

    cargo_rows: List[Dict[str, Any]] = []
    by_category: Counter = Counter()
    for commodity in sim.commodity_registry.all_commodities():
        quantity = ship.cargo.get_quantity(commodity)
        if quantity <= 0:
            continue
        hops = brain._cargo_hops.get(commodity.id, 0) if trader else 0
        unsold = brain._unsold_turns.get(commodity.id, 0) if trader else 0
        basis = brain.basis_per_unit(commodity) if trader else None
        reserved_kit = 0
        if trader and commodity.id in MAINTENANCE_KIT_IDS:
            kit = brain._repair_kit_tier()
            if kit is not None and kit[0] == commodity.id:
                reserved_kit = kit[1]
        with _LOCK:
            queue = [
                dict(lot) for lot in lots[ship.name][commodity.id] if lot["qty"] > 0
            ]
            ship_visits = list(visits[ship.name])
        oldest = min((lot["turn"] for lot in queue), default=None)
        lot_planets = sorted({lot["planet"] for lot in queue})
        planets_since = (
            len({name for t, name in ship_visits if t > oldest})
            if oldest is not None
            else 0
        )
        category = classify(
            commodity.id,
            quantity,
            reserved_kit,
            plan,
            here,
            lot_planets,
            hops,
            unsold,
        )
        by_category[category] += quantity
        ask = listed.get(commodity.id)
        markdown_floor = (
            int(basis * max(LIQUIDATION_FLOOR, 1.0 - UNSOLD_DECAY * unsold))
            if basis is not None
            else None
        )
        cargo_rows.append(
            {
                "commodity": commodity.id,
                "quantity": quantity,
                "category": category,
                "basis": round(basis, 3) if basis is not None else None,
                "unsold_turns": unsold,
                "cargo_hops": hops,
                "reserved_kit": reserved_kit,
                "age_turns": (turn - oldest) if oldest is not None else None,
                "buy_planets": lot_planets,
                "bought_elsewhere": bool(
                    lot_planets and any(name != here for name in lot_planets)
                ),
                "planets_visited_since_buy": planets_since,
                "listed_here": ask is not None,
                "listed_quantity": ask["quantity"] if ask else 0,
                "listed_min_price": min(ask["prices"]) if ask else None,
                "markdown_floor": markdown_floor,
                "listed_at_floor": (
                    ask is not None
                    and markdown_floor is not None
                    and min(ask["prices"]) <= markdown_floor + 1
                ),
            }
        )

    docked_planet, docked_turns = streaks.get(ship.name, ("", 0))
    return {
        "turn": turn,
        "ship": ship.name,
        "status": ship.status.name,
        "planet": here,
        "destination": (
            ship.destination.name
            if getattr(ship, "destination", None) is not None
            else None
        ),
        "money": ship.money,
        "cargo_capacity": ship.cargo_capacity,
        "cargo_total": ship.cargo.get_total_quantity(),
        "free_hold": ship.free_hold(),
        "contract_hold_units": ship.contract_hold_units(),
        "fuel_units": round(ship.fuel / FUEL_MILLI, 2),
        "fuel_capacity_units": round(ship.fuel_capacity / FUEL_MILLI, 2),
        "docked_turns_here": docked_turns if docked_planet == here else 0,
        "is_trader": trader,
        "plan": plan,
        "contracts": contract_rows(ship),
        "cargo": cargo_rows,
        "by_category": dict(by_category),
    }


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    sample_every: int,
    warmup: int,
    workers: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    if workers > 1:
        sim.parallel_workers = workers

    streaks: Dict[str, Tuple[str, int]] = {}
    ship_rows: List[Dict[str, Any]] = []
    for _ in range(turns):
        sim.run_turn()
        update_docked_streaks(sim, streaks)
        if sim.current_turn > warmup and sim.current_turn % sample_every == 0:
            rows = [ship_row(ship, sim, streaks) for ship in sim.ships]
            ship_rows.extend(rows)
            occ = Counter()
            for row in rows:
                occ.update(row["by_category"])
            print(
                f"turn {sim.current_turn}: {len(rows)} ships, "
                f"median cargo {statistics.median([r['cargo_total'] for r in rows])}, "
                f"median free hold "
                f"{statistics.median([r['free_hold'] for r in rows])}, "
                f"units by category {dict(occ.most_common())}",
                flush=True,
            )

    if patch_calls["update_journey"] == 0 or patch_calls["ship_buy"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "workers": workers,
            "ships": len(sim.ships),
            "max_cargo_hops": MAX_CARGO_HOPS,
            "liquidation_floor": LIQUIDATION_FLOOR,
        },
        "ship_rows": ship_rows,
        "sale_rows": sale_rows,
        "patch_calls": dict(patch_calls),
    }


def _median(values: List[Any]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 2) if clean else None


def _pct(values: List[Any], q: float) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    index = min(len(clean) - 1, int(q * len(clean)))
    return round(clean[index], 2)


def print_table(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["ship_rows"]
    sales: List[Dict[str, Any]] = result["sale_rows"]
    if not rows:
        print("no ship samples")
        return
    capacity = rows[0]["cargo_capacity"]
    print(f"\n{len(rows)} ship-samples, hold capacity {capacity}")

    print("\n1. hold occupancy (all ship-samples)")
    for label, key in (
        ("cargo_total", "cargo_total"),
        ("free_hold", "free_hold"),
        ("contract_hold_units", "contract_hold_units"),
    ):
        values = [r[key] for r in rows]
        print(
            f"{label:>20} median={_median(values):>7} p10={_pct(values, 0.1):>7} "
            f"p90={_pct(values, 0.9):>7} mean={round(statistics.mean(values), 2):>7} "
            f"zero={sum(1 for v in values if v == 0)}/{len(values)}"
        )

    docked = [r for r in rows if r["status"] == "DOCKED"]
    transit = [r for r in rows if r["status"] != "DOCKED"]
    print(
        f"docked {len(docked)} (median cargo {_median([r['cargo_total'] for r in docked])}, "
        f"free {_median([r['free_hold'] for r in docked])}); "
        f"in transit {len(transit)} (median cargo "
        f"{_median([r['cargo_total'] for r in transit])})"
    )

    occ: Counter = Counter()
    for row in rows:
        occ.update(row["by_category"])
    total_units = sum(occ.values())
    contract_units = sum(r["contract_hold_units"] for r in rows)
    grand = total_units + contract_units
    print(f"\noccupied hold units, pooled over samples (total {grand})")
    print(f"{'occupant':>22} {'units':>9} {'share':>7} {'per ship-sample':>16}")
    for name, units in occ.most_common():
        print(
            f"{name:>22} {units:>9} {100.0 * units / grand:>6.1f}% "
            f"{units / len(rows):>16.2f}"
        )
    print(
        f"{'contract_reservation':>22} {contract_units:>9} "
        f"{100.0 * contract_units / grand:>6.1f}% {contract_units / len(rows):>16.2f}"
    )

    print("\n2. carried-over cargo")
    carried = [c for r in rows for c in r["cargo"] if c["category"] == "carried_over"]
    if carried:
        by_commodity: Counter = Counter()
        for c in carried:
            by_commodity[c["commodity"]] += c["quantity"]
        print(f"  {len(carried)} lots, {sum(c['quantity'] for c in carried)} units")
        print(f"  top commodities: {dict(by_commodity.most_common(8))}")
        print(
            "  age turns median={m} p90={p}; planets visited since buy median={pv} "
            "p90={pv9}; hops>0 {h}/{n}; unsold_turns median={u} p90={u9}".format(
                m=_median([c["age_turns"] for c in carried]),
                p=_pct([c["age_turns"] for c in carried], 0.9),
                pv=_median([c["planets_visited_since_buy"] for c in carried]),
                pv9=_pct([c["planets_visited_since_buy"] for c in carried], 0.9),
                h=sum(1 for c in carried if c["cargo_hops"] > 0),
                n=len(carried),
                u=_median([c["unsold_turns"] for c in carried]),
                u9=_pct([c["unsold_turns"] for c in carried], 0.9),
            )
        )
        print(
            "  listed here {ln}/{n}; listed at markdown floor {lf}/{n}".format(
                ln=sum(1 for c in carried if c["listed_here"]),
                n=len(carried),
                lf=sum(1 for c in carried if c["listed_at_floor"]),
            )
        )
    else:
        print("  none")

    print("\n  every cargo lot by category, age and listing")
    print(
        f"{'category':>22} {'lots':>6} {'units':>7} {'age_med':>8} {'age_p90':>8} "
        f"{'listed':>7} {'at_floor':>9}"
    )
    lots_by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        for c in r["cargo"]:
            lots_by_cat[c["category"]].append(c)
    for name, group in sorted(
        lots_by_cat.items(), key=lambda kv: -sum(c["quantity"] for c in kv[1])
    ):
        print(
            f"{name:>22} {len(group):>6} {sum(c['quantity'] for c in group):>7} "
            f"{str(_median([c['age_turns'] for c in group])):>8} "
            f"{str(_pct([c['age_turns'] for c in group], 0.9)):>8} "
            f"{sum(1 for c in group if c['listed_here']):>7} "
            f"{sum(1 for c in group if c['listed_at_floor']):>9}"
        )

    print("\n3. realized sales by ships")
    if sales:
        stale = [s for s in sales if s["cargo_hops"] > 0 or s["unsold_turns"] > 0]
        print(
            f"  {len(sales)} fills, {sum(s['quantity'] for s in sales)} units; "
            f"median qty {_median([s['quantity'] for s in sales])}"
        )
        print(
            "  age at sale median={a} p90={a9}; sold where bought {sw}/{n}; "
            "planets since buy median={p}".format(
                a=_median([s["age_turns"] for s in sales]),
                a9=_pct([s["age_turns"] for s in sales], 0.9),
                sw=sum(1 for s in sales if s["sold_where_bought"]),
                n=len(sales),
                p=_median([s["planets_visited_since_buy"] for s in sales]),
            )
        )
        print(
            "  at liquidation floor {lf}/{n}; below basis {bb}/{n}; "
            "stale (hops or unsold) {st}/{n}".format(
                lf=sum(1 for s in sales if s["at_liquidation_floor"]),
                bb=sum(1 for s in sales if s["below_basis"]),
                st=len(stale),
                n=len(sales),
            )
        )
        by_commodity = Counter()
        for s in sales:
            by_commodity[s["commodity"]] += s["quantity"]
        print(f"  units sold by commodity: {dict(by_commodity.most_common(8))}")
    else:
        print("  no ship sales recorded")

    print("\n4. ship sell orders resting at the current planet")
    listed_lots = [c for r in rows for c in r["cargo"] if c["listed_here"]]
    if listed_lots:
        cats = Counter(c["category"] for c in listed_lots)
        print(
            f"  {len(listed_lots)} listed lots; by category {dict(cats.most_common())}"
        )
        print(
            "  median age {a}, median unsold_turns {u}, at floor {f}/{n}".format(
                a=_median([c["age_turns"] for c in listed_lots]),
                u=_median([c["unsold_turns"] for c in listed_lots]),
                f=sum(1 for c in listed_lots if c["listed_at_floor"]),
                n=len(listed_lots),
            )
        )
    else:
        print("  none")

    planned = [r for r in rows if r["plan"] is not None]
    trade_plans = [r for r in planned if r["plan"]["kind"] == "trade"]
    print(
        f"\nplans: {len(planned)}/{len(rows)} ship-samples hold one "
        f"({len(trade_plans)} trade); median plan quantity "
        f"{_median([r['plan']['quantity'] for r in trade_plans])}"
    )
    with_contracts = [r for r in rows if r["contract_hold_units"] > 0]
    kinds = Counter(c["kind"] for r in rows for c in r["contracts"])
    print(
        f"contracts: {len(with_contracts)}/{len(rows)} ship-samples reserve hold; "
        f"payload kinds {dict(kinds.most_common())}"
    )
    print("patch calls:", result["patch_calls"])


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--sample-every", type=int, default=25)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        sample_every=args.sample_every,
        warmup=args.warmup,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
