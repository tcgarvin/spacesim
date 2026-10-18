"""Which code path buys ship fuel at spike prices, and would honest costing stop it.

Tier-1b in-process probe. Wraps the TraderBrain fuel-buying paths so every
ship fuel buy order is tagged with the path that placed it, then matches
fills to those orders through ``Transaction.buy_order_id``. Records every
adopted trade plan with its spike ratio (origin fuel ask over the galaxy
reference) and re-scores it under two alternative fuel costings:

* cash: also charge the return-leg shortfall the ship buys here at the
  local ask (what the ship actually pays before it leaves).
* replacement: value the outbound units already in the tank at
  max(local ask, reference) instead of the reference.

Also records, at every plan departure, hold utilization and whether a second
commodity to the same destination would have added profit with the money and
space left over, and which cap (cargo, money, demand) bound the plan.

Run:
    uv run python notebooks/ship_fuel_path_probe.py --turns 300 --planets 12 --out tmp/fuel_path_12.json
"""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Optional

from spacesim.cli.common import create_and_setup_simulation
from spacesim.core import ship as ship_mod
from spacesim.core.market import Market
from spacesim.core.navigation import get_navigator
from spacesim.core.ship import (
    FUEL_BUNKER_PREMIUM,
    FUEL_MILLI,
    Ship,
    TradePlan,
    TraderBrain,
    _PairEconomics,
    milli_to_units_ceil,
)

PATH_STACK: list[str] = []
ORDER_PATH: dict[str, dict[str, Any]] = {}
FUEL_ORDERS: list[dict[str, Any]] = []
FUEL_FILLS: list[dict[str, Any]] = []
PLANS: list[dict[str, Any]] = []
DEPARTURES: list[dict[str, Any]] = []
PLAN_CAPS: dict[int, str] = {}
BANDS = (("a<=1.3", 1.3), ("b1.3-2", 2.0), ("c2-4", 4.0), ("d>4", math.inf))


def band(ratio: float) -> str:
    for label, upper in BANDS:
        if ratio <= upper:
            return label
    return "d>4"


def reference_for(brain: TraderBrain) -> float:
    ref = brain._fuel_value_reference()
    return float(ref) if ref else 0.0


# --- wrap order placement ----------------------------------------------------
_orig_place_buy = Market.place_buy_order


def place_buy_order(
    self: Market, actor: Any, commodity: Any, quantity: int, price: int
) -> Optional[str]:  # type: ignore[override]
    order_id = _orig_place_buy(self, actor, commodity, quantity, price)
    if order_id and isinstance(actor, Ship) and commodity.id == "nova_fuel":
        ref = reference_for(actor.brain)
        path = PATH_STACK[-1] if PATH_STACK else "untagged"
        record = {
            "turn": actor.simulation.current_turn,
            "path": path,
            "quantity": quantity,
            "price": price,
            "ref": ref,
            "ratio": price / ref if ref else math.nan,
        }
        ORDER_PATH[order_id] = record
        FUEL_ORDERS.append(record)
    return order_id


Market.place_buy_order = place_buy_order  # type: ignore[method-assign]


def tagged(name: str, method: Any) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        PATH_STACK.append(name)
        try:
            return method(self, *args, **kwargs)
        finally:
            PATH_STACK.pop()

    return wrapper


TraderBrain._execute_trade_plan = tagged("plan_fuel", TraderBrain._execute_trade_plan)  # type: ignore[method-assign]
TraderBrain._post_standing_fuel_bid = tagged(
    "standing_bid", TraderBrain._post_standing_fuel_bid
)  # type: ignore[method-assign]
Ship._buy_maintenance_supplies = tagged("maintenance", Ship._buy_maintenance_supplies)  # type: ignore[method-assign]

_orig_topup = TraderBrain._opportunistic_fuel_topup


def topup(self: TraderBrain, *args: Any, **kwargs: Any) -> Optional[str]:
    planet = self.ship.planet
    fuel = self._fuel_commodity()
    label = "topup_ration"
    if planet is not None and fuel is not None:
        _, ask = planet.market.get_bid_ask_spread(fuel)
        ref = reference_for(self)
        if ask is not None and ref and ask <= math.ceil(ref * FUEL_BUNKER_PREMIUM):
            label = "topup_bunker"
    PATH_STACK.append(label)
    try:
        return _orig_topup(self, *args, **kwargs)
    finally:
        PATH_STACK.pop()


