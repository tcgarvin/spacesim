"""Per-destination trace of why processed_food never leaves the cheap planets.

Tier-1b in-process probe. One row per sampled ``(origin, destination)`` pair
for ``processed_food``, where the origins are the planets whose resting
processed_food ask is cheap and where trader ships are docked, and the
destinations are the highest-priced processed_food markets in the galaxy at
that sample turn, united with ``Navigator.candidate_destinations``.

Each pair is walked through the planner's real gates, in order, by calling
the real methods (``_trip_fuel_economics``, ``_pair_economics``,
``_origin_acquisition``, ``_evaluate_trade_opportunity``,
``_plan_acceptable``, ``_best_plan_from``). Nothing in ship.py is
re-implemented for the verdict; the probe only recomputes the intermediate
*numbers* behind each gate so the rejection can be read. When the real
method disagrees with the probe's sub-gate attribution, the pair is counted
in ``gate_attribution_mismatches``.

Per sampled origin it also records the plan ``_best_plan_from`` actually
returns across all commodities, so what outranks the staple is visible.

Run (threaded actor phase, free-threaded interpreter):

    /home/timg/code/spacesim/.venv-ft/bin/python \
        /home/timg/code/spacesim/notebooks/processed_food_export_pairs.py \
        --turns 400 --planets 50 --ships 1 --warmup 150 --sample-every 50 \
        --workers 6 --out /home/timg/code/spacesim/tmp/pf_pairs.json
"""

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spacesim.cli.common import create_and_setup_simulation
from spacesim.core.commodity import CommodityDefinition
from spacesim.core.market import Market
from spacesim.core.navigation import Navigator, get_navigator
from spacesim.core.planet import Planet
from spacesim.core.ship import (
    DEMAND_HORIZON_TURNS,
    FUEL_MILLI,
    MAINTENANCE_CHANCE,
    MAINTENANCE_FUEL_MILLI,
    SELL_PRICE_HAIRCUT,
    SPECULATIVE_PLAN_CAP,
    Ship,
    ShipStatus,
    TradePlan,
    TraderBrain,
    milli_to_units_ceil,
    milli_to_units_floor,
    units_to_milli,
)
from spacesim.core.simulation import Simulation

STAPLE_ID = "processed_food"
DEFAULT_CHEAP_ASK_MAX = 2
DEFAULT_TOP_DESTINATIONS = 8

ship_purchases: Counter[str] = Counter()
ship_sales: Counter[str] = Counter()
arrivals: Counter[str] = Counter()
patch_calls: Counter[str] = Counter()
gate_attribution_mismatches: Counter[str] = Counter()


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


