"""Would the ration branch have found a cheap pump, or a fair price, instead
of paying the spike.

Tier-1b in-process probe, sizing step 2 of the fuel refueling rework
(scratchpad/fuel_brief.md). ``TraderBrain._opportunistic_fuel_topup`` has two
branches: bunkering (ask within FUEL_BUNKER_PREMIUM of the galaxy reference)
and the ration branch (ask above that). This probe wraps the ration branch
only and, at the moment each ration buy order is placed, records the state
the order was placed under, then runs two independent analyses over the same
recorded orders:

1. Cheap-pump analysis (``by_reachability_*``, ``by_target_component``,
   ``pump_distance_stats``): which target set the order's size (the survival
   target vs a committed destination's fuel need), the ship's tank and
   capacity, and whether a "cheap pump" - a planet with a resting fuel ask at
   or below FUEL_BUNKER_PREMIUM times the reference and real supply evidence
   - was reachable either on the ship's current tank or from its committed
   destination after arrival. Fills are matched back to orders through
   ``Transaction.buy_order_id``, the same mechanism ship_fuel_path_probe.py
   uses.

2. Wait-for-fair-price analysis (``by_cap_and_wait``, ``wait_failures``):
   for orders whose ask/reference ratio exceeds a cap (2x, 3x tested), would
   a standing bid at a fair price (1.5x reference, or the planet's own
   30-day average) have filled within N turns (3, 5, 8 tested) had the ship
   waited instead of lifting the spiked ask? Answered by recording every
   nova_fuel trade at every planet during the run and looking forward from
   each order's turn. Orders are counted whether or not they filled, since
   the question is about the road not taken.

Two supply-evidence tests are recorded for the pump search: depth
(``Navigator.fuel_ask_depth_at`` >= 5, the design's threshold) and the
weaker recency signal (``Navigator.fuel_traded_recently``). Pump aggregates
use the depth test unless labelled "weak".

Run:
    uv run python notebooks/ship_fuel_cheap_pump_probe.py --turns 300 \\
        --planets 12 --out tmp/fuel_cheap_pump_12.json \\
        --wait-out tmp/fuel_wait_12.json
"""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Optional

from spacesim.cli.common import create_and_setup_simulation
from spacesim.core.market import Market
from spacesim.core.navigation import Navigator
from spacesim.core.planet import Planet
from spacesim.core.ship import (
    FUEL_BUNKER_PREMIUM,
    Ship,
    TraderBrain,
    milli_to_units_ceil,
)

# Ask depth, in units, that counts as real supply at a candidate pump. Below
# this a resting ask is a probe, not something a ship could actually fill up
# from; matches the design's "ask depth thick enough" language.
CHEAP_PUMP_MIN_DEPTH = 5

# Ask/reference ratios and wait windows sized for the standing-bid
# alternative: post a fair-price bid instead of lifting the spike, and wait
# up to N turns before giving in and lifting it.
WAIT_CAPS = (2.0, 3.0)
WAIT_TURNS = (3, 5, 8)
FAIR_PRICE_MULT = 1.5

ORDER_CTX: dict[str, dict[str, Any]] = {}
ORDER_FILLS: dict[str, dict[str, Any]] = defaultdict(lambda: {"units": 0, "credits": 0})
ORDER_QUANTITY: dict[str, int] = {}
ORDER_PLANET: dict[str, Planet] = {}
PLANET_FUEL_TRADES: dict[Planet, list[tuple[int, int]]] = defaultdict(list)


def cheap_ask_ceiling(reference: float) -> int:
    """The ask ceiling a planet must clear to count as a cheap pump."""
    return math.ceil(reference * FUEL_BUNKER_PREMIUM)


def find_cheap_pump(
    nav: Navigator,
    origin: Planet,
    fuel_commodity: Any,
    ceiling: int,
    use_depth: bool,
) -> Optional[Planet]:
    """Nearest planet with a resting ask <= ``ceiling`` and supply evidence.

    ``planets_by_proximity`` is sorted nearest-first, so the first match is
    the nearest cheap pump. ``use_depth`` selects the evidence test: ask
    depth >= CHEAP_PUMP_MIN_DEPTH (the design's primary test) or the weaker
    ``fuel_traded_recently`` signal.
    """
    for candidate in nav.planets_by_proximity(origin):
        _, ask = candidate.market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask > ceiling:
            continue
        if use_depth:
            if nav.fuel_ask_depth_at(candidate) >= CHEAP_PUMP_MIN_DEPTH:
                return candidate
        else:
            if nav.fuel_traded_recently(candidate):
                return candidate
    return None


