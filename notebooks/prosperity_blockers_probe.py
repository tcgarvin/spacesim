"""Per-category blocker decomposition for the six prosperity drives.

In-process Tier-1b probe. For each prosperity category it reports the demand
side (gate pass, standing bids, WTP ceiling and which term binds, money), the
supply side (recipe chosen/run, facilities, input asks, recipe score) and the
outcome (consumption events served, traded volume and price).

    uv run python notebooks/prosperity_blockers_probe.py --turns 450 --planets 12

Two monkeypatches collect what the objects do not expose after the fact:
``ProsperityDrive.tick`` (consumption events vs served) and
``ProcessCommand.execute`` (which recipes actually run). Both call through and
return the original result; both are asserted to have fired.
"""

import argparse
import math
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import BrainCache
from spacesim2.core.actor_brain import GOVERNMENT_WAGE
from spacesim2.core.brains.industrialist import (
    MIN_OUTPUT_DEPTH_UNITS,
    NEVER_TRADED_VALUE_CAP,
    OUTPUT_SALES_HORIZON_RUNS,
    IndustrialistBrain,
)
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.drives.prosperity_drive import (
    PROSPERITY_CATEGORIES,
    GATE_MAX_DEBT,
    GATE_MIN_BUFFER,
    ProsperityDrive,
    prosperity_index,
)
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 450
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
DEFAULT_SAMPLES = (150, 300, 450)
WTP_SAMPLE_PER_PLANET = 12  # actors per planet used for the costly WTP math
INPUT_WATCH = (
    "refined_chemicals",
    "textiles",
    "polymers",
    "glass",
    "electronics",
    "precision_parts",
    "rare_earth",
    "medicine",
)

# Filled by the monkeypatches.
EVENTS: Counter[str] = Counter()  # category -> consumption events
SERVED: Counter[str] = Counter()  # category -> events that found stock
RUNS: Counter[str] = Counter()  # process id -> successful executions
TICK_CALLS = [0]
EXEC_CALLS = [0]


def install_patches() -> None:
    """Wrap tick and execute to record events, service, and runs."""
    orig_tick = ProsperityDrive.tick
    orig_exec = ProcessCommand.execute

    def tick(self: ProsperityDrive, actor: Any) -> Any:
        TICK_CALLS[0] += 1
        before_stock = actor.inventory.get_available_quantity(self.good)
        before_debt = self.metrics.debt
        result = orig_tick(self, actor)
        after_stock = actor.inventory.get_available_quantity(self.good)
        name = self.category.name
        if after_stock < before_stock:
            EVENTS[name] += 1
            SERVED[name] += 1
        elif self.metrics.debt != before_debt:
            # An unserved event is the only thing that moves prosperity debt.
            EVENTS[name] += 1
        return result

    def execute(self: ProcessCommand, actor: Any) -> bool:
        EXEC_CALLS[0] += 1
        ok = orig_exec(self, actor)
        if ok:
            RUNS[self.process_id] += 1
        return ok

    ProsperityDrive.tick = tick  # type: ignore[method-assign]
    ProcessCommand.execute = execute  # type: ignore[method-assign]


def regular_actors(sim: Simulation) -> List[Any]:
    return [
        a for p in sim.planets for a in p.actors if a.actor_type is ActorType.REGULAR
    ]


def pct(num: int, den: int) -> float:
    return 100.0 * num / den if den else 0.0