TraderBrain._opportunistic_fuel_topup = topup  # type: ignore[method-assign]

# --- which cap bound each evaluated plan -----------------------------------------
_orig_evaluate = TraderBrain._evaluate_trade_opportunity


def evaluate(
    self: TraderBrain,
    origin: Any,
    destination: Any,
    commodity: Any,
    pair: Optional[_PairEconomics] = None,
    acquisition: Any = None,
) -> Optional[TradePlan]:
    if pair is None:
        pair = self._pair_economics(origin, destination)
    if acquisition is None:
        acquisition = self._origin_acquisition(origin, commodity)
    plan = _orig_evaluate(
        self, origin, destination, commodity, pair=pair, acquisition=acquisition
    )
    if plan is not None and pair is not None and acquisition is not None:
        by_money = (
            pair.money_for_trading // acquisition.bid_price
            if acquisition.bid_price > 0
            else 0
        )
        if plan.quantity >= pair.max_by_cargo:
            cap = "cargo"
        elif plan.quantity >= by_money:
            cap = "money"
        else:
            cap = "demand"
        PLAN_CAPS[id(plan)] = cap
    return plan


TraderBrain._evaluate_trade_opportunity = evaluate  # type: ignore[method-assign]

# --- plan adoption ------------------------------------------------------------------
_orig_decide = TraderBrain.decide_trade_actions


def decide_trade_actions(self: TraderBrain) -> None:
    before = self._current_plan
    _orig_decide(self)
    plan = self._current_plan
    ship = self.ship
    if plan is None or plan is before or ship.planet is not plan.origin:
        return
    fuel = self._fuel_commodity()
    if fuel is None:
        return
    current_fuel = ship.fuel
    ref = reference_for(self)
    ask = plan.fuel_price_at_origin
    # These plan fields and current_fuel are millifuel (see FUEL_MILLI);
    # every product with a per-market-unit price is divided back down.
    to_buy_round_trip = max(0, plan.fuel_needed_round_trip - current_fuel)
    to_buy_outbound = plan.fuel_needed_one_way - min(
        plan.fuel_units_from_tank, plan.fuel_needed_one_way
    )
    extra_cash = round((to_buy_round_trip - to_buy_outbound) * ask / FUEL_MILLI)
    replacement_price = max(ask, plan.fuel_price_from_tank)
    extra_replacement = round(
        min(plan.fuel_units_from_tank, plan.fuel_needed_one_way)
        * (replacement_price - plan.fuel_price_from_tank)
        / FUEL_MILLI
    )
    costs = (
        plan.total_purchase_cost + plan.total_fuel_cost + plan.expected_maintenance_cost
    )
    profit_cash = plan.expected_profit - extra_cash
    profit_repl = plan.expected_profit - extra_replacement
    PLANS.append(
        {
            "turn": ship.simulation.current_turn,
            "ratio": ask / ref if ref else math.nan,
            "profit": plan.expected_profit,
            "margin": plan.profit_margin,
            "fuel_share_of_cost": plan.total_fuel_cost / costs if costs else 0.0,
            "profit_cash": profit_cash,
            "margin_cash": profit_cash / (costs + extra_cash)
            if costs + extra_cash > 0
            else 0.0,
            "profit_repl": profit_repl,
            "margin_repl": profit_repl / (costs + extra_replacement)
            if costs + extra_replacement > 0
            else 0.0,
            "fuel_to_buy": milli_to_units_ceil(to_buy_round_trip),
            "cap": PLAN_CAPS.get(id(plan), "unknown"),
            "distressed": self.is_distressed,
        }
    )


TraderBrain.decide_trade_actions = decide_trade_actions  # type: ignore[method-assign]

# --- departure: hold utilization and add-on opportunity ---------------------------
_orig_start = Ship.start_journey


