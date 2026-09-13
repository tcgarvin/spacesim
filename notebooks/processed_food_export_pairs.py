"""Per-destination trace of why processed_food never leaves the plant planets.

Tier-1b in-process probe, derived from ``notebooks/processed_food_export_probe.py``.
The parent probe aggregated across destinations and only reported the first
gate that blocked *any* plan. This one keeps one row per sampled
``(origin, destination)`` pair for ``processed_food``, so a destination with a
deep bid book can be followed all the way to the gate that rejects it.

Per pair it records the destination book, the planner's flow value and flow
quantity, whether the destination is in ``Navigator.candidate_destinations``,
which of the three ``TraderBrain._pair_economics`` exits it takes (with the
fuel and cash values behind that exit), and, when the pair passes, the plan
the planner builds and whether ``_plan_acceptable`` takes it.

``_pair_economics`` is re-implemented here rather than edited in ship.py; the
re-implementation's verdict is cross-checked against the real method on every
pair and any disagreement is counted in ``pair_trace_mismatches``.

Per sampled origin it also records the plan ``_best_plan_from`` actually
returns across *all* commodities, so what beats the staple is visible.

Run:

    uv run --project /home/timg/code/spacesim2 python pf_probe_dest.py \
        --turns 400 --planets 100 --warmup 150 --sample-every 50 \
        --out tmp/pf_probe_dest.json
"""

import argparse
import json
import math
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
from spacesim2.core.ship import (
    DEMAND_HORIZON_TURNS,
    MAINTENANCE_CHANCE,
    MAINTENANCE_FUEL_UNITS,
    SELL_PRICE_HAIRCUT,
    SPECULATIVE_PLAN_CAP,
    Ship,
    ShipStatus,
    TradePlan,
    TraderBrain,
    milli_to_units_ceil,
    units_to_milli,
)
from spacesim2.core.simulation import Simulation

STAPLE_ID = "processed_food"
DEFAULT_STOCK_THRESHOLD = 300

ship_purchases: Counter[str] = Counter()
ship_sales: Counter[str] = Counter()
arrivals: Counter[str] = Counter()
patch_calls: Counter[str] = Counter()
pair_trace_mismatches: Counter[str] = Counter()