def med(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def gate_failure(actor: Any) -> Optional[Tuple[str, str]]:
    """First need drive that blocks the prosperity gate, and why."""
    for drive in actor.drives:
        if not drive.WELLBEING:
            continue
        if drive.metrics.debt >= GATE_MAX_DEBT:
            return (drive.metrics.get_name(), "debt")
        if drive.metrics.buffer < GATE_MIN_BUFFER:
            return (drive.metrics.get_name(), "buffer")
    return None


def standing_bids(market: Any, commodity: Any) -> Dict[str, int]:
    """Live buy-order price per actor name for one commodity."""
    out: Dict[str, int] = {}
    for order in market.buy_orders.get(commodity, []):
        if order.cancelled:
            continue
        name = getattr(order.actor, "name", "?")
        out[name] = max(out.get(name, 0), int(order.price))
    return out


def wtp_terms(brain: Any, actor: Any, market: Any, drive: Any, commodity: Any):
    """Return (wtp, welfare_wtp, replacement_cap) for one drive material."""
    cache = BrainCache()
    lam = brain._value_of_money(actor, market, cache)
    if not drive.WELLBEING:
        lam *= brain._surplus_money_discount(actor, market, cache)
    if lam <= 0:
        return 0, 0.0, None
    welfare = drive.marginal_welfare() / lam
    replacement = brain._replacement_cost(actor, market, commodity, cache)
    if replacement is not None:
        replacement *= 1.0 + drive.metrics.debt
    wtp = brain._drive_willingness_to_pay(actor, market, drive, commodity, lam, cache)
    return wtp, welfare, replacement


def window_volume_price(market: Any, commodity: Any, turns: int) -> Tuple[float, float]:
    """Mean units per turn and volume-weighted price over the last N turns."""
    vols = market.volume_history.get(commodity, [])[-turns:]
    prices = market.price_history.get(commodity, [])[-turns:]
    total_v = float(sum(vols))
    if total_v <= 0:
        return 0.0, 0.0
    paired = list(zip(prices, vols))
    vwap = sum(p * v for p, v in paired) / total_v
    return total_v / turns, vwap


def supply_stats(sim: Simulation, commodity: Any) -> Dict[str, Any]:
    """Recipes, facilities, and input availability for one prosperity good."""
    reg = sim.commodity_registry
    procs = sim.process_registry.get_processes_producing(commodity)
    chosen = 0
    industrialists = 0
    for p in sim.planets:
        for a in p.actors:
            if a.actor_type is not ActorType.REGULAR:
                continue
            if not isinstance(a.brain, IndustrialistBrain):
                continue
            industrialists += 1
            if a.brain.chosen_recipe_id in {pr.id for pr in procs}:
                chosen += 1
    fac_owners: Dict[str, int] = {}
    input_ask_planets: Dict[str, int] = {}
    for proc in procs:
        for fac in proc.facilities_required:
            fac_owners[fac.id] = sum(
                1
                for p in sim.planets
                for a in p.actors
                if a.inventory.get_quantity(fac) > 0
            )
        for c in proc.inputs:
            n = 0
            for p in sim.planets:
                _, ask = p.market.get_bid_ask_spread(c)
                if ask is not None:
                    n += 1
            input_ask_planets[c.id] = n
    # Recipe score, positive count over the industrialists on each planet.
    score_pos = 0
    score_vals: List[float] = []
    raw_vals: List[float] = []
    for p in sim.planets:
        inds = [
            a
            for a in p.actors
            if a.actor_type is ActorType.REGULAR
            and isinstance(a.brain, IndustrialistBrain)
        ]
        if not inds:
            continue
        a = inds[0]
        for proc in procs:
            s = a.brain._calculate_recipe_score(
                a, p.market, proc, require_entry_margin=True
            )
            r = a.brain._calculate_recipe_score(
                a, p.market, proc, require_entry_margin=False
            )
            if math.isfinite(s):
                score_vals.append(s)
            if math.isfinite(r):
                raw_vals.append(r)
            if s > 0:
                score_pos += 1
    del reg
    return {
        "procs": [pr.id for pr in procs],
        "chosen": chosen,
        "industrialists": industrialists,
        "fac_owners": fac_owners,
        "input_ask_planets": input_ask_planets,
        "score_pos_planets": score_pos,
        "score_med": med(score_vals),
        "raw_med": med(raw_vals),
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "INF"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def output_tier(brain, actor, market, commodity, eq, memo) -> Tuple[str, float]:
    """Which valuation tier the industrialist uses for one output, and at what price."""
    horizon = max(MIN_OUTPUT_DEPTH_UNITS, math.ceil(eq * OUTPUT_SALES_HORIZON_RUNS))
    bid, _ = market.get_bid_ask_spread(commodity)
    vol = market.get_30_day_average_volume(commodity)
    if bid is not None and vol >= horizon:
        return "1:liquid-bid", float(bid)
    depth_price = market.get_bid_price_at_depth(commodity, horizon)
    if depth_price is not None:
        return f"2:depth(h={horizon})", float(depth_price)
    if market.has_price_signal(commodity):
        return "3:avg-traded", float(market.get_30_day_average_price(commodity))
    ref = float(bid) if bid is not None else float(market.get_avg_price(commodity))
    imputed = brain._imputed_unit_cost(actor, market, commodity, 0, frozenset(), memo)
    if math.isinf(imputed):
        return f"4:never(ref={ref:.0f},imp=INF)", ref
    return f"4:never(ref={ref:.0f})", min(ref, imputed * NEVER_TRADED_VALUE_CAP)


def decompose(actor, market, brain, process) -> Dict[str, Any]:
    """Cost/value decomposition of one recipe for one industrialist."""
    memo: Dict[Any, Any] = {}
    cost = float(GOVERNMENT_WAGE)
    infinite = False
    inputs = []
    for c, q in process.inputs.items():
        unit = brain._imputed_unit_cost(actor, market, c, 1, frozenset(), memo)
        if math.isinf(unit):
            infinite = True
        else:
            cost += unit * q
        inputs.append(f"{c.id}x{q}@{_fmt(unit)}")
    reqs = []
    for fac in process.facilities_required:
        if actor.inventory.has_quantity(fac, 1):
            reqs.append(f"{fac.id}=OWNED")
            continue
        build_id = brain._get_build_process_for_facility(fac)
        build = actor.sim.process_registry.get_process(build_id) if build_id else None
        if build is None:
            infinite = True
            reqs.append(f"{fac.id}=NO_BUILD")
            continue
        build_cost = brain._impute_recipe_cost(
            actor, market, build, 1, frozenset(), memo
        )
        if math.isinf(build_cost):
            infinite = True
            reqs.append(f"{fac.id}=BUILD_INF")
        else:
            cost += build_cost / brain.facility_amortization_horizon
            reqs.append(
                f"{fac.id}=build{build_cost:.0f}/{brain.facility_amortization_horizon}"
            )
    for tool in process.tools_required:
        if actor.inventory.has_quantity(tool, 1):
            reqs.append(f"tool:{tool.id}=OWNED")
            continue
        unit = brain._imputed_unit_cost(actor, market, tool, 1, frozenset(), memo)
        if math.isinf(unit):
            infinite = True
        else:
            cost += unit / 100.0
        reqs.append(f"tool:{tool.id}@{_fmt(unit)}")
    outs = []
    out_value = 0.0
    for c, q in process.outputs.items():
        if not c.transportable:
            out_value += 50.0 * q
            outs.append(f"{c.id}=FACILITY(50)")
            continue
        tier, price = output_tier(brain, actor, market, c, q, memo)
        out_value += price * q
        outs.append(f"{c.id}x{q} {tier} px={price:.0f}")
    return {
        "cost": "INF" if infinite else round(cost, 1),
        "outval": round(out_value, 1),
        "score": round(
            brain._calculate_recipe_score(
                actor, market, process, require_entry_margin=True
            ),
            1,
        ),
        "raw": round(
            brain._calculate_recipe_score(
                actor, market, process, require_entry_margin=False
            ),
            1,
        ),
        "inputs": inputs,
        "reqs": reqs,
        "outs": outs,
    }


def print_decompositions(sim: Simulation, planets: int = 2) -> None:
    """Per-recipe cost/value breakdown on a couple of planets."""
    print(f"\nrecipe decomposition (first industrialist on each of {planets} planets):")
    shown = 0
    for p in sim.planets:
        inds = [
            a
            for a in p.actors
            if a.actor_type is ActorType.REGULAR
            and isinstance(a.brain, IndustrialistBrain)
        ]
        if not inds:
            continue
        a = inds[0]
        print(f"\n--- planet {p.name} actor {a.name} chosen={a.brain.chosen_recipe_id}")
        for cat in PROSPERITY_CATEGORIES:
            good = sim.commodity_registry.get_commodity(cat.commodity_id)
            if good is None:
                continue
            for proc in sim.process_registry.get_processes_producing(good):
                d = decompose(a, p.market, a.brain, proc)
                print(
                    f"  {proc.id:24} cost={str(d['cost']):>8} out={d['outval']:>8} "
                    f"score={d['score']:>8} raw={d['raw']:>9}"
                )
                print(f"      in: {', '.join(d['inputs'])}")
                print(f"      req: {', '.join(d['reqs']) or '-'}")
                print(f"      out: {'; '.join(d['outs'])}")
        shown += 1
        if shown >= planets:
            break


def input_ask_table(sim: Simulation) -> Dict[str, Tuple[int, float]]:
    """Planets with a live ask, and the median ask, for each watched input."""
    reg = sim.commodity_registry
    out: Dict[str, Tuple[int, float]] = {}
    for cid in INPUT_WATCH:
        c = reg.get_commodity(cid)
        if c is None:
            continue
        asks = []
        for p in sim.planets:
            _, ask = p.market.get_bid_ask_spread(c)
            if ask is not None:
                asks.append(float(ask))
        out[cid] = (len(asks), med(asks))
    return out


def sample(sim: Simulation, turn: int, window: int, prev: Counter) -> Counter:
    reg = sim.commodity_registry
    actors = regular_actors(sim)
    n = len(actors)
    print(
        f"\n{'=' * 108}\nTURN {turn}   regular actors={n}  planets={len(sim.planets)}"
    )

    # ---- gate ----
    gated = [a for a in actors if gate_failure(a) is None]
    fails: Counter[str] = Counter()
    for a in actors:
        f = gate_failure(a)
        if f is not None:
            fails[f"{f[0]}:{f[1]}"] += 1
    print(f"gate pass {pct(len(gated), n):.0f}%  ({len(gated)}/{n})")
    print(
        "gate failure first-blocker: "
        + ", ".join(f"{k}={v} ({pct(v, n):.0f}%)" for k, v in fails.most_common(8))
    )

    idx = sorted(prosperity_index(a) for a in actors)
    if idx:
        q = [idx[int(f * (len(idx) - 1))] for f in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0)]
        print(
            "prosperity index p0/p25/p50/p75/p90/p100: "
            + "/".join(f"{v:.2f}" for v in q)
            + f"  mean={statistics.fmean(idx):.3f}"
        )

    print(
        "\ninput asks (planets with ask / median ask): "
        + ", ".join(f"{k}={v[0]}@{v[1]:.0f}" for k, v in input_ask_table(sim).items())
    )

    # ---- per category ----
    header = (
        f"{'category':>10} {'bid%':>5} {'bidP50':>7} {'askP':>5} {'askP50':>7} "
        f"{'wtpP50':>7} {'welfBind%':>9} {'money':>7} {'chosen':>7} {'runs/w':>7} "
        f"{'facOwn':>7} {'cover':>6} {'served%':>8} {'vol/pt':>7} {'price':>6}"
    )
    print("\n" + header)
    detail: List[str] = []
    for cat in PROSPERITY_CATEGORIES:
        good = reg.get_commodity(cat.commodity_id)
        if good is None:
            continue
        # bids among gated actors
        bid_prices: List[float] = []
        gated_with_bid = 0
        gated_by_planet: Dict[str, List[Any]] = defaultdict(list)
        for a in gated:
            gated_by_planet[a.planet.name].append(a)
        for p in sim.planets:
            bids = standing_bids(p.market, good)
            for a in gated_by_planet.get(p.name, []):
                price = bids.get(a.name)
                if price is not None:
                    gated_with_bid += 1
                    bid_prices.append(float(price))
        asks: List[float] = []
        for p in sim.planets:
            _, ask = p.market.get_bid_ask_spread(good)
            if ask is not None:
                asks.append(float(ask))

        # WTP terms on a bounded sample of gated actors
        wtps: List[float] = []
        welfare_binds = 0
        wtp_n = 0
        for p in sim.planets:
            for a in gated_by_planet.get(p.name, [])[:WTP_SAMPLE_PER_PLANET]:
                drive = next(
                    (
                        d
                        for d in a.drives
                        if isinstance(d, ProsperityDrive) and d.category is cat
                    ),
                    None,
                )
                if drive is None:
                    continue
                w, welfare, repl = wtp_terms(a.brain, a, p.market, drive, good)
                wtps.append(float(w))
                wtp_n += 1
                if repl is None or welfare <= repl:
                    welfare_binds += 1

        money = [float(a.money) for a in gated]
        sup = supply_stats(sim, good)
        runs_w = sum(RUNS[pid] - prev[pid] for pid in sup["procs"])
        vol, price = window_volume_price_all(sim, good, window)
        cover = statistics.fmean(
            [
                d.metrics.coverage
                for a in actors
                for d in a.drives
                if isinstance(d, ProsperityDrive) and d.category is cat
            ]
        )
        ev = EVENTS[cat.name] - prev[f"ev:{cat.name}"]
        sv = SERVED[cat.name] - prev[f"sv:{cat.name}"]
        fac_own = ",".join(f"{k}={v}" for k, v in sup["fac_owners"].items()) or "-"
        print(
            f"{cat.name:>10} {pct(gated_with_bid, len(gated)):>5.0f} "
            f"{med(bid_prices):>7.0f} {len(asks):>5} {med(asks):>7.0f} "
            f"{med(wtps):>7.0f} {pct(welfare_binds, wtp_n):>9.0f} "
            f"{med(money):>7.0f} {sup['chosen']:>7} {runs_w:>7} "
            f"{fac_own[:7]:>7} {cover:>6.2f} {pct(sv, ev):>8.0f} "
            f"{vol:>7.2f} {price:>6.0f}"
        )
        detail.append(
            f"  {cat.name:>10} procs={sup['procs']} fac_owners={sup['fac_owners']} "
            f"input_ask_planets={sup['input_ask_planets']} "
            f"score_med={sup['score_med']:.1f} raw_med={sup['raw_med']:.1f} "
            f"planets_with_positive_score={sup['score_pos_planets']}/{len(sim.planets)} "
            f"events={ev} served={sv}"
        )
    print("\nsupply detail:")
    for line in detail:
        print(line)

    snap: Counter = Counter()
    for pid, v in RUNS.items():
        snap[pid] = v
    for cat in PROSPERITY_CATEGORIES:
        snap[f"ev:{cat.name}"] = EVENTS[cat.name]
        snap[f"sv:{cat.name}"] = SERVED[cat.name]
    return snap


def window_volume_price_all(sim: Simulation, commodity: Any, turns: int):
    """Galaxy-wide units per planet-turn and volume-weighted price."""
    total_v = 0.0
    weighted = 0.0
    for p in sim.planets:
        v, px = window_volume_price(p.market, commodity, turns)
        total_v += v
        weighted += v * px
    price = weighted / total_v if total_v > 0 else 0.0
    return total_v / len(sim.planets), price


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    ap.add_argument("--actors", type=int, default=DEFAULT_ACTORS)
    ap.add_argument("--samples", type=str, default="")
    args = ap.parse_args(argv)
    samples = (
        tuple(int(s) for s in args.samples.split(",") if s)
        if args.samples
        else DEFAULT_SAMPLES
    )

    install_patches()
    sim = Simulation()
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    prev: Counter = Counter()
    last = 0
    for t in range(1, args.turns + 1):
        sim.run_turn()
        if t in samples:
            prev = sample(sim, t, max(1, t - last), prev)
            last = t
            if t == max(samples):
                print_decompositions(sim)

    if TICK_CALLS[0] == 0 or EXEC_CALLS[0] == 0:
        raise RuntimeError(
            f"monkeypatch did not land: tick={TICK_CALLS[0]} exec={EXEC_CALLS[0]}"
        )
    print(f"\npatch calls: tick={TICK_CALLS[0]} process_execute={EXEC_CALLS[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