def pump_facts(
    brain: TraderBrain,
    origin: Planet,
    fuel_commodity: Any,
    ceiling: int,
    use_depth: bool,
    current_fuel: int,
) -> dict[str, Any]:
    """Distance, cost, and reachability facts for the nearest cheap pump.

    ``reachable_now`` asks whether the ship's current tank covers the leg to
    the pump plus the pump's own arrival floor (``_arrival_fuel_requirement``
    with ``origin`` as the return planet, the same convention
    ``_reposition_destination`` uses for a non-plan trip).
    """
    nav = brain._nav
    pump = find_cheap_pump(nav, origin, fuel_commodity, ceiling, use_depth)
    if pump is None:
        return {
            "found": False,
            "distance": None,
            "hops": None,
            "fuel_to_pump": None,
            "arrival_req": None,
            "ask": None,
            "reachable_now": False,
        }
    distance = nav.distance(origin, pump)
    fuel_to_pump_milli = brain.ship.fuel_required(distance)
    arrival_req_milli = brain._arrival_fuel_requirement(pump, origin)
    _, ask = pump.market.get_bid_ask_spread(fuel_commodity)
    hops = len(nav.route(origin, pump)) - 1
    return {
        "found": True,
        "distance": distance,
        "hops": hops,
        # Market units, so these compare directly with order quantities/asks.
        "fuel_to_pump": milli_to_units_ceil(fuel_to_pump_milli),
        "arrival_req": milli_to_units_ceil(arrival_req_milli),
        "ask": ask,
        "reachable_now": current_fuel >= fuel_to_pump_milli + arrival_req_milli,
    }


def committed_destination_of(brain: TraderBrain) -> Optional[Planet]:
    """The destination this turn's fuel commitment is funding, if any.

    Mirrors the branch ``decide_trade_actions`` uses to set
    ``_committed_fuel_need``: a loaded plan's destination, else a standing
    reposition intent's target. The cargo-disposition branch (selling held
    cargo without a plan) does not expose a single destination object and is
    left as "no committed destination" here.
    """
    plan = brain._current_plan
    if plan is not None and brain._plan_loaded:
        return plan.destination
    if brain._reposition_intent is not None:
        return brain._reposition_intent.target
    return None


# --- capture the quantity behind every ship nova_fuel buy order -------------
_orig_place_buy = Market.place_buy_order


def place_buy_order(
    self: Market, actor: Any, commodity: Any, quantity: int, price: int
) -> Optional[str]:  # type: ignore[override]
    order_id = _orig_place_buy(self, actor, commodity, quantity, price)
    if order_id and isinstance(actor, Ship) and commodity.id == "nova_fuel":
        ORDER_QUANTITY[order_id] = quantity
    return order_id


Market.place_buy_order = place_buy_order  # type: ignore[method-assign]

# --- wrap the ration branch, tag its orders with full context --------------
_orig_topup = TraderBrain._opportunistic_fuel_topup