def start_journey(self: Ship, destination: Any, **kwargs: Any) -> bool:
    brain = self.brain
    plan = brain._current_plan
    origin = self.planet
    record: Optional[dict[str, Any]] = None
    if (
        isinstance(brain, TraderBrain)
        and plan is not None
        and origin is not None
        and plan.origin is origin
        and plan.destination is destination
        and brain._plan_loaded
    ):
        fuel = brain._fuel_commodity()
        fuel_units = self.cargo.get_quantity(fuel) if fuel else 0
        total = self.cargo.get_total_quantity()
        non_fuel = total - fuel_units
        free = self.cargo_capacity - total
        held_kinds = sum(
            1
            for c in brain._get_tradeable_commodities()
            if c.id != "nova_fuel" and self.cargo.get_quantity(c) > 0
        )
        nav = brain._nav
        ref = reference_for(brain)
        _, ask = origin.market.get_bid_ask_spread(fuel) if fuel else (None, None)
        fuel_price = ask if ask else math.ceil(ref) if ref else 10
        distance = nav.distance(origin, destination)
        one_way = self.fuel_required(distance)
        addon_pair = _PairEconomics(
            distance=distance,
            fuel_one_way=one_way,
            fuel_price=fuel_price,
            fuel_reference_price=math.ceil(ref) if ref else fuel_price,
            fuel_to_buy=0,
            fuel_from_tank_one_way=one_way,
            expected_maintenance_cost=0,
            money_for_trading=int(self.money * 0.9),
            max_by_cargo=free,
        )
        best_addon = 0
        best_addon_units = 0
        addon_candidates = 0
        if free > 0 and self.money > 0:
            for commodity in nav.exportable_commodities(origin):
                if commodity is plan.commodity or commodity.id == "nova_fuel":
                    continue
                addon = _orig_evaluate(
                    brain,
                    origin,
                    destination,
                    commodity,
                    pair=addon_pair,
                    acquisition=brain._origin_acquisition(origin, commodity),
                )
                if addon is None:
                    continue
                marginal = addon.expected_revenue - addon.total_purchase_cost
                if marginal > 0:
                    addon_candidates += 1
                    if marginal > best_addon:
                        best_addon = marginal
                        best_addon_units = addon.quantity
        record = {
            "turn": self.simulation.current_turn,
            "capacity": self.cargo_capacity,
            "fuel_units": fuel_units,
            "non_fuel_units": non_fuel,
            "free": free,
            "held_kinds": held_kinds,
            "money": self.money,
            "plan_profit": plan.expected_profit,
            "plan_quantity": plan.quantity,
            "addon_candidates": addon_candidates,
            "best_addon": best_addon,
            "best_addon_units": best_addon_units,
        }
    started = _orig_start(self, destination, **kwargs)
    if started and record is not None:
        DEPARTURES.append(record)
    return started


Ship.start_journey = start_journey  # type: ignore[method-assign]


# --- fills -------------------------------------------------------------------------------
def collect_fills(sim: Any, seen: set[int]) -> None:
    for planet in sim.planets:
        for tx in planet.market.transaction_history:
            if tx.transaction_id in seen:
                continue
            seen.add(tx.transaction_id)
            record = ORDER_PATH.get(tx.buy_order_id or "")
            if record is None:
                continue
            ref = record["ref"]
            FUEL_FILLS.append(
                {
                    "turn": tx.turn,
                    "path": record["path"],
                    "quantity": tx.quantity,
                    "price": tx.price,
                    "ref": ref,
                    "ratio": tx.price / ref if ref else math.nan,
                    "premium": tx.quantity * (tx.price - ref) if ref else 0.0,
                    "seller": type(tx.seller).__name__,
                }
            )


