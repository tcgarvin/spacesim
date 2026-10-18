"""Tier-1b probe: where the fleet's launch-window fuel money goes.

Records, per sampled turn, every ship's money/fuel/distress/plan state and the
local nova_fuel book, plus every fuel buy and fuel sell a ship places, tagged
with the brain branch that placed it.

    uv run python notebooks/fleet_fuel_launch_probe.py --turns 120 --planets 100 \
        --sample-every 10 --warmup 0 --out tmp/fleet_fuel_launch.json

Branches recorded for a fuel order:
  plan_roundtrip  TraderBrain._execute_trade_plan step 1 (round-trip shortfall)
  topup           TraderBrain._opportunistic_fuel_topup (bunker or rationed)
  standing_bid    TraderBrain._post_standing_fuel_bid
  flow_sell       TraderBrain._place_flow_sell_orders (sell side)
"""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, List, Optional

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.market import Market
from spacesim2.core.ship import FUEL_BUNKER_PREMIUM, Ship, ShipStatus, TraderBrain
from spacesim2.core.simulation import Simulation

FUEL_ID = "nova_fuel"

_branch_stack: List[str] = []
_buy_events: List[dict] = []
_sell_events: List[dict] = []
_call_counts: Counter[str] = Counter()
_current_turn = [0]


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 2) if values else None


def _fuel_commodity(sim: Simulation):
    return sim.commodity_registry.get_commodity(FUEL_ID)


def _record(events: List[dict], actor, commodity, quantity: int, price: int) -> None:
    if commodity.id != FUEL_ID or not isinstance(actor, Ship):
        return
    brain = actor.brain
    planet = actor.planet
    bid = ask = None
    if planet is not None:
        bid, ask = planet.market.get_bid_ask_spread(commodity)
    events.append(
        {
            "turn": _current_turn[0],
            "ship": actor.name,
            "branch": _branch_stack[-1] if _branch_stack else "other",
            "quantity": quantity,
            "price": price,
            "money": actor.money,
            "fuel": actor.cargo.get_quantity(commodity),
            "distressed": bool(getattr(brain, "is_distressed", False)),
            "local_bid": bid,
            "local_ask": ask,
            "scarcity_gate": bool(brain._local_fuel_bid_is_scarcity_priced()),
            "fuel_run": bool(brain._fuel_delivery_in_progress()),
            "cheapest_galaxy_ask": brain._nav.cheapest_fuel_ask(),
            "fuel_reference": brain._fuel_value_reference(),
        }
    )


def install_instrumentation() -> None:
    """Wrap the fuel decision sites and the market order calls."""
    orig_buy = Market.place_buy_order
    orig_sell = Market.place_sell_order

    def buy(self, actor, commodity_type, quantity, price, *a, **kw):
        order_id = orig_buy(self, actor, commodity_type, quantity, price, *a, **kw)
        if order_id:
            _record(_buy_events, actor, commodity_type, quantity, price)
        return order_id

    def sell(self, actor, commodity_type, quantity, price, *a, **kw):
        order_id = orig_sell(self, actor, commodity_type, quantity, price, *a, **kw)
        if order_id:
            _record(_sell_events, actor, commodity_type, quantity, price)
        return order_id

    Market.place_buy_order = buy  # type: ignore[method-assign]
    Market.place_sell_order = sell  # type: ignore[method-assign]

    def tag(name: str, method):
        def wrapper(self, *a, **kw):
            _call_counts[name] += 1
            _branch_stack.append(name)
            try:
                return method(self, *a, **kw)
            finally:
                _branch_stack.pop()

        return wrapper

    def topup(self, *a, **kw):
        _call_counts["topup"] += 1
        reference = self._fuel_value_reference()
        planet = self.ship.planet
        ask = None
        if planet is not None:
            fuel = self._fuel_commodity()
            if fuel is not None:
                _, ask = planet.market.get_bid_ask_spread(fuel)
        bunkering = (
            reference is not None
            and ask is not None
            and ask <= reference * FUEL_BUNKER_PREMIUM
        )
        label = "topup_bunker" if bunkering else "topup_ration"
        _branch_stack.append(label)
        try:
            return orig_topup(self, *a, **kw)
        finally:
            _branch_stack.pop()

    orig_topup = TraderBrain._opportunistic_fuel_topup
    TraderBrain._opportunistic_fuel_topup = topup  # type: ignore[method-assign]
    TraderBrain._execute_trade_plan = tag(  # type: ignore[method-assign]
        "plan_roundtrip", TraderBrain._execute_trade_plan
    )
    TraderBrain._post_standing_fuel_bid = tag(  # type: ignore[method-assign]
        "standing_bid", TraderBrain._post_standing_fuel_bid
    )
    TraderBrain._place_flow_sell_orders = tag(  # type: ignore[method-assign]
        "flow_sell", TraderBrain._place_flow_sell_orders
    )