def install_patches() -> None:
    """Wrap transaction execution and journey arrival, keeping behavior."""
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey

    def wrapped_execute(
        self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
    ):  # type: ignore[no-untyped-def]
        patch_calls["execute"] += 1
        result = original_execute(
            self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
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


def trace_pair_economics(
    brain: TraderBrain, origin: Planet, destination: Planet
) -> Dict[str, Any]:
    """Re-implementation of ``TraderBrain._pair_economics`` that reports its exit.

    Mirrors spacesim2/core/ship.py:1029-1128 statement for statement. Returns
    the exit label ("no_fuel_commodity", "fuel_not_buyable_at_origin",
    "destination_not_fuel_safe", "no_money_for_trading", "ok") plus every
    value the exits turn on.
    """
    out: Dict[str, Any] = {"exit": "ok"}
    ship = brain.ship
    fuel_commodity = brain._fuel_commodity()
    if fuel_commodity is None:
        out["exit"] = "no_fuel_commodity"
        return out

    distance = brain._nav.distance(origin, destination)
    fuel_one_way = ship.fuel_required(distance)
    fuel_round_trip = fuel_one_way * 2

    origin_market = origin.market
    _, fuel_ask = origin_market.get_bid_ask_spread(fuel_commodity)
    fuel_price = (
        fuel_ask
        if fuel_ask is not None
        else origin_market.get_avg_price(fuel_commodity)
    )
    if fuel_price is None or fuel_price <= 0:
        fuel_price = 10
    reference = brain._fuel_value_reference()
    fuel_reference_price = math.ceil(reference) if reference else fuel_price

    current_fuel = ship.fuel
    cargo_space = ship.cargo_capacity - ship.cargo.get_total_quantity()
    round_trip_shortfall = max(0, fuel_round_trip - current_fuel)
    fuel_purchasable = brain._fuel_purchasable_at(origin)

    out.update(
        {
            "distance": round(distance, 3),
            # Market units, so these compare with fuel_price/money below.
            "fuel_one_way": milli_to_units_ceil(fuel_one_way),
            "fuel_round_trip": milli_to_units_ceil(fuel_round_trip),
            "fuel_price_at_origin": fuel_price,
            "fuel_ask_at_origin": fuel_ask,
            "fuel_reference_price": fuel_reference_price,
            "ship_money": ship.money,
            "ship_fuel": milli_to_units_ceil(current_fuel),
            "ship_fuel_capacity": milli_to_units_ceil(ship.fuel_capacity),
            "cargo_space": cargo_space,
            "round_trip_shortfall": milli_to_units_ceil(round_trip_shortfall),
            "fuel_purchasable_at_origin": fuel_purchasable,
            "ship_is_distressed": brain.is_distressed,
        }
    )

    if round_trip_shortfall > 0 and not fuel_purchasable:
        out["exit"] = "fuel_not_buyable_at_origin"
        return out

    # fuel_to_buy is market units (what the ship actually orders); the rest
    # of this function's arithmetic stays in millifuel to match core.
    fuel_to_buy = milli_to_units_ceil(round_trip_shortfall)
    fuel_cost = fuel_to_buy * fuel_price
    fuel_after_arrival = current_fuel + units_to_milli(fuel_to_buy) - fuel_one_way
    arrival_requirement = brain._arrival_fuel_requirement(destination, origin)
    out.update(
        {
            "fuel_to_buy": fuel_to_buy,
            "fuel_cost": fuel_cost,
            "fuel_after_arrival": milli_to_units_ceil(fuel_after_arrival),
            "arrival_fuel_requirement": milli_to_units_ceil(arrival_requirement),
            "fuel_purchasable_at_destination": brain._fuel_purchasable_at(destination),
        }
    )
    if not brain._fuel_safe_destination(destination, origin, fuel_after_arrival):
        out["exit"] = "destination_not_fuel_safe"
        return out

    maintenance_cost = math.ceil(
        2 * MAINTENANCE_CHANCE * MAINTENANCE_FUEL_UNITS * fuel_price
    )
    fuel_left_after_trip = max(current_fuel, fuel_round_trip) - fuel_round_trip
    reserve_need = brain._fuel_reserve_need()
    refuel_shortfall = max(0, reserve_need - fuel_left_after_trip)
    refuel_shortfall_units = milli_to_units_ceil(refuel_shortfall)
    refuel_floor = refuel_shortfall_units * fuel_price
    money_for_trading = int(
        (ship.money - fuel_cost - refuel_floor - maintenance_cost) * 0.9
    )
    out.update(
        {
            "maintenance_cost": maintenance_cost,
            "fuel_reserve_need": milli_to_units_ceil(reserve_need),
            "fuel_left_after_trip": milli_to_units_ceil(fuel_left_after_trip),
            "refuel_shortfall": refuel_shortfall_units,
            "refuel_floor": refuel_floor,
            "money_for_trading": money_for_trading,
            "max_by_cargo": cargo_space,
        }
    )
    if money_for_trading <= 0:
        out["exit"] = "no_money_for_trading"
    return out


def destination_demand(
    brain: TraderBrain,
    destination: Planet,
    commodity: CommodityDefinition,
    entry_price: int,
) -> Dict[str, Any]:
    """Destination-side demand exactly as ``_evaluate_trade_opportunity`` sees it.

    Mirrors spacesim2/core/ship.py:1238-1266.
    """
    market = destination.market
    all_bid_levels = market.get_bid_levels(commodity)
    qualifying = [(p, q) for p, q in all_bid_levels if p > entry_price]
    depth = sum(q for _, q in qualifying)
    flow_value = brain._flow_value(market, commodity)
    flow_px = int(flow_value * SELL_PRICE_HAIRCUT) if flow_value else 0
    flow_per_turn = brain._recent_flow_per_turn(market, commodity)
    flow_qty = 0
    if flow_px > entry_price:
        flow_qty = int(flow_per_turn * DEMAND_HORIZON_TURNS)
    if depth + flow_qty > 0:
        sellable = depth + flow_qty
        sellable_source = "book_plus_flow"
    elif flow_px > entry_price:
        sellable = SPECULATIVE_PLAN_CAP
        sellable_source = "speculative_cap"
    else:
        sellable = 0
        sellable_source = "none"
    best_bid, best_ask = market.get_bid_ask_spread(commodity)
    return {
        "dest_best_bid": best_bid,
        "dest_best_ask": best_ask,
        "dest_bid_depth_total": sum(q for _, q in all_bid_levels),
        "dest_bid_depth_above_entry": depth,
        "dest_bid_levels_above_entry": qualifying[:5],
        "dest_has_price_signal": market.has_price_signal(commodity),
        "dest_avg_price": market.get_avg_price(commodity),
        "dest_flow_value": flow_value,
        "dest_flow_px_haircut": flow_px,
        "dest_flow_per_turn": round(flow_per_turn, 3),
        "dest_flow_qty": flow_qty,
        "sellable": sellable,
        "sellable_source": sellable_source,
    }


def sample_origin(
    brain: TraderBrain,
    nav: Navigator,
    origin: Planet,
    commodity: CommodityDefinition,
    sim: Simulation,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rows for every sampled destination from ``origin``, plus the origin summary."""
    summary: Dict[str, Any] = {
        "turn": sim.current_turn,
        "origin": origin.name,
        "has_any_trade_signal": nav.has_any_trade_signal(),
        "exportable_here": commodity in nav.exportable_commodities(origin),
        "entry_price": None,
        "bid_price": None,
        "flow_price": None,
        "n_candidate_destinations": 0,
        "best_plan_any_commodity": None,
    }
    # What the planner actually returns from here, across every commodity.
    overall = brain._best_plan_from(origin)
    if overall is not None:
        summary["best_plan_any_commodity"] = {
            "commodity": overall.commodity.id,
            "destination": overall.destination.name,
            "expected_profit": overall.expected_profit,
            "quantity": overall.quantity,
            "margin": round(overall.profit_margin, 4),
            "purchase_price": overall.purchase_price_per_unit,
            "sell_price": overall.expected_sell_price_per_unit,
        }
    if not summary["has_any_trade_signal"] or not summary["exportable_here"]:
        return [], summary

    acquisition = brain._origin_acquisition(origin, commodity)
    if acquisition is None:
        summary["acquisition"] = "none"
        return [], summary
    summary["entry_price"] = acquisition.entry_price
    summary["bid_price"] = acquisition.bid_price
    summary["flow_price"] = acquisition.flow_price

    candidates = nav.candidate_destinations(origin, commodity)
    candidate_set = set(candidates)
    summary["n_candidate_destinations"] = len(candidates)

    # Sample every candidate destination plus every other planet that holds a
    # resting bid for the commodity: the deep-bid planets the aggregate probe
    # could not distinguish from planets the planner never looked at.
    sampled = list(candidates)
    for planet in sim.planets:
        if planet is origin or planet in candidate_set:
            continue
        if any(q for _, q in planet.market.get_bid_levels(commodity)):
            sampled.append(planet)

    rows: List[Dict[str, Any]] = []
    for destination in sampled:
        route = nav.route(origin, destination)
        row: Dict[str, Any] = {
            "turn": sim.current_turn,
            "origin": origin.name,
            "destination": destination.name,
            "commodity": commodity.id,
            "lane_hops": max(0, len(route) - 1),
            "route_distance": round(nav.distance(origin, destination), 3),
            "in_candidate_destinations": destination in candidate_set,
            "entry_price": acquisition.entry_price,
            "bid_price": acquisition.bid_price,
        }
        row.update(
            destination_demand(brain, destination, commodity, acquisition.entry_price)
        )
        trace = trace_pair_economics(brain, origin, destination)
        row["pair"] = trace

        real_pair = brain._pair_economics(origin, destination)
        if (real_pair is None) != (trace["exit"] != "ok"):
            pair_trace_mismatches[trace["exit"]] += 1
            row["pair_trace_mismatch"] = True

        if real_pair is None:
            row["outcome"] = trace["exit"]
            row["plan"] = None
            rows.append(row)
            continue

        plan = brain._evaluate_trade_opportunity(
            origin=origin,
            destination=destination,
            commodity=commodity,
            pair=real_pair,
            acquisition=acquisition,
        )
        if plan is None:
            row["outcome"] = (
                "no_sellable_demand"
                if row["sellable"] <= 0
                else "quantity_zero_after_budget"
            )
            row["plan"] = None
            rows.append(row)
            continue

        accepted = brain._plan_acceptable(plan)
        row["plan"] = {
            "quantity": plan.quantity,
            "purchase_price": plan.purchase_price_per_unit,
            "sell_price": plan.expected_sell_price_per_unit,
            "expected_profit": plan.expected_profit,
            "expected_revenue": plan.expected_revenue,
            "total_purchase_cost": plan.total_purchase_cost,
            "total_fuel_cost": plan.total_fuel_cost,
            "expected_maintenance_cost": plan.expected_maintenance_cost,
            "margin": round(plan.profit_margin, 4),
            "min_margin": TradePlan.MIN_MARGIN,
            "return_leg_fuel_cost": plan.return_leg_fuel_cost,
            "accepted": accepted,
        }
        row["outcome"] = "plan_accepted" if accepted else "plan_margin_rejected"
        rows.append(row)
    return rows, summary


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
    sim: Simulation, staple: CommodityDefinition, threshold: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ship = pick_evaluator(sim)
    if ship is None:
        return [], []
    brain = ship.brain
    if not isinstance(brain, TraderBrain):
        return [], []
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    nav = get_navigator(sim)
    pair_rows: List[Dict[str, Any]] = []
    origin_rows: List[Dict[str, Any]] = []
    for planet, stock in surplus_planets(sim, staple, threshold):
        # A ship docked at the surplus planet, if any: it carries this
        # planet's real fuel and cash situation.
        local_ships = [
            s
            for s in planet.ships
            if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
        ]
        local = max(local_ships, key=lambda s: s.money) if local_ships else None
        evaluator = local.brain if local is not None else brain
        rows, summary = sample_origin(evaluator, nav, planet, staple, sim)
        summary.update(
            {
                "stock": stock,
                "ships_docked": len(planet.ships),
                "evaluator_is_local": local is not None,
                "evaluator_money": evaluator.ship.money,
                "evaluator_fuel": milli_to_units_ceil(evaluator.ship.fuel),
                "evaluator_cargo_fuel": (
                    evaluator.ship.cargo.get_quantity(fuel)
                    if fuel is not None
                    else None
                ),
                "book_staple": _book(planet.market, staple),
                "n_sampled_destinations": len(rows),
            }
        )
        for row in rows:
            row["origin_stock"] = stock
            row["evaluator_is_local"] = local is not None
        pair_rows.extend(rows)
        origin_rows.append(summary)
    return pair_rows, origin_rows


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
    if staple is None:
        raise ValueError(f"{STAPLE_ID} missing from the commodity registry")

    pair_rows: List[Dict[str, Any]] = []
    origin_rows: List[Dict[str, Any]] = []
    for _ in range(min(warmup, turns)):
        sim.run_turn()
    for _ in range(max(0, turns - warmup)):
        sim.run_turn()
        if sim.current_turn % sample_every == 0:
            pairs, origins = sample(sim, staple, threshold)
            pair_rows.extend(pairs)
            origin_rows.extend(origins)
            outcomes = Counter(r["outcome"] for r in pairs)
            print(
                f"turn {sim.current_turn}: {len(origins)} surplus planets, "
                f"{len(pairs)} pairs, outcomes {dict(outcomes)}",
                flush=True,
            )

    if patch_calls["execute"] == 0 or patch_calls["update_journey"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    surplus_names = {r["origin"] for r in origin_rows}
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "stock_threshold": threshold,
            "ships": len(sim.ships),
            "commodity": STAPLE_ID,
        },
        "pair_rows": pair_rows,
        "origin_rows": origin_rows,
        "ship_purchases": dict(ship_purchases.most_common()),
        "ship_sales": dict(ship_sales.most_common()),
        "arrivals_total": sum(arrivals.values()),
        "arrivals_at_surplus_planets": sum(arrivals[name] for name in surplus_names),
        "surplus_planets_ever": sorted(surplus_names),
        "surplus_planets_visited": sorted(
            name for name in surplus_names if arrivals[name] > 0
        ),
        "pair_trace_mismatches": dict(pair_trace_mismatches),
        "patch_calls": dict(patch_calls),
    }


def _median(values: List[Any]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def print_table(result: Dict[str, Any]) -> None:
    pairs: List[Dict[str, Any]] = result["pair_rows"]
    origins: List[Dict[str, Any]] = result["origin_rows"]
    print(
        f"\n{len(origins)} origin-observations, {len(pairs)} (origin, destination) pairs"
    )
    if not pairs:
        print("no pairs sampled")
        return

    outcomes = Counter(r["outcome"] for r in pairs)
    total = sum(outcomes.values())
    print("\npair outcomes")
    print(f"{'outcome':>30} {'count':>6} {'share':>6}")
    for outcome, count in outcomes.most_common():
        print(f"{outcome:>30} {count:>6} {100.0 * count / total:>5.0f}%")

    in_cand = [r for r in pairs if r["in_candidate_destinations"]]
    print(
        f"\nin candidate_destinations: {len(in_cand)}/{len(pairs)}; "
        f"outcomes {dict(Counter(r['outcome'] for r in in_cand).most_common())}"
    )
    out_cand = [r for r in pairs if not r["in_candidate_destinations"]]
    if out_cand:
        print(
            f"outside candidate_destinations: {len(out_cand)}; median bid depth "
            f"{_median([r['dest_bid_depth_total'] for r in out_cand])}, median best bid "
            f"{_median([r['dest_best_bid'] for r in out_cand])}"
        )

    print("\n10 deepest-bid destinations sampled")
    header = (
        f"{'destination':>14} {'origin':>14} {'hops':>4} {'dist':>7} {'bid':>5} "
        f"{'depth':>6} {'>entry':>6} {'entry':>5} {'sig':>4} {'flowv':>6} "
        f"{'flowq':>6} {'cand':>5} {'outcome':>28}"
    )
    print(header)
    for row in sorted(pairs, key=lambda r: -r["dest_bid_depth_total"])[:10]:
        print(
            f"{row['destination'][:14]:>14} {row['origin'][:14]:>14} "
            f"{row['lane_hops']:>4} {row['route_distance']:>7.2f} "
            f"{str(row['dest_best_bid']):>5} {row['dest_bid_depth_total']:>6} "
            f"{row['dest_bid_depth_above_entry']:>6} {row['entry_price']:>5} "
            f"{str(row['dest_has_price_signal'])[:4]:>4} "
            f"{str(row['dest_flow_value']):>6} {row['dest_flow_qty']:>6} "
            f"{str(row['in_candidate_destinations'])[:4]:>5} {row['outcome']:>28}"
        )
        pair = row["pair"]
        print(
            f"{'':>14}   money={pair.get('ship_money')} fuel={pair.get('ship_fuel')} "
            f"rt_fuel={pair.get('fuel_round_trip')} fuel_px={pair.get('fuel_price_at_origin')} "
            f"refuel_floor={pair.get('refuel_floor')} maint={pair.get('maintenance_cost')} "
            f"money_for_trading={pair.get('money_for_trading')}"
        )
        if row.get("plan"):
            p = row["plan"]
            print(
                f"{'':>14}   plan qty={p['quantity']} sell={p['sell_price']} "
                f"buy={p['purchase_price']} profit={p['expected_profit']} "
                f"margin={p['margin']} accepted={p['accepted']}"
            )

    exits = Counter(r["pair"]["exit"] for r in pairs)
    print(f"\n_pair_economics exits: {dict(exits.most_common())}")
    print(
        "medians: money_for_trading={m} round_trip_fuel={f} fuel_price={p} "
        "refuel_floor={r}".format(
            m=_median([r["pair"].get("money_for_trading") for r in pairs]),
            f=_median([r["pair"].get("fuel_round_trip") for r in pairs]),
            p=_median([r["pair"].get("fuel_price_at_origin") for r in pairs]),
            r=_median([r["pair"].get("refuel_floor") for r in pairs]),
        )
    )

    planned = [r for r in pairs if r.get("plan")]
    if planned:
        print(
            "plans built={n} accepted={a} median qty={q} sell={s} profit={pr} margin={mg}".format(
                n=len(planned),
                a=sum(1 for r in planned if r["plan"]["accepted"]),
                q=_median([r["plan"]["quantity"] for r in planned]),
                s=_median([r["plan"]["sell_price"] for r in planned]),
                pr=_median([r["plan"]["expected_profit"] for r in planned]),
                mg=_median([r["plan"]["margin"] for r in planned]),
            )
        )

    winners = Counter(
        r["best_plan_any_commodity"]["commodity"]
        for r in origins
        if r["best_plan_any_commodity"]
    )
    none_count = sum(1 for r in origins if not r["best_plan_any_commodity"])
    print(
        f"\n_best_plan_from winners across all commodities: {dict(winners.most_common())}; "
        f"no plan at all: {none_count}/{len(origins)}"
    )
    profits = [
        r["best_plan_any_commodity"]["expected_profit"]
        for r in origins
        if r["best_plan_any_commodity"]
    ]
    if profits:
        print(f"  median winning expected_profit={_median(profits)}")

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
    if result["pair_trace_mismatches"]:
        print("WARNING pair trace mismatches:", result["pair_trace_mismatches"])


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
