"""Why food health fell when the prosperity surplus-money discount landed.

In-process Tier-1b probe. Runs the sim twice (once per invocation) and
records, per window, the things that discriminate the candidate mechanisms:

  a) producer reallocation      recipe choice counts, food/biomass runs
  b) buyers priced out of food  money quantiles, food bids, unfilled bids,
                                prosperity spend per actor
  c) processed_food fallback    basic eats vs quality eats vs missed meals
  d) food consumed as an input  process_food no longer takes food; it runs
                                40 biomass + 1 chemicals -> 60 processed_food

    uv run python notebooks/food_regression_probe.py --turns 450 --tag after
    uv run python notebooks/food_regression_probe.py --turns 450 --tag before \
        --no-surplus-discount

``--no-surplus-discount`` restores the pre-f17e74a lambda by forcing
``ActorBrain._surplus_money_discount`` to 1.0, so both arms run from the same
working tree with no worktree checkout.

Three monkeypatches, all call through and all asserted to have fired:
``ProcessCommand.execute`` (runs per recipe), ``FoodDrive.tick`` (meal
outcome), ``Market._execute_transaction`` (spend per commodity per buyer).
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from typing import Any, Dict, List

from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.drives.food_drive import FoodDrive
from spacesim2.core.drives.prosperity_drive import (
    GATE_MAX_DEBT,
    GATE_MIN_BUFFER,
    PROSPERITY_CATEGORIES,
)
from spacesim2.core.market import Market
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 450
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
SAMPLES = (150, 300, 450)
FOOD_PROCS = ("gather_biomass", "make_food", "process_food")
PROSPERITY_GOODS = tuple(c.commodity_id for c in PROSPERITY_CATEGORIES)

RUNS: Counter = Counter()
MEALS: Counter = Counter()  # basic / quality / missed
SPEND: Counter = Counter()  # commodity id -> credits paid by regular actors
UNITS: Counter = Counter()  # commodity id -> units bought by regular actors
CALLS = Counter()


def install_patches(disable_discount: bool) -> None:
    orig_exec = ProcessCommand.execute
    orig_tick = FoodDrive.tick
    orig_txn = Market._execute_transaction

    def execute(self: ProcessCommand, actor: Any) -> bool:
        CALLS["exec"] += 1
        ok = orig_exec(self, actor)
        if ok:
            RUNS[self.process_id] += 1
        return ok

    def tick(self: FoodDrive, actor: Any) -> Any:
        CALLS["tick"] += 1
        basic = actor.inventory.get_available_quantity(self.food_commodity)
        result = orig_tick(self, actor)
        if not actor.food_consumed_this_turn:
            MEALS["missed"] += 1
        elif basic > 0:
            MEALS["basic"] += 1
        else:
            MEALS["quality"] += 1
        return result

    def execute_transaction(self: Market, buyer, seller, commodity_type, *a, **kw):
        CALLS["txn"] += 1
        orig_txn(self, buyer, seller, commodity_type, *a, **kw)
        txn = self.transaction_history[-1]
        if getattr(buyer, "actor_type", None) is ActorType.REGULAR:
            SPEND[commodity_type.id] += txn.total_amount
            UNITS[commodity_type.id] += txn.quantity

    ProcessCommand.execute = execute  # type: ignore[method-assign]
    FoodDrive.tick = tick  # type: ignore[method-assign]
    Market._execute_transaction = execute_transaction  # type: ignore[method-assign]

    if disable_discount:
        ActorBrain._surplus_money_discount = (  # type: ignore[method-assign]
            lambda self, actor, market, cache=None: 1.0
        )


def regular_actors(sim: Simulation) -> List[Any]:
    return [
        a for p in sim.planets for a in p.actors if a.actor_type is ActorType.REGULAR
    ]


def quantiles(values: List[float]) -> List[float]:
    if not values:
        return [0.0] * 5
    s = sorted(values)
    return [s[int(f * (len(s) - 1))] for f in (0.1, 0.25, 0.5, 0.75, 0.9)]


def food_drive(actor: Any) -> Any:
    return next(d for d in actor.drives if isinstance(d, FoodDrive))


def market_stats(sim: Simulation, cid: str) -> Dict[str, float]:
    c = sim.commodity_registry.get_commodity(cid)
    if c is None:
        return {}
    asks, bids, vols, prices = [], [], [], []
    for p in sim.planets:
        bid, ask = p.market.get_bid_ask_spread(c)
        if ask is not None:
            asks.append(float(ask))
        if bid is not None:
            bids.append(float(bid))
        vols.append(p.market.get_30_day_average_volume(c))
        if p.market.has_price_signal(c):
            prices.append(p.market.get_30_day_average_price(c))
    return {
        "ask_planets": len(asks),
        "ask_med": statistics.median(asks) if asks else 0.0,
        "bid_planets": len(bids),
        "bid_med": statistics.median(bids) if bids else 0.0,
        "vol_per_planet": statistics.fmean(vols) if vols else 0.0,
        "price_med": statistics.median(prices) if prices else 0.0,
    }


def standing_food_bidders(sim: Simulation) -> Dict[str, Any]:
    """Regular actors with a live food buy order, and their bid prices."""
    food = sim.commodity_registry.get_commodity("food")
    names: set = set()
    prices: List[float] = []
    for p in sim.planets:
        for order in p.market.buy_orders.get(food, []):
            if order.cancelled:
                continue
            if getattr(order.actor, "actor_type", None) is ActorType.REGULAR:
                names.add(order.actor.name)
                prices.append(float(order.price))
    return {
        "bidders": len(names),
        "bid_med": statistics.median(prices) if prices else 0.0,
    }


def sample(sim: Simulation, turn: int, prev: Counter) -> Counter:
    actors = regular_actors(sim)
    n = len(actors)
    fds = [food_drive(a) for a in actors]
    hungry = [a for a, d in zip(actors, fds) if d.metrics.debt > 0.2]
    money = [float(a.money) for a in actors]

    recipes: Counter = Counter()
    for a in actors:
        rid = getattr(a.brain, "chosen_recipe_id", None)
        recipes[rid or "-none-"] += 1

    gate_fail: Counter = Counter()
    for a in actors:
        for d in a.drives:
            if not d.WELLBEING:
                continue
            if d.metrics.debt >= GATE_MAX_DEBT:
                gate_fail[f"{d.metrics.get_name()}:debt"] += 1
                break
            if d.metrics.buffer < GATE_MIN_BUFFER:
                gate_fail[f"{d.metrics.get_name()}:buffer"] += 1
                break

    win_runs = {pid: RUNS[pid] - prev[pid] for pid in FOOD_PROCS}
    win_meals = {k: MEALS[k] - prev[f"m:{k}"] for k in ("basic", "quality", "missed")}
    win_spend = {
        cid: SPEND[cid] - prev[f"s:{cid}"]
        for cid in ("food", "biomass") + PROSPERITY_GOODS
    }
    win_units = {
        cid: UNITS[cid] - prev[f"u:{cid}"] for cid in ("food",) + PROSPERITY_GOODS
    }

    out = {
        "turn": turn,
        "food_health_mean": round(statistics.fmean(d.metrics.health for d in fds), 3),
        "food_debt_mean": round(statistics.fmean(d.metrics.debt for d in fds), 3),
        "food_buffer_mean": round(statistics.fmean(d.metrics.buffer for d in fds), 3),
        "hungry_n": len(hungry),
        "hungry_money_med": round(
            statistics.median([float(a.money) for a in hungry]) if hungry else 0.0, 1
        ),
        "money_q": [round(v, 0) for v in quantiles(money)],
        "pantry_med": statistics.median(
            [d.pantry_units(a) for a, d in zip(actors, fds)]
        ),
        "gate_fail": dict(gate_fail.most_common(6)),
        "recipes_top": dict(recipes.most_common(12)),
        "runs_window": win_runs,
        "meals_window": win_meals,
        "spend_window": {k: round(v) for k, v in win_spend.items()},
        "units_window": {k: round(v) for k, v in win_units.items()},
        "food_bids": standing_food_bidders(sim),
        "market": {
            cid: {k: round(v, 1) for k, v in market_stats(sim, cid).items()}
            for cid in ("food", "biomass", "processed_food")
        },
        "n_actors": n,
    }
    print(json.dumps(out, sort_keys=True))

    snap: Counter = Counter()
    for pid in FOOD_PROCS:
        snap[pid] = RUNS[pid]
    for k in ("basic", "quality", "missed"):
        snap[f"m:{k}"] = MEALS[k]
    for cid in ("food", "biomass") + PROSPERITY_GOODS:
        snap[f"s:{cid}"] = SPEND[cid]
        snap[f"u:{cid}"] = UNITS[cid]
    return snap


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    ap.add_argument("--actors", type=int, default=DEFAULT_ACTORS)
    ap.add_argument("--tag", type=str, default="run")
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--no-surplus-discount", action="store_true")
    args = ap.parse_args(argv)

    install_patches(args.no_surplus_discount)
    sim = Simulation()
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    print(f"### arm={args.tag} discount_disabled={args.no_surplus_discount}")
    prev: Counter = Counter()
    samples: List[Dict[str, Any]] = []
    for t in range(1, args.turns + 1):
        sim.run_turn()
        if t in SAMPLES or t == args.turns:
            prev = sample(sim, t, prev)

    if not CALLS["exec"] or not CALLS["tick"] or not CALLS["txn"]:
        raise RuntimeError(f"monkeypatch did not land: {dict(CALLS)}")
    print(f"### patch calls {dict(CALLS)}")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"tag": args.tag, "samples": samples}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