def fleet_snapshot(sim: Any) -> dict[str, Any]:
    """Fleet-wide wealth snapshot: money, tank fuel value, hold cargo value.

    Tank fuel is valued at the galaxy-wide reference (``Navigator.
    fuel_value_reference``); hold cargo is valued at each ship's current
    planet's 30-day average price, or 0 for a commodity/planet with no
    believable price signal (including a ship in transit, with no current
    planet). ``fleet_wealth`` is money plus both value components.
    """
    nav = get_navigator(sim)
    fuel_commodity = nav.fuel_commodity()
    fuel_ref = nav.fuel_value_reference() or 0.0
    commodities = [
        c for c in sim.commodity_registry.all_commodities() if c.transportable
    ]
    total_money = 0
    total_fuel_units = 0
    total_fuel_value = 0.0
    total_cargo_value = 0.0
    broke = 0
    never_departed = 0
    total_refuel_stops = 0
    for ship in sim.ships:
        total_money += ship.money
        total_fuel_units += ship.fuel_units
        total_fuel_value += ship.fuel_units * fuel_ref
        if ship.money < 100:
            broke += 1
        if not ship.departure_turns:
            never_departed += 1
        total_refuel_stops += len(getattr(ship, "refuel_stop_turns", []))
        planet = ship.planet
        if planet is None:
            continue
        market = planet.market
        for commodity in commodities:
            if fuel_commodity is not None and commodity.id == fuel_commodity.id:
                continue
            qty = ship.cargo.get_quantity(commodity)
            if qty <= 0:
                continue
            if market.has_price_signal(commodity):
                total_cargo_value += qty * market.get_30_day_average_price(commodity)
    fleet_wealth = total_money + total_fuel_value + total_cargo_value
    return {
        "n_ships": len(sim.ships),
        "money": total_money,
        "fuel_units": total_fuel_units,
        "fuel_ref": fuel_ref,
        "fuel_value": total_fuel_value,
        "cargo_value": total_cargo_value,
        "fleet_wealth": fleet_wealth,
        "broke_ships": broke,
        "never_departed": never_departed,
        "refuel_stops": total_refuel_stops,
    }


def summarize(
    turns: int,
    planets: int,
    ships: int,
    fleet_start: dict[str, Any],
    fleet_end: dict[str, Any],
) -> dict[str, Any]:
    fills_by = defaultdict(lambda: {"units": 0, "credits": 0, "premium": 0.0})
    for f in FUEL_FILLS:
        key = (f["path"], band(f["ratio"]) if not math.isnan(f["ratio"]) else "noref")
        fills_by[key]["units"] += f["quantity"]
        fills_by[key]["credits"] += f["quantity"] * f["price"]
        fills_by[key]["premium"] += f["premium"]
    orders_by_path = Counter(o["path"] for o in FUEL_ORDERS)
    order_units_by_path = Counter()
    for o in FUEL_ORDERS:
        order_units_by_path[o["path"]] += o["quantity"]

    spiked = [
        p
        for p in PLANS
        if not math.isnan(p["ratio"]) and p["ratio"] > FUEL_BUNKER_PREMIUM
    ]
    normal = [
        p
        for p in PLANS
        if not math.isnan(p["ratio"]) and p["ratio"] <= FUEL_BUNKER_PREMIUM
    ]

    def plan_stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        if not group:
            return {"n": 0}
        return {
            "n": len(group),
            "profit_med": median(p["profit"] for p in group),
            "margin_med": round(median(p["margin"] for p in group), 3),
            "fuel_share_med": round(median(p["fuel_share_of_cost"] for p in group), 3),
            "fail_cash_profit<=0": sum(p["profit_cash"] <= 0 for p in group),
            "fail_cash_margin<15%": sum(
                p["margin_cash"] < TradePlan.MIN_MARGIN for p in group
            ),
            "fail_repl_profit<=0": sum(p["profit_repl"] <= 0 for p in group),
            "fail_repl_margin<15%": sum(
                p["margin_repl"] < TradePlan.MIN_MARGIN for p in group
            ),
            "distressed": sum(p["distressed"] for p in group),
            "caps": dict(Counter(p["cap"] for p in group)),
        }

    dep: dict[str, Any] = {"n": len(DEPARTURES)}
    if DEPARTURES:
        util = [
            d["non_fuel_units"] / max(1, d["capacity"] - d["fuel_units"])
            for d in DEPARTURES
        ]
        dep.update(
            {
                "hold_util_med": round(median(util), 3),
                "hold_util_<50%": sum(u < 0.5 for u in util),
                "fuel_share_of_hold_med": round(
                    median(d["fuel_units"] / d["capacity"] for d in DEPARTURES), 3
                ),
                "free_units_med": median(d["free"] for d in DEPARTURES),
                "money_med": median(d["money"] for d in DEPARTURES),
                "held_kinds_2+": sum(d["held_kinds"] >= 2 for d in DEPARTURES),
                "addon_available": sum(d["best_addon"] > 0 for d in DEPARTURES),
                "addon_med_when_available": median(
                    [d["best_addon"] for d in DEPARTURES if d["best_addon"] > 0] or [0]
                ),
                "plan_profit_med": median(d["plan_profit"] for d in DEPARTURES),
                "addon_share_of_plan_profit_med": round(
                    median(
                        [
                            d["best_addon"] / max(1, d["plan_profit"])
                            for d in DEPARTURES
                            if d["best_addon"] > 0
                        ]
                        or [0]
                    ),
                    3,
                ),
                "addon_total": sum(d["best_addon"] for d in DEPARTURES),
                "plan_profit_total": sum(d["plan_profit"] for d in DEPARTURES),
            }
        )

    return {
        "params": {"turns": turns, "planets": planets, "ships": ships},
        "fuel_orders_by_path": {
            k: {"orders": orders_by_path[k], "units": order_units_by_path[k]}
            for k in orders_by_path
        },
        "fuel_fills_by_path_band": {
            f"{k[0]}|{k[1]}": v for k, v in sorted(fills_by.items())
        },
        "plans_all": plan_stats(PLANS),
        "plans_spiked": plan_stats(spiked),
        "plans_normal": plan_stats(normal),
        "departures": dep,
        "fleet": {
            "start_money": fleet_start["money"],
            "end_money": fleet_end["money"],
            "end_fuel_units": fleet_end["fuel_units"],
            "end_fuel_value": fleet_end["fuel_value"],
            "end_cargo_value": fleet_end["cargo_value"],
            "end_wealth": fleet_end["fleet_wealth"],
            "wealth_change": fleet_end["fleet_wealth"] - fleet_start["money"],
            "broke_ships": fleet_end["broke_ships"],
            "never_departed": fleet_end["never_departed"],
            "refuel_stops": fleet_end["refuel_stops"],
        },
    }


