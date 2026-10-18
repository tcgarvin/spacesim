"""Why processed_food surpluses never leave the plant planets.

Tier-1b in-process probe. For every planet holding more than
``--stock-threshold`` units of ``processed_food`` across its actors, it runs
the ship planner's own evaluation (``TraderBrain._origin_acquisition``,
``_pair_economics``, ``_evaluate_trade_opportunity``, ``_plan_acceptable``)
from that planet for ``processed_food`` and for ``food``, and records the
first gate that blocks a plan, in the order ``_best_plan_from`` applies them.

Run:

    uv run python notebooks/processed_food_export_probe.py \
        --turns 100 --planets 12 --out tmp/pf_smoke.json
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.commodity import CommodityDefinition
from spacesim2.core.market import Market
from spacesim2.core.navigation import Navigator, get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.ship import Ship, ShipStatus, TradePlan, TraderBrain
from spacesim2.core.simulation import Simulation

STAPLE_ID = "processed_food"
HAND_FOOD_ID = "food"
DEFAULT_STOCK_THRESHOLD = 300

# Counters filled by the monkeypatches; module level so the wrappers stay
# plain functions.
ship_purchases: Counter[str] = Counter()
ship_sales: Counter[str] = Counter()
arrivals: Counter[str] = Counter()
patch_calls = Counter()


def install_patches() -> None:
    """Wrap transaction execution and journey arrival, keeping behavior."""
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey

    def wrapped_execute(
        self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
    ):  # type: ignore[no-untyped-def]
        patch_calls["execute"] += 1
        result = original_execute(
            self,
            buyer,
            seller,
            commodity_type,
            quantity,
            price,
            buy_order,
            sell_order,
        )
        if isinstance(buyer, Ship):
            ship_purchases[commodity_type.id] += quantity
        if isinstance(seller, Ship):
            ship_sales[commodity_type.id] += quantity
        return result

    def wrapped_update(self):  # type: ignore[no-untyped-def]
        patch_calls["update_journey"] += 1
        arrived = original_update(self)
        if arrived and self.planet is not None:
            arrivals[self.planet.name] += 1
        return arrived

    Market._execute_transaction = wrapped_execute  # type: ignore[assignment]
    Ship.update_journey = wrapped_update  # type: ignore[assignment]


def _book(market: Market, commodity: CommodityDefinition) -> Dict[str, Any]:
    best_bid, best_ask = market.get_bid_ask_spread(commodity)
    bid_levels = market.get_bid_levels(commodity)
    ask_levels = market.get_ask_levels(commodity)
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth": sum(q for _, q in bid_levels),
        "ask_depth": sum(q for _, q in ask_levels),
        "avg_price": market.get_avg_price(commodity),
        "has_signal": market.has_price_signal(commodity),
        "flow_30d": market.get_30_day_average_volume(commodity),
    }


def evaluate_export(
    brain: TraderBrain,
    nav: Navigator,
    origin: Planet,
    commodity: CommodityDefinition,
) -> Dict[str, Any]:
    """Run the planner's gates for exporting ``commodity`` from ``origin``.

    Gates are applied in the order ``TraderBrain._best_plan_from`` applies
    them, so the result lands in exactly one bucket.
    """
    out: Dict[str, Any] = {
        "gate": "",
        "n_destinations": 0,
        "n_pair_ok": 0,
        "n_plans": 0,
        "best_dest": None,
        "best_margin": None,
        "best_profit": None,
        "best_quantity": None,
        "purchase_price": None,
        "sell_price": None,
        "entry_price": None,
        "bid_price": None,
        "dest_best_bid": None,
        "dest_bid_depth": 0,
    }
    if not nav.has_any_trade_signal():
        out["gate"] = "no_trade_signal"
        return out
    if commodity not in nav.exportable_commodities(origin):
        out["gate"] = "not_exportable_here"
        return out
    acquisition = brain._origin_acquisition(origin, commodity)
    if acquisition is None:
        out["gate"] = "no_acquisition"
        return out
    out["entry_price"] = acquisition.entry_price
    out["bid_price"] = acquisition.bid_price

    destinations = nav.candidate_destinations(origin, commodity)
    out["n_destinations"] = len(destinations)
    if not destinations:
        out["gate"] = "no_demand_planet"
        return out

    best_plan: Optional[TradePlan] = None
    best_margin_plan: Optional[TradePlan] = None
    pair_ok = 0
    plans = 0
    dest_best_bid = 0
    dest_bid_depth = 0
    for destination in destinations:
        bid, _ = destination.market.get_bid_ask_spread(commodity)
        if bid is not None:
            dest_best_bid = max(dest_best_bid, bid)
        # Units bid for at the destination, the depth a ship could sell into.
        dest_bid_depth += sum(
            q for _, q in destination.market.get_bid_levels(commodity)
        )
        pair = brain._pair_economics(origin, destination)
        if pair is None:
            continue
        pair_ok += 1
        plan = brain._evaluate_trade_opportunity(
            origin=origin,
            destination=destination,
            commodity=commodity,
            pair=pair,
            acquisition=acquisition,
        )
        if plan is None:
            continue
        plans += 1
        if best_plan is None or plan.expected_profit > best_plan.expected_profit:
            best_plan = plan
        if (
            best_margin_plan is None
            or plan.profit_margin > best_margin_plan.profit_margin
        ):
            best_margin_plan = plan

    out["n_pair_ok"] = pair_ok
    out["n_plans"] = plans
    out["dest_best_bid"] = dest_best_bid or None
    out["dest_bid_depth"] = dest_bid_depth
    if pair_ok == 0:
        out["gate"] = "pair_infeasible_fuel_or_cash"
        return out
    if best_plan is None:
        out["gate"] = "no_dest_demand_or_budget"
        return out

    reported = best_plan
    out["best_dest"] = reported.destination.name
    out["best_margin"] = round(reported.profit_margin, 4)
    out["best_profit"] = reported.expected_profit
    out["best_quantity"] = reported.quantity
    out["purchase_price"] = reported.purchase_price_per_unit
    out["sell_price"] = reported.expected_sell_price_per_unit
    if best_margin_plan is not None:
        out["best_margin_any"] = round(best_margin_plan.profit_margin, 4)
    out["gate"] = (
        "plan_acceptable" if brain._plan_acceptable(best_plan) else "margin_below_min"
    )
    return out


def pick_evaluator(sim: Simulation) -> Optional[Ship]:
    """A representative docked ship: median money among docked ships."""
    docked = [s for s in sim.ships if s.status is ShipStatus.DOCKED]
    pool = docked if docked else list(sim.ships)
    if not pool:
        return None
    pool.sort(key=lambda s: s.money)
    return pool[len(pool) // 2]


def surplus_planets(
    sim: Simulation, staple: CommodityDefinition, threshold: int
) -> List[Tuple[Planet, int]]:
    rows: List[Tuple[Planet, int]] = []
    for planet in sim.planets:
        stock = sum(
            actor.inventory.get_quantity(staple)
            for actor in sim.actors
            if actor.planet is planet
        )
        if stock >= threshold:
            rows.append((planet, stock))
    return rows


def sample(
    sim: Simulation,
    staple: CommodityDefinition,
    hand_food: CommodityDefinition,
    threshold: int,
) -> List[Dict[str, Any]]:
    ship = pick_evaluator(sim)
    if ship is None:
        return []
    brain = ship.brain
    if not isinstance(brain, TraderBrain):
        return []
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    nav = get_navigator(sim)
    rows: List[Dict[str, Any]] = []
    for planet, stock in surplus_planets(sim, staple, threshold):
        # A second arm evaluated by a ship actually docked here, which has
        # this planet's real fuel and cash situation. None when no ship is
        # docked, which is itself part of the answer.
        local_ships = [
            s
            for s in planet.ships
            if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
        ]
        local = max(local_ships, key=lambda s: s.money) if local_ships else None
        food_healths = [
            drive.metrics.health
            for actor in sim.actors
            if actor.planet is planet
            for drive in actor.drives
            if drive.WELLBEING and drive.metrics.get_name() == "food"
        ]
        rows.append(
            {
                "turn": sim.current_turn,
                "planet": planet.name,
                "stock": stock,
                "food_health_mean": (
                    round(statistics.mean(food_healths), 3) if food_healths else None
                ),
                "ships_docked": len(planet.ships),
                "evaluator_money": ship.money,
                "evaluator_fuel": (
                    ship.cargo.get_quantity(fuel) if fuel is not None else None
                ),
                "local_evaluator_money": local.money if local else None,
                "book_staple": _book(planet.market, staple),
                "book_food": _book(planet.market, hand_food),
                "export_staple": evaluate_export(brain, nav, planet, staple),
                "export_food": evaluate_export(brain, nav, planet, hand_food),
                "export_staple_local": (
                    evaluate_export(local.brain, nav, planet, staple) if local else None
                ),
                "export_food_local": (
                    evaluate_export(local.brain, nav, planet, hand_food)
                    if local
                    else None
                ),
            }
        )
    return rows


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    sample_every: int,
    warmup: int,
    threshold: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(planets=planets, actors=actors, makers=2)
    staple = sim.commodity_registry.get_commodity(STAPLE_ID)
    hand_food = sim.commodity_registry.get_commodity(HAND_FOOD_ID)
    if staple is None or hand_food is None:
        raise ValueError("food commodities missing from the registry")

    rows: List[Dict[str, Any]] = []
    for _ in range(min(warmup, turns)):
        sim.run_turn()
    for _ in range(max(0, turns - warmup)):
        sim.run_turn()
        if sim.current_turn % sample_every == 0:
            turn_rows = sample(sim, staple, hand_food, threshold)
            rows.extend(turn_rows)
            gates = Counter(r["export_staple"]["gate"] for r in turn_rows)
            print(
                f"turn {sim.current_turn}: {len(turn_rows)} surplus planets, "
                f"staple gates {dict(gates)}",
                flush=True,
            )

    if patch_calls["execute"] == 0 or patch_calls["update_journey"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    surplus_names = {r["planet"] for r in rows}
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "stock_threshold": threshold,
            "ships": len(sim.ships),
        },
        "rows": rows,
        "ship_purchases": dict(ship_purchases.most_common()),
        "ship_sales": dict(ship_sales.most_common()),
        "arrivals_total": sum(arrivals.values()),
        "arrivals_at_surplus_planets": sum(arrivals[name] for name in surplus_names),
        "surplus_planets_ever": sorted(surplus_names),
        "surplus_planets_visited": sorted(
            name for name in surplus_names if arrivals[name] > 0
        ),
        "patch_calls": dict(patch_calls),
    }


def _median(values: List[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def print_table(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["rows"]
    print(f"\n{len(rows)} planet-observations over surplus planets")
    if not rows:
        print("no surplus planets observed")
        return
    for label, key in (("processed_food", "export_staple"), ("food", "export_food")):
        gates = Counter(r[key]["gate"] for r in rows)
        total = sum(gates.values())
        print(f"\n{label}: first blocking gate")
        print(f"{'gate':>32} {'count':>6} {'share':>6}")
        for gate, count in gates.most_common():
            print(f"{gate:>32} {count:>6} {100.0 * count / total:>5.0f}%")
        print(
            "  median margin={m} purchase={p} sell={s} dest_best_bid={b} "
            "dest_bid_depth={bd} entry={e} quantity={q}".format(
                m=_median([r[key]["best_margin"] for r in rows]),
                p=_median([r[key]["purchase_price"] for r in rows]),
                s=_median([r[key]["sell_price"] for r in rows]),
                b=_median([r[key]["dest_best_bid"] for r in rows]),
                bd=_median([r[key].get("dest_bid_depth") for r in rows]),
                e=_median([r[key]["entry_price"] for r in rows]),
                q=_median([r[key]["best_quantity"] for r in rows]),
            )
        )
        print(
            "  median n_dest={d} n_pair_ok={po} n_plans={np}".format(
                d=_median([r[key]["n_destinations"] for r in rows]),
                po=_median([r[key]["n_pair_ok"] for r in rows]),
                np=_median([r[key]["n_plans"] for r in rows]),
            )
        )
    local_rows = [r for r in rows if r.get("export_staple_local")]
    print(
        f"\nlocal-ship arm: {len(local_rows)}/{len(rows)} observations had a "
        "trader docked at the surplus planet"
    )
    for label, key in (
        ("processed_food", "export_staple_local"),
        ("food", "export_food_local"),
    ):
        gates = Counter(r[key]["gate"] for r in local_rows)
        total = sum(gates.values())
        if not total:
            continue
        print(
            f"  {label}: "
            + ", ".join(f"{gate}={count}" for gate, count in gates.most_common())
        )
        print(
            "    median margin={m} purchase={p} sell={s}".format(
                m=_median([r[key]["best_margin"] for r in local_rows]),
                p=_median([r[key]["purchase_price"] for r in local_rows]),
                s=_median([r[key]["sell_price"] for r in local_rows]),
            )
        )
    print("\nlocal books at surplus planets (medians)")
    print(
        "  processed_food ask={a} bid={b} ask_depth={ad} bid_depth={bd} stock={s}".format(
            a=_median([r["book_staple"]["best_ask"] for r in rows]),
            b=_median([r["book_staple"]["best_bid"] for r in rows]),
            ad=_median([r["book_staple"]["ask_depth"] for r in rows]),
            bd=_median([r["book_staple"]["bid_depth"] for r in rows]),
            s=_median([r["stock"] for r in rows]),
        )
    )
    print(
        "  food          ask={a} bid={b} ask_depth={ad} bid_depth={bd}".format(
            a=_median([r["book_food"]["best_ask"] for r in rows]),
            b=_median([r["book_food"]["best_bid"] for r in rows]),
            ad=_median([r["book_food"]["ask_depth"] for r in rows]),
            bd=_median([r["book_food"]["bid_depth"] for r in rows]),
        )
    )
    print(
        "  food drive health median={h}".format(
            h=_median([r["food_health_mean"] for r in rows])
        )
    )
    print("\nship purchases (units, whole run):", result["ship_purchases"])
    print("ship sales (units, whole run):", result["ship_sales"])
    print(
        "arrivals total={t} at surplus planets={s}; surplus planets {v}/{n} visited".format(
            t=result["arrivals_total"],
            s=result["arrivals_at_surplus_planets"],
            v=len(result["surplus_planets_visited"]),
            n=len(result["surplus_planets_ever"]),
        )
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=450)
    parser.add_argument("--planets", type=int, default=100)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=150)
    parser.add_argument("--stock-threshold", type=int, default=DEFAULT_STOCK_THRESHOLD)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        sample_every=args.sample_every,
        warmup=args.warmup,
        threshold=args.stock_threshold,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