def trip_diagnostics(
    brain: TraderBrain, origin: Planet, destination: Planet
) -> Dict[str, Any]:
    """Every number ``_trip_fuel_economics`` and ``_pair_economics`` turn on.

    Recomputed from the ship's own helper methods so the rejection can be
    read; the verdict itself always comes from the real methods.
    """
    ship = brain.ship
    out: Dict[str, Any] = {}
    fuel_commodity = brain._fuel_commodity()
    if fuel_commodity is None:
        out["fuel_commodity"] = None
        return out

    distance = brain._nav.distance(origin, destination)
    fuel_one_way = ship.fuel_required(distance)
    fuel_round_trip = fuel_one_way * 2

    _, fuel_ask = origin.market.get_bid_ask_spread(fuel_commodity)
    fuel_price = (
        fuel_ask
        if fuel_ask is not None
        else origin.market.get_avg_price(fuel_commodity)
    )
    if fuel_price is None or fuel_price <= 0:
        fuel_price = 10

    current_fuel = ship.fuel
    round_trip_shortfall = max(0, fuel_round_trip - current_fuel)
    arrival_requirement = brain._arrival_fuel_requirement(destination, origin)
    fuel_wanted = max(fuel_round_trip, fuel_one_way + arrival_requirement)
    tank_room = ship.fuel_capacity - current_fuel
    fuel_to_buy = min(
        milli_to_units_ceil(min(fuel_wanted - current_fuel, tank_room)),
        milli_to_units_floor(tank_room),
    )
    fuel_after_arrival = current_fuel + units_to_milli(fuel_to_buy) - fuel_one_way
    fuel_cost = fuel_to_buy * fuel_price
    maintenance_cost = math.ceil(
        2 * MAINTENANCE_CHANCE * MAINTENANCE_FUEL_MILLI * fuel_price / FUEL_MILLI
    )
    fuel_left_after_trip = max(current_fuel, fuel_round_trip) - fuel_round_trip
    reserve_need = brain._fuel_reserve_need()
    refuel_shortfall = max(0, reserve_need - fuel_left_after_trip)
    refuel_floor = milli_to_units_ceil(refuel_shortfall) * fuel_price
    money_for_trading = int(
        (ship.money - fuel_cost - refuel_floor - maintenance_cost) * 0.9
    )

    out.update(
        {
            "distance": round(distance, 3),
            "fuel_one_way": milli_to_units_ceil(fuel_one_way),
            "fuel_round_trip": milli_to_units_ceil(fuel_round_trip),
            "fuel_price_at_origin": fuel_price,
            "fuel_ask_at_origin": fuel_ask,
            "ship_money": ship.money,
            "ship_fuel": milli_to_units_ceil(current_fuel),
            "ship_fuel_capacity": milli_to_units_ceil(ship.fuel_capacity),
            "cargo_space": ship.cargo_capacity - ship.cargo.get_total_quantity(),
            "round_trip_shortfall": milli_to_units_ceil(round_trip_shortfall),
            "arrival_fuel_requirement": milli_to_units_ceil(arrival_requirement),
            "fuel_to_buy": fuel_to_buy,
            "fuel_cost": fuel_cost,
            "fuel_after_arrival": milli_to_units_ceil(fuel_after_arrival),
            "fuel_purchasable_at_origin": brain._fuel_purchasable_at(origin),
            "fuel_purchasable_at_destination": brain._fuel_purchasable_at(destination),
            "local_fuel_bunkerable": brain._local_fuel_bunkerable(origin),
            "can_reach_station": brain._can_reach_station(),
            "fuel_safe_destination": brain._fuel_safe_destination(
                destination, origin, fuel_after_arrival
            ),
            "maintenance_cost": maintenance_cost,
            "fuel_reserve_need": milli_to_units_ceil(reserve_need),
            "refuel_floor": refuel_floor,
            "money_for_trading": money_for_trading,
            "ship_is_distressed": brain.is_distressed,
        }
    )
    return out


def attribute_trip_gate(diag: Dict[str, Any]) -> str:
    """Which ``_trip_fuel_economics`` gate rejected the pair, from ``diag``."""
    if diag.get("fuel_commodity", "present") is None:
        return "no_fuel_commodity"
    if diag["round_trip_shortfall"] > 0 and not diag["fuel_purchasable_at_origin"]:
        return "fuel_not_buyable_at_origin"
    if diag["fuel_to_buy"] > 0 and not diag["fuel_purchasable_at_origin"]:
        return "fuel_not_buyable_at_origin"
    if (
        diag["fuel_to_buy"] > 0
        and not diag["local_fuel_bunkerable"]
        and diag["can_reach_station"]
    ):
        return "fuel_spiked_prefer_station"
    if not diag["fuel_safe_destination"]:
        return "destination_not_fuel_safe"
    return "unattributed_trip_gate"


def destination_demand(
    brain: TraderBrain,
    destination: Planet,
    commodity: CommodityDefinition,
    entry_price: int,
) -> Dict[str, Any]:
    """Destination-side demand exactly as ``_evaluate_trade_opportunity`` sees it."""
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


def staple_price(market: Market, commodity: CommodityDefinition) -> float:
    """What a seller could get here: best resting bid, else the clearing price."""
    best_bid, _ = market.get_bid_ask_spread(commodity)
    avg = market.get_avg_price(commodity) if market.has_price_signal(commodity) else 0
    return float(max(best_bid or 0, avg or 0))


def pick_destinations(
    sim: Simulation,
    origin: Planet,
    commodity: CommodityDefinition,
    nav: Navigator,
    top_n: int,
) -> Tuple[List[Planet], set]:
    """The ``top_n`` priciest staple markets, united with the planner shortlist."""
    candidates = set(nav.candidate_destinations(origin, commodity))
    priced = sorted(
        (p for p in sim.planets if p is not origin),
        key=lambda p: -staple_price(p.market, commodity),
    )
    chosen: List[Planet] = [p for p in priced[:top_n]]
    chosen_set = set(chosen)
    for planet in candidates:
        if planet is not origin and planet not in chosen_set:
            chosen.append(planet)
            chosen_set.add(planet)
    return chosen, candidates