def print_tables(result: dict[str, Any]) -> None:
    print("\n== FUEL BUY ORDERS PLACED, BY PATH ==")
    for path, v in sorted(result["fuel_orders_by_path"].items()):
        print(f"  {path:14s} orders={v['orders']:6d} units={v['units']:7d}")
    print("\n== FUEL FILLS BY PATH x PRICE BAND (ratio = price/reference) ==")
    print(f"  {'path|band':26s} {'units':>7s} {'credits':>9s} {'premium':>9s}")
    for key, v in result["fuel_fills_by_path_band"].items():
        print(f"  {key:26s} {v['units']:7d} {v['credits']:9d} {v['premium']:9.0f}")
    for label in ("plans_all", "plans_spiked", "plans_normal"):
        print(f"\n== {label.upper()} ==")
        for k, v in result[label].items():
            print(f"  {k}: {v}")
    print("\n== DEPARTURES ON A LOADED PLAN ==")
    for k, v in result["departures"].items():
        print(f"  {k}: {v}")
    print("\n== FLEET WEALTH ==")
    for k, v in result["fleet"].items():
        print(f"  {k}: {v}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=300)
    parser.add_argument("--planets", type=int, default=12)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("tmp/fuel_path_probe.json"))
    args = parser.parse_args()
    sim = create_and_setup_simulation(
        planets=args.planets, actors=args.actors, makers=2, ships=args.ships
    )
    fleet_start = fleet_snapshot(sim)
    seen: set[int] = set()
    for _ in range(args.turns):
        sim.run_turn()
        collect_fills(sim, seen)
        if sim.current_turn % 50 == 0:
            print(
                f"turn {sim.current_turn}: fuel orders {len(FUEL_ORDERS)} fills {len(FUEL_FILLS)} plans {len(PLANS)} departures {len(DEPARTURES)}",
                flush=True,
            )
    fleet_end = fleet_snapshot(sim)
    result = summarize(args.turns, args.planets, len(sim.ships), fleet_start, fleet_end)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, default=str))
    print_tables(result)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