def topup(self: TraderBrain, *args: Any, **kwargs: Any) -> Optional[str]:
    planet = self.ship.planet
    fuel = self._fuel_commodity()
    ctx: Optional[dict[str, Any]] = None
    if planet is not None and fuel is not None:
        _, ask = planet.market.get_bid_ask_spread(fuel)
        reference = self._fuel_value_reference()
        if ask is not None and reference and ask > cheap_ask_ceiling(reference):
            # Ration branch: replicate the exact target computation from
            # _opportunistic_fuel_topup so "which component set the target"
            # matches what the order actually bought.
            ship = self.ship
            survival_target = self._fuel_survival_target()
            escape_target = self._fuel_escape_target()
            if escape_target is not None:
                survival_target = min(survival_target, escape_target)
            committed = self._committed_fuel_need
            if committed > survival_target:
                component = "committed"
            elif survival_target > committed:
                component = "survival"
            else:
                component = "tie"

            current_fuel = ship.fuel
            ceiling = cheap_ask_ceiling(reference)
            dest = committed_destination_of(self)
            dest_fuel_needed = None
            dest_name = None
            reachable_from_dest = None
            pump_from_dest = None
            pump_from_dest_weak = None
            if dest is not None:
                dest_name = dest.name
                distance_to_dest = self._nav.distance(planet, dest)
                dest_fuel_needed = ship.fuel_required(distance_to_dest)
                tank_after_dest = current_fuel - dest_fuel_needed
                if tank_after_dest >= 0:
                    pump_from_dest = pump_facts(
                        self, dest, fuel, ceiling, True, tank_after_dest
                    )
                    pump_from_dest_weak = pump_facts(
                        self, dest, fuel, ceiling, False, tank_after_dest
                    )
                    reachable_from_dest = (
                        pump_from_dest["found"] and pump_from_dest["reachable_now"]
                    )
                else:
                    reachable_from_dest = False

            # Truly stuck: no escape hop exists at all, or the tank is
            # already at or below the escape-only ration level. Stuck ships
            # are not candidates for "wait it out" - they must lift the
            # spike to leave at all.
            stuck = escape_target is None or current_fuel <= escape_target

            ctx = {
                "turn": ship.simulation.current_turn,
                "ship": ship.name,
                "planet": planet.name,
                "ask": ask,
                "ref": reference,
                "ratio": ask / reference,
                "money": ship.money,
                "fuel_traded_recently": self._nav.fuel_traded_recently(planet),
                "thirty_day_avg": planet.market.get_30_day_average_price(fuel),
                "component": component,
                # Market units, so these compare with ask/ref/money above.
                "survival_target": milli_to_units_ceil(survival_target),
                "escape_target": (
                    milli_to_units_ceil(escape_target)
                    if escape_target is not None
                    else None
                ),
                "committed_fuel_need": milli_to_units_ceil(committed),
                "stuck": stuck,
                "tank": milli_to_units_ceil(current_fuel),
                "tank_capacity": milli_to_units_ceil(ship.fuel_capacity),
                "has_committed_destination": dest is not None,
                "committed_destination": dest_name,
                "committed_fuel_one_way": (
                    milli_to_units_ceil(dest_fuel_needed)
                    if dest_fuel_needed is not None
                    else None
                ),
                "reachable_from_committed_destination": reachable_from_dest,
                "pump_now": pump_facts(self, planet, fuel, ceiling, True, current_fuel),
                "pump_now_weak": pump_facts(
                    self, planet, fuel, ceiling, False, current_fuel
                ),
                "pump_from_dest": pump_from_dest,
                "pump_from_dest_weak": pump_from_dest_weak,
            }
    order_id = _orig_topup(self, *args, **kwargs)
    if order_id and ctx is not None:
        ORDER_CTX[order_id] = ctx
        ORDER_PLANET[order_id] = planet
    return order_id


TraderBrain._opportunistic_fuel_topup = topup  # type: ignore[method-assign]

# --- fills and market-wide fuel trades ---------------------------------------


def collect_fills(sim: Any, seen: set[int], fuel_commodity: Any) -> None:
    for planet in sim.planets:
        for tx in planet.market.transaction_history:
            if tx.transaction_id in seen:
                continue
            seen.add(tx.transaction_id)
            if tx.commodity_type is fuel_commodity:
                PLANET_FUEL_TRADES[planet].append((tx.turn, tx.price))
            order_id = tx.buy_order_id or ""
            if order_id not in ORDER_CTX:
                continue
            bucket = ORDER_FILLS[order_id]
            bucket["units"] += tx.quantity
            bucket["credits"] += tx.quantity * tx.price


# --- cheap-pump aggregation ---------------------------------------------------


def category_for(ctx: dict[str, Any], weak: bool) -> str:
    """(a) reachable now, (b) reachable from committed destination, (c) neither."""
    pump_now = ctx["pump_now_weak"] if weak else ctx["pump_now"]
    if pump_now["found"] and pump_now["reachable_now"]:
        return "a_reachable_now"
    if weak:
        pump_dest = ctx["pump_from_dest_weak"]
        reachable_dest = (
            pump_dest is not None and pump_dest["found"] and pump_dest["reachable_now"]
        )
    else:
        reachable_dest = ctx["reachable_from_committed_destination"]
    if reachable_dest:
        return "b_reachable_from_dest"
    return "c_neither"