def sample_origin(
    brain: TraderBrain,
    nav: Navigator,
    origin: Planet,
    commodity: CommodityDefinition,
    sim: Simulation,
    top_n: int,
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
        "staple_in_shortlist_count": 0,
        "best_plan_any_commodity": None,
    }
    listing_here = {
        order.commodity_type
        for order in origin.market.get_actor_orders(brain.ship)["sell"]
    }
    summary["ship_listing_staple_here"] = commodity in listing_here

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
    if not summary["has_any_trade_signal"]:
        return [], summary
    if not summary["exportable_here"]:
        summary["blocked"] = "staple_not_exportable_here"
        return [], summary

    acquisition = brain._origin_acquisition(origin, commodity)
    if acquisition is None:
        summary["blocked"] = "staple_not_acquirable_here"
        return [], summary
    summary["entry_price"] = acquisition.entry_price
    summary["bid_price"] = acquisition.bid_price
    summary["flow_price"] = acquisition.flow_price

    sampled, candidate_set = pick_destinations(sim, origin, commodity, nav, top_n)
    summary["n_candidate_destinations"] = len(candidate_set)

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
        diag = trip_diagnostics(brain, origin, destination)
        row["pair"] = diag

        trip = brain._trip_fuel_economics(origin, destination)
        if trip is None:
            gate = attribute_trip_gate(diag)
            if gate == "unattributed_trip_gate":
                gate_attribution_mismatches["trip_none"] += 1
            row["outcome"] = gate
            row["plan"] = None
            rows.append(row)
            continue

        pair = brain._pair_economics(origin, destination)
        if pair is None:
            if diag["money_for_trading"] > 0:
                gate_attribution_mismatches["pair_none_but_money_positive"] += 1
            row["outcome"] = "no_money_for_trading"
            row["plan"] = None
            rows.append(row)
            continue

        plan = brain._evaluate_trade_opportunity(
            origin=origin,
            destination=destination,
            commodity=commodity,
            pair=pair,
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
        if not accepted:
            row["outcome"] = "plan_margin_rejected"
        elif commodity in listing_here:
            row["outcome"] = "skipped_ship_lists_staple_here"
        elif not row["in_candidate_destinations"]:
            row["outcome"] = "accepted_but_not_shortlisted"
        else:
            winner = summary["best_plan_any_commodity"]
            if (
                winner is not None
                and winner["commodity"] == commodity.id
                and winner["destination"] == destination.name
            ):
                row["outcome"] = "plan_won"
            else:
                row["outcome"] = "plan_lost_ranking"
                row["winner"] = winner
        rows.append(row)

    summary["staple_in_shortlist_count"] = sum(
        1 for r in rows if r["in_candidate_destinations"]
    )
    return rows, summary


def cheap_origins(
    sim: Simulation, staple: CommodityDefinition, nav: Navigator, cheap_ask_max: int
) -> List[Tuple[Planet, int, int]]:
    """Planets the staple can be acquired at, cheapest entry price first.

    Returns ``(planet, entry_price, resting ask depth)``. Planets whose entry
    price is at or under ``cheap_ask_max`` come first; the rest follow in
    price order, so a run where nothing rests at 1.0 still yields the
    cheapest sources instead of no rows at all.
    """
    rows: List[Tuple[Planet, int, int]] = []
    for planet in sim.planets:
        if staple not in nav.exportable_commodities(planet):
            continue
        _, ask = planet.market.get_bid_ask_spread(staple)
        depth = sum(q for _, q in planet.market.get_ask_levels(staple))
        avg = planet.market.get_avg_price(staple)
        entry = ask if ask is not None and ask > 0 else (avg or 0)
        if entry <= 0:
            continue
        rows.append((planet, int(entry), depth))
    rows.sort(key=lambda row: (row[1] > cheap_ask_max, row[1], -row[2]))
    return rows


def pick_evaluator(sim: Simulation) -> Optional[Ship]:
    """A representative docked ship: median money among docked ships."""
    docked = [
        s
        for s in sim.ships
        if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
    ]
    pool = (
        docked if docked else [s for s in sim.ships if isinstance(s.brain, TraderBrain)]
    )
    if not pool:
        return None
    pool.sort(key=lambda s: s.money)
    return pool[len(pool) // 2]


def sample(
    sim: Simulation,
    staple: CommodityDefinition,
    cheap_ask_max: int,
    top_n: int,
    max_origins: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    fallback = pick_evaluator(sim)
    if fallback is None:
        return [], []
    nav = get_navigator(sim)
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    pair_rows: List[Dict[str, Any]] = []
    origin_rows: List[Dict[str, Any]] = []

    origins = cheap_origins(sim, staple, nav, cheap_ask_max)
    with_ships = [
        row
        for row in origins
        if any(
            s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
            for s in row[0].ships
        )
    ]
    selected = (with_ships or origins)[:max_origins]

    for planet, entry_seen, depth in selected:
        local_ships = [
            s
            for s in planet.ships
            if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
        ]
        local = max(local_ships, key=lambda s: s.money) if local_ships else None
        evaluator = local.brain if local is not None else fallback.brain
        rows, summary = sample_origin(evaluator, nav, planet, staple, sim, top_n)
        summary.update(
            {
                "ask_depth": depth,
                "origin_entry_seen": entry_seen,
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
            row["origin_ask_depth"] = depth
            row["evaluator_is_local"] = local is not None
        pair_rows.extend(rows)
        origin_rows.append(summary)
    return pair_rows, origin_rows


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    sample_every: int,
    warmup: int,
    cheap_ask_max: int,
    top_n: int,
    max_origins: int,
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
            pairs, origins = sample(sim, staple, cheap_ask_max, top_n, max_origins)
            pair_rows.extend(pairs)
            origin_rows.extend(origins)
            outcomes = Counter(r["outcome"] for r in pairs)
            print(
                f"turn {sim.current_turn}: {len(origins)} cheap origins, "
                f"{len(pairs)} pairs, outcomes {dict(outcomes)}",
                flush=True,
            )

    if patch_calls["execute"] == 0 or patch_calls["update_journey"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    origin_names = {r["origin"] for r in origin_rows}
    dest_names = {r["destination"] for r in pair_rows}
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "cheap_ask_max": cheap_ask_max,
            "top_destinations": top_n,
            "max_origins": max_origins,
            "workers": workers,
            "ships": len(sim.ships),
            "commodity": STAPLE_ID,
        },
        "pair_rows": pair_rows,
        "origin_rows": origin_rows,
        "ship_purchases": dict(ship_purchases.most_common()),
        "ship_sales": dict(ship_sales.most_common()),
        "arrivals_total": sum(arrivals.values()),
        "arrivals_at_origins": sum(arrivals[name] for name in origin_names),
        "arrivals_at_destinations": sum(arrivals[name] for name in dest_names),
        "cheap_origins_ever": sorted(origin_names),
        "destinations_ever": sorted(dest_names),
        "gate_attribution_mismatches": dict(gate_attribution_mismatches),
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
    print(f"{'outcome':>32} {'count':>6} {'share':>6}")
    for outcome, count in outcomes.most_common():
        print(f"{outcome:>32} {count:>6} {100.0 * count / total:>5.0f}%")

    in_cand = [r for r in pairs if r["in_candidate_destinations"]]
    print(
        f"\nin candidate_destinations: {len(in_cand)}/{len(pairs)}; "
        f"outcomes {dict(Counter(r['outcome'] for r in in_cand).most_common())}"
    )
    out_cand = [r for r in pairs if not r["in_candidate_destinations"]]
    if out_cand:
        print(
            f"outside the shortlist: {len(out_cand)}; median best bid "
            f"{_median([r['dest_best_bid'] for r in out_cand])}, median avg price "
            f"{_median([r['dest_avg_price'] for r in out_cand])}, median bid depth "
            f"{_median([r['dest_bid_depth_total'] for r in out_cand])}"
        )

    print("\nsellable sources:", dict(Counter(r["sellable_source"] for r in pairs)))
    print(
        "medians: sellable={s} depth_above_entry={d} flow_qty={f} entry={e} "
        "dest_avg={a}".format(
            s=_median([r["sellable"] for r in pairs]),
            d=_median([r["dest_bid_depth_above_entry"] for r in pairs]),
            f=_median([r["dest_flow_qty"] for r in pairs]),
            e=_median([r["entry_price"] for r in pairs]),
            a=_median([r["dest_avg_price"] for r in pairs]),
        )
    )

    print("\n12 highest-priced destinations sampled")
    header = (
        f"{'destination':>14} {'origin':>14} {'hops':>4} {'dist':>7} {'bid':>5} "
        f"{'avg':>5} {'depth':>6} {'>entry':>6} {'entry':>5} {'flowv':>6} "
        f"{'flowq':>6} {'sell':>5} {'cand':>5} {'outcome':>30}"
    )
    print(header)
    for row in sorted(pairs, key=lambda r: -(r["dest_avg_price"] or 0))[:12]:
        print(
            f"{row['destination'][:14]:>14} {row['origin'][:14]:>14} "
            f"{row['lane_hops']:>4} {row['route_distance']:>7.2f} "
            f"{str(row['dest_best_bid']):>5} {str(row['dest_avg_price']):>5} "
            f"{row['dest_bid_depth_total']:>6} "
            f"{row['dest_bid_depth_above_entry']:>6} {row['entry_price']:>5} "
            f"{str(row['dest_flow_value']):>6} {row['dest_flow_qty']:>6} "
            f"{row['sellable']:>5} "
            f"{str(row['in_candidate_destinations'])[:4]:>5} {row['outcome']:>30}"
        )
        pair = row["pair"]
        print(
            f"{'':>14}   money={pair.get('ship_money')} fuel={pair.get('ship_fuel')} "
            f"rt_fuel={pair.get('fuel_round_trip')} buy={pair.get('fuel_to_buy')} "
            f"fuel_px={pair.get('fuel_price_at_origin')} "
            f"refuel_floor={pair.get('refuel_floor')} "
            f"mft={pair.get('money_for_trading')} "
            f"safe={pair.get('fuel_safe_destination')} "
            f"bunkerable={pair.get('local_fuel_bunkerable')} "
            f"station={pair.get('can_reach_station')}"
        )
        if row.get("plan"):
            p = row["plan"]
            print(
                f"{'':>14}   plan qty={p['quantity']} sell={p['sell_price']} "
                f"buy={p['purchase_price']} profit={p['expected_profit']} "
                f"margin={p['margin']} accepted={p['accepted']}"
            )
        if row.get("winner"):
            w = row["winner"]
            print(
                f"{'':>14}   beaten by {w['commodity']} -> {w['destination']} "
                f"qty={w['quantity']} profit={w['expected_profit']} "
                f"margin={w['margin']}"
            )

    planned = [r for r in pairs if r.get("plan")]
    if planned:
        print(
            "\nplans built={n} accepted={a} median qty={q} sell={s} profit={pr} "
            "margin={mg}".format(
                n=len(planned),
                a=sum(1 for r in planned if r["plan"]["accepted"]),
                q=_median([r["plan"]["quantity"] for r in planned]),
                s=_median([r["plan"]["sell_price"] for r in planned]),
                pr=_median([r["plan"]["expected_profit"] for r in planned]),
                mg=_median([r["plan"]["margin"] for r in planned]),
            )
        )

    lost = [r for r in pairs if r["outcome"] == "plan_lost_ranking" and r.get("winner")]
    if lost:
        print(f"\nlost-ranking pairs: {len(lost)}")
        print(
            f"{'origin':>14} {'staple_dest':>14} {'sqty':>5} {'sprof':>7} "
            f"{'winner':>20} {'wdest':>14} {'wqty':>5} {'wprof':>7}"
        )
        for row in lost[:12]:
            w = row["winner"]
            p = row["plan"]
            print(
                f"{row['origin'][:14]:>14} {row['destination'][:14]:>14} "
                f"{p['quantity']:>5} {p['expected_profit']:>7} "
                f"{w['commodity'][:20]:>20} {w['destination'][:14]:>14} "
                f"{w['quantity']:>5} {w['expected_profit']:>7}"
            )
        print(
            "  median staple profit={s} vs winner profit={w}".format(
                s=_median([r["plan"]["expected_profit"] for r in lost]),
                w=_median([r["winner"]["expected_profit"] for r in lost]),
            )
        )

    winners = Counter(
        r["best_plan_any_commodity"]["commodity"]
        for r in origins
        if r["best_plan_any_commodity"]
    )
    none_count = sum(1 for r in origins if not r["best_plan_any_commodity"])
    print(
        f"\n_best_plan_from winners across all commodities: "
        f"{dict(winners.most_common())}; no plan at all: {none_count}/{len(origins)}"
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
        "arrivals total={t} at cheap origins={o} at sampled destinations={d}".format(
            t=result["arrivals_total"],
            o=result["arrivals_at_origins"],
            d=result["arrivals_at_destinations"],
        )
    )
    if result["gate_attribution_mismatches"]:
        print(
            "WARNING gate attribution mismatches:",
            result["gate_attribution_mismatches"],
        )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=150)
    parser.add_argument("--cheap-ask-max", type=int, default=DEFAULT_CHEAP_ASK_MAX)
    parser.add_argument(
        "--top-destinations", type=int, default=DEFAULT_TOP_DESTINATIONS
    )
    parser.add_argument("--max-origins", type=int, default=8)
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
        cheap_ask_max=args.cheap_ask_max,
        top_n=args.top_destinations,
        max_origins=args.max_origins,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