def sample(sim: Simulation) -> dict[str, Any]:
    """One snapshot of the whole fleet."""
    fuel = _fuel_commodity(sim)
    distressed = 0
    monies: List[float] = []
    fuels: List[float] = []
    sell_asks: List[float] = []
    bid_prices: List[float] = []
    bid_vs_ask: List[float] = []
    local_asks: List[float] = []
    ask_planets = 0
    plans = Counter()
    statuses = Counter()
    for ship in sim.ships:
        brain = ship.brain
        statuses[ship.status.name.lower()] += 1
        monies.append(ship.money)
        if fuel is not None:
            fuels.append(ship.cargo.get_quantity(fuel))
        if getattr(brain, "is_distressed", False):
            distressed += 1
        plan = getattr(brain, "_current_plan", None)
        plans["plan" if plan is not None else "no_plan"] += 1
        planet = ship.planet
        if planet is None or fuel is None:
            continue
        bid, ask = planet.market.get_bid_ask_spread(fuel)
        if ask is not None:
            local_asks.append(ask)
            ask_planets += 1
        for order_id in list(ship.active_orders):
            order = planet.market.orders_by_id.get(order_id)
            if order is None or order.commodity_type.id != FUEL_ID:
                continue
            if order.is_buy:
                bid_prices.append(order.price)
                if ask:
                    bid_vs_ask.append(order.price / ask)
            else:
                sell_asks.append(order.price)
    return {
        "turn": sim.current_turn,
        "ships": len(sim.ships),
        "distressed": distressed,
        "median_money": _median(monies),
        "median_fuel": _median(fuels),
        "n_fuel_sell_orders": len(sell_asks),
        "median_sell_ask": _median(sell_asks),
        "n_fuel_bids": len(bid_prices),
        "median_bid": _median(bid_prices),
        "median_bid_over_ask": _median(bid_vs_ask),
        "planets_with_local_ask": ask_planets,
        "median_local_ask": _median(local_asks),
        "statuses": dict(statuses),
        "plans": dict(plans),
    }


def run_probe(
    turns: int, planets: int, actors: int, sample_every: int
) -> dict[str, Any]:
    install_instrumentation()
    sim = create_and_setup_simulation(planets=planets, actors=actors, makers=2)
    samples = []
    for _ in range(turns):
        sim.run_turn()
        _current_turn[0] = sim.current_turn
        if sim.current_turn % sample_every == 0:
            snap = sample(sim)
            samples.append(snap)
            print(
                f"t{snap['turn']:>4} distressed={snap['distressed']:>3} "
                f"money={snap['median_money']} fuel={snap['median_fuel']} "
                f"sells={snap['n_fuel_sell_orders']}@{snap['median_sell_ask']} "
                f"bids={snap['n_fuel_bids']}@{snap['median_bid']} "
                f"ask={snap['median_local_ask']}",
                flush=True,
            )
    if not _call_counts:
        raise RuntimeError("instrumentation never fired; wrappers did not land")
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "ships": len(sim.ships),
        },
        "call_counts": dict(_call_counts),
        "samples": samples,
        "buys": _buy_events,
        "sells": _sell_events,
    }


def _branch_table(events: List[dict], title: str) -> None:
    by_branch: dict[str, List[dict]] = defaultdict(list)
    for event in events:
        by_branch[event["branch"]].append(event)
    print(f"\n{title}: {len(events)} orders")
    print(
        f"{'branch':>16} {'orders':>7} {'units':>7} {'med px':>8} {'med qty':>8} {'distress%':>9}"
    )
    for branch, rows in sorted(by_branch.items(), key=lambda kv: -len(kv[1])):
        units = sum(r["quantity"] for r in rows)
        distress = 100.0 * sum(1 for r in rows if r["distressed"]) / len(rows)
        scarcity = 100.0 * sum(1 for r in rows if r.get("scarcity_gate")) / len(rows)
        run = 100.0 * sum(1 for r in rows if r.get("fuel_run")) / len(rows)
        print(
            f"{branch:>16} {len(rows):>7} {units:>7} "
            f"{_median([r['price'] for r in rows]):>8} "
            f"{_median([r['quantity'] for r in rows]):>8} {distress:>8.0f}%"
            f" scarcity={scarcity:>3.0f}% fuelrun={run:>3.0f}%"
        )


def print_report(result: dict[str, Any]) -> None:
    print(
        f"\n{'turn':>5} {'distr':>6} {'money':>9} {'fuel':>6} {'sells':>6} {'ask':>8} {'bids':>6} {'bidpx':>8} {'locask':>8}"
    )
    for snap in result["samples"]:
        print(
            f"{snap['turn']:>5} {snap['distressed']:>6} {str(snap['median_money']):>9} "
            f"{str(snap['median_fuel']):>6} {snap['n_fuel_sell_orders']:>6} "
            f"{str(snap['median_sell_ask']):>8} {snap['n_fuel_bids']:>6} "
            f"{str(snap['median_bid']):>8} {str(snap['median_local_ask']):>8}"
        )
    _branch_table(result["buys"], "fuel BUY orders by branch")
    _branch_table(result["sells"], "fuel SELL orders by branch")
    print("\nwrapper call counts:", result["call_counts"])


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=120)
    parser.add_argument("--planets", type=int, default=100)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--sample-every", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_probe(args.turns, args.planets, args.actors, args.sample_every)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_report(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