def avoided_premium(ctx: dict[str, Any], filled_units: int, avg_price: float) -> float:
    """Premium avoided had the ship bought only leg + arrival floor to the
    nearest cheap pump at the spiked ask, filling the rest there.

    Uses the depth-evidenced pump at the ship's current planet, the primary
    evidence test. Zero when no cheap pump was found or none of the filled
    units exceed the leg + arrival floor.
    """
    pump = ctx["pump_now"]
    if not pump["found"]:
        return 0.0
    spike_units_needed = pump["fuel_to_pump"] + pump["arrival_req"]
    remainder = filled_units - spike_units_needed
    if remainder <= 0:
        return 0.0
    return remainder * (avg_price - pump["ask"])


def summarize_pump(turns: int, planets: int, ships: int) -> dict[str, Any]:
    rows = []
    for order_id, ctx in ORDER_CTX.items():
        fill = ORDER_FILLS.get(order_id)
        if fill is None or fill["units"] <= 0:
            continue
        units = fill["units"]
        credits = fill["credits"]
        avg_price = credits / units
        premium = units * (avg_price - ctx["ref"])
        rows.append(
            {
                "ctx": ctx,
                "units": units,
                "credits": credits,
                "premium": premium,
                "avoided": avoided_premium(ctx, units, avg_price),
                "cat": category_for(ctx, weak=False),
                "cat_weak": category_for(ctx, weak=True),
            }
        )

    def bucket_table(key_fn: Any) -> dict[str, Any]:
        table: dict[str, dict[str, float]] = defaultdict(
            lambda: {"units": 0, "credits": 0.0, "premium": 0.0, "n": 0}
        )
        for r in rows:
            b = table[key_fn(r)]
            b["units"] += r["units"]
            b["credits"] += r["credits"]
            b["premium"] += r["premium"]
            b["n"] += 1
        total_units = sum(b["units"] for b in table.values()) or 1
        total_premium = sum(b["premium"] for b in table.values()) or 1
        for b in table.values():
            b["share_units"] = round(b["units"] / total_units, 3)
            b["share_premium"] = round(b["premium"] / total_premium, 3)
        return {k: dict(v) for k, v in sorted(table.items())}

    category_table = bucket_table(lambda r: r["cat"])
    category_table_weak = bucket_table(lambda r: r["cat_weak"])
    component_table = bucket_table(lambda r: r["ctx"]["component"])
    cross_table = bucket_table(lambda r: f"{r['ctx']['component']}|{r['cat']}")

    found_now = [r["ctx"]["pump_now"] for r in rows if r["ctx"]["pump_now"]["found"]]
    distances = [p["distance"] for p in found_now]
    hops = [p["hops"] for p in found_now]
    no_pump_orders = sum(1 for r in rows if not r["ctx"]["pump_now"]["found"])

    total_premium = sum(r["premium"] for r in rows)
    total_avoided = sum(r["avoided"] for r in rows)

    return {
        "params": {"turns": turns, "planets": planets, "ships": ships},
        "ration_orders_placed": len(ORDER_CTX),
        "ration_orders_filled": len(rows),
        "ration_units_total": sum(r["units"] for r in rows),
        "ration_credits_total": sum(r["credits"] for r in rows),
        "ration_premium_total": total_premium,
        "avoided_premium_total": total_avoided,
        "avoided_premium_share": round(total_avoided / total_premium, 3)
        if total_premium
        else 0.0,
        "by_reachability_depth": category_table,
        "by_reachability_weak": category_table_weak,
        "by_target_component": component_table,
        "by_component_x_reachability": cross_table,
        "pump_distance_stats": {
            "n_with_pump": len(distances),
            "n_no_pump_found": no_pump_orders,
            "distance_median": round(median(distances), 1) if distances else None,
            "distance_p90": (
                round(sorted(distances)[int(0.9 * (len(distances) - 1))], 1)
                if distances
                else None
            ),
            "hops_median": median(hops) if hops else None,
            "hops_p90": (sorted(hops)[int(0.9 * (len(hops) - 1))] if hops else None),
        },
    }


def print_pump_tables(result: dict[str, Any]) -> None:
    print("\n== RATION BRANCH: ORDERS AND FILLS ==")
    for k in (
        "ration_orders_placed",
        "ration_orders_filled",
        "ration_units_total",
        "ration_credits_total",
        "ration_premium_total",
        "avoided_premium_total",
        "avoided_premium_share",
    ):
        print(f"  {k}: {result[k]}")

    for label in (
        "by_reachability_depth",
        "by_reachability_weak",
        "by_target_component",
        "by_component_x_reachability",
    ):
        print(f"\n== {label.upper()} ==")
        print(
            f"  {'key':28s} {'n':>5s} {'units':>7s} {'credits':>9s} {'premium':>9s} {'sh_u':>6s} {'sh_p':>6s}"
        )
        for k, v in result[label].items():
            print(
                f"  {k:28s} {v['n']:5d} {v['units']:7d} {v['credits']:9.0f} "
                f"{v['premium']:9.0f} {v['share_units']:6.2f} {v['share_premium']:6.2f}"
            )

    print("\n== NEAREST CHEAP PUMP DISTANCE/HOPS (depth evidence) ==")
    for k, v in result["pump_distance_stats"].items():
        print(f"  {k}: {v}")


# --- wait-for-fair-price aggregation ------------------------------------------


def fair_trade_within(
    planet: Planet, order_turn: int, window: int, threshold: float
) -> bool:
    """Whether ``planet`` traded nova_fuel at or below ``threshold`` within
    ``window`` turns strictly after ``order_turn``.

    Uses ``PLANET_FUEL_TRADES``, recorded live during the run rather than
    read back from ``Market.transaction_history`` after the fact, because
    that history is capped (TRANSACTIONS_KEEP_GLOBAL) per planet across all
    commodities and would have dropped early fuel trades by the end of a
    300-turn run.
    """
    trades = PLANET_FUEL_TRADES.get(planet, [])
    return any(
        order_turn < t <= order_turn + window and p <= threshold for t, p in trades
    )


def summarize_wait(turns: int, planets: int, ships: int) -> dict[str, Any]:
    orders = []
    for order_id, ctx in ORDER_CTX.items():
        quantity = ORDER_QUANTITY.get(order_id)
        planet = ORDER_PLANET.get(order_id)
        if quantity is None or planet is None:
            continue
        ask = ctx["ask"]
        ref = ctx["ref"]
        fair_price = FAIR_PRICE_MULT * ref
        thirty_day_avg = ctx["thirty_day_avg"]
        orders.append(
            {
                "ctx": ctx,
                "planet": planet,
                "quantity": quantity,
                "ask": ask,
                "ref": ref,
                "fair_price": fair_price,
                "thirty_day_avg": thirty_day_avg,
                "credits_at_spike": quantity * ask,
                "premium_at_spike": quantity * (ask - ref),
                "premium_if_waited": quantity * max(0.0, ask - fair_price),
            }
        )

    by_cap_and_wait: dict[str, dict[str, Any]] = {}
    wait_failures: dict[str, dict[str, Any]] = {}
    for cap in WAIT_CAPS:
        subset = [o for o in orders if o["ctx"]["ratio"] > cap]
        units_total = sum(o["quantity"] for o in subset)
        credits_total = sum(o["credits_at_spike"] for o in subset)
        premium_total = sum(o["premium_at_spike"] for o in subset)
        for n in WAIT_TURNS:
            key = f"cap{cap:g}x_wait{n}"
            fair1_units = 0
            fair1_credits = 0
            fair1_premium_saved = 0.0
            fair30_units = 0
            n_fail = 0
            n_fail_stuck = 0
            n_fail_not_stuck = 0
            for o in subset:
                fair1 = fair_trade_within(
                    o["planet"], o["ctx"]["turn"], n, o["fair_price"]
                )
                fair30 = fair_trade_within(
                    o["planet"], o["ctx"]["turn"], n, o["thirty_day_avg"]
                )
                if fair1:
                    fair1_units += o["quantity"]
                    fair1_credits += o["credits_at_spike"]
                    fair1_premium_saved += o["premium_if_waited"]
                else:
                    n_fail += 1
                    if o["ctx"]["stuck"]:
                        n_fail_stuck += 1
                    else:
                        n_fail_not_stuck += 1
                if fair30:
                    fair30_units += o["quantity"]
            by_cap_and_wait[key] = {
                "n_orders": len(subset),
                "units_total": units_total,
                "credits_total": credits_total,
                "premium_total": round(premium_total, 0),
                "share_units_fair_1_5x": round(fair1_units / units_total, 3)
                if units_total
                else 0.0,
                "share_credits_fair_1_5x": round(fair1_credits / credits_total, 3)
                if credits_total
                else 0.0,
                "share_premium_saveable_1_5x": round(
                    fair1_premium_saved / premium_total, 3
                )
                if premium_total
                else 0.0,
                "premium_saved_if_waited": round(fair1_premium_saved, 0),
                "share_units_fair_30day_avg": round(fair30_units / units_total, 3)
                if units_total
                else 0.0,
            }
            wait_failures[key] = {
                "n_fail": n_fail,
                "n_fail_stuck": n_fail_stuck,
                "n_fail_not_stuck": n_fail_not_stuck,
            }

    return {
        "params": {"turns": turns, "planets": planets, "ships": ships},
        "ration_orders_recorded": len(orders),
        "by_cap_and_wait": by_cap_and_wait,
        "wait_failures": wait_failures,
    }


def print_wait_tables(result: dict[str, Any]) -> None:
    print("\n== WAIT-FOR-FAIR-PRICE: BY CAP x WAIT WINDOW ==")
    print(
        f"  {'key':16s} {'n':>5s} {'units':>7s} {'credits':>9s} {'premium':>9s} "
        f"{'sh_u_1.5x':>9s} {'sh_p_saveable':>13s} {'saved':>9s} {'sh_u_30avg':>10s}"
    )
    for k, v in result["by_cap_and_wait"].items():
        print(
            f"  {k:16s} {v['n_orders']:5d} {v['units_total']:7d} "
            f"{v['credits_total']:9.0f} {v['premium_total']:9.0f} "
            f"{v['share_units_fair_1_5x']:9.2f} {v['share_premium_saveable_1_5x']:13.2f} "
            f"{v['premium_saved_if_waited']:9.0f} {v['share_units_fair_30day_avg']:10.2f}"
        )

    print("\n== WAIT FAILURES (no fair trade within window), BY STUCK STATUS ==")
    print(f"  {'key':16s} {'n_fail':>7s} {'stuck':>7s} {'not_stuck':>10s}")
    for k, v in result["wait_failures"].items():
        print(
            f"  {k:16s} {v['n_fail']:7d} {v['n_fail_stuck']:7d} "
            f"{v['n_fail_not_stuck']:10d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=300)
    parser.add_argument("--planets", type=int, default=12)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument(
        "--out", type=Path, default=Path("tmp/fuel_cheap_pump_probe.json")
    )
    parser.add_argument(
        "--wait-out", type=Path, default=Path("tmp/fuel_wait_probe.json")
    )
    args = parser.parse_args()
    sim = create_and_setup_simulation(
        planets=args.planets, actors=args.actors, makers=2, ships=args.ships
    )
    fuel_commodity = sim.commodity_registry.get_commodity("nova_fuel")
    seen: set[int] = set()
    for _ in range(args.turns):
        sim.run_turn()
        collect_fills(sim, seen, fuel_commodity)
        if sim.current_turn % 25 == 0:
            print(
                f"turn {sim.current_turn}: ration orders {len(ORDER_CTX)} "
                f"fills tracked {len(ORDER_FILLS)}",
                flush=True,
            )
    pump_result = summarize_pump(args.turns, args.planets, len(sim.ships))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pump_result, indent=1, default=str))
    print_pump_tables(pump_result)
    print(f"\nwrote {args.out}")

    wait_result = summarize_wait(args.turns, args.planets, len(sim.ships))
    args.wait_out.parent.mkdir(parents=True, exist_ok=True)
    args.wait_out.write_text(json.dumps(wait_result, indent=1, default=str))
    print_wait_tables(wait_result)
    print(f"\nwrote {args.wait_out}")


if __name__ == "__main__":
    main()
