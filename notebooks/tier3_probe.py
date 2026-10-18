"""Why is no tier-2/3 recipe ever selected? Score decomposition probe."""

import math
from collections import Counter

from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import GOVERNMENT_WAGE
from spacesim2.core.brains.industrialist import (
    MIN_OUTPUT_DEPTH_UNITS,
    NEVER_TRADED_VALUE_CAP,
    OUTPUT_SALES_HORIZON_RUNS,
    IndustrialistBrain,
)
from spacesim2.core.simulation import Simulation

TURNS = 200
SAMPLES = (100, 150, 200)
PLANETS = 12
ACTORS = 100
TARGETS = [
    "make_prefab_housing",
    "make_luxury_goods",
    "make_computers",
    "make_advanced_medicine",
]
FACILITIES = [
    "smelting_facility",
    "metalworking_facility",
    "chemistry_lab",
    "textile_mill",
    "electronics_workshop",
    "precision_forge",
    "advanced_factory",
]
WATCH = [
    "prefab_housing",
    "luxury_goods",
    "computers",
    "advanced_medicine",
    "polymers",
    "glass",
    "rare_earth",
    "rare_earth_ore",
    "textiles",
    "electronics",
    "precision_parts",
    "refined_chemicals",
    "medicine",
    "chemicals",
    "silica",
    "simple_building_materials",
]


def f(v):
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "INF"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def tier_of(brain, actor, market, commodity, eq, memo):
    horizon = max(MIN_OUTPUT_DEPTH_UNITS, math.ceil(eq * OUTPUT_SALES_HORIZON_RUNS))
    bid, _ = market.get_bid_ask_spread(commodity)
    vol = market.get_30_day_average_volume(commodity)
    if bid is not None and vol >= horizon:
        return "1:liquid-bid", float(bid)
    dp = market.get_bid_price_at_depth(commodity, horizon)
    if dp is not None:
        return f"2:depth(h={horizon})", float(dp)
    if market.has_price_signal(commodity):
        return "3:avg-traded", market.get_30_day_average_price(commodity)
    ref = float(bid) if bid is not None else float(market.get_avg_price(commodity))
    imp = brain._imputed_unit_cost(actor, market, commodity, 0, frozenset(), memo)
    if math.isinf(imp):
        return f"4:never(ref={ref:.0f},imp=INF)", ref
    return f"4:never(ref={ref:.0f},imp={imp:.0f})", min(
        ref, imp * NEVER_TRADED_VALUE_CAP
    )


def decompose(actor, market, brain, process):
    memo = {}
    cost = float(GOVERNMENT_WAGE)
    inf = False
    inputs = []
    for c, q in process.inputs.items():
        u = brain._imputed_unit_cost(actor, market, c, 1, frozenset(), memo)
        if math.isinf(u):
            inf = True
        else:
            cost += u * q
        inputs.append(f"{c.id}x{q}@{f(u)}")
    facs = []
    for fac in process.facilities_required:
        if actor.inventory.has_quantity(fac, 1):
            facs.append(f"{fac.id}=OWNED")
            continue
        bpid = brain._get_build_process_for_facility(fac)
        bp = actor.sim.process_registry.get_process(bpid) if bpid else None
        if not bp:
            inf = True
            facs.append(f"{fac.id}=NO_BUILD")
            continue
        bc = brain._impute_recipe_cost(actor, market, bp, 1, frozenset(), memo)
        if math.isinf(bc):
            inf = True
            facs.append(f"{fac.id}=BUILD_INF")
        else:
            cost += bc / brain.facility_amortization_horizon
            facs.append(f"{fac.id}=build{bc:.0f}/{brain.facility_amortization_horizon}")
    for tool in process.tools_required:
        if actor.inventory.has_quantity(tool, 1):
            facs.append(f"tool:{tool.id}=OWNED")
            continue
        u = brain._imputed_unit_cost(actor, market, tool, 1, frozenset(), memo)
        if math.isinf(u):
            inf = True
        else:
            cost += u / 100.0
        facs.append(f"tool:{tool.id}@{f(u)}")

    outs = []
    outval = 0.0
    for c, q in process.outputs.items():
        if not c.transportable:
            outval += 50.0 * q
            outs.append(f"{c.id}=FACILITY(50)")
            continue
        t, px = tier_of(brain, actor, market, c, q, memo)
        outval += px * q
        outs.append(f"{c.id}x{q} {t} px={f(px)}")
    score = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=True
    )
    raw = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=False
    )
    return dict(
        cost=("INF" if inf else round(cost, 1)),
        outval=round(outval, 1),
        score=round(score, 1),
        raw=round(raw, 1),
        inputs=inputs,
        facs=facs,
        outs=outs,
    )


def sample(sim, turn):
    reg = sim.commodity_registry
    preg = sim.process_registry
    print(f"\n{'=' * 76}\nTURN {turn}")
    # galaxy facility ownership + stock
    fac_counts = {}
    for fid in FACILITIES:
        c = reg.get_commodity(fid)
        fac_counts[fid] = sum(
            1 for p in sim.planets for a in p.actors if a.inventory.get_quantity(c) > 0
        )
    print("facility owners galaxy-wide:", fac_counts)
    print(
        f"{'commodity':>28} {'stock':>7} {'traded_ever':>11} {'bid':>6} {'ask':>6} {'vol30':>6}"
    )
    for cid in WATCH:
        c = reg.get_commodity(cid)
        stock = sum(a.inventory.get_quantity(c) for p in sim.planets for a in p.actors)
        ever = sum(1 for p in sim.planets if p.market.has_price_signal(c))
        bids, asks, vol = [], [], 0.0
        for p in sim.planets:
            m = p.market
            b, a_ = m.get_bid_ask_spread(c)
            if b is not None:
                bids.append(b)
            if a_ is not None:
                asks.append(a_)
            vol += m.get_30_day_average_volume(c)
        print(
            f"{cid:>28} {stock:>7} {ever:>11} "
            f"{max(bids, default=0):>6} {min(asks, default=0):>6} {vol:>6.1f}"
        )

    chosen = Counter()
    for p in sim.planets:
        for a in p.actors:
            if isinstance(a.brain, IndustrialistBrain):
                chosen[a.brain.chosen_recipe_id] += 1
    print("chosen recipes (top 8):", chosen.most_common(8))

    if turn != SAMPLES[-1]:
        return
    # detailed decomposition on 3 planets, 1 industrialist each
    shown = 0
    for p in sim.planets:
        inds = [
            a
            for a in p.actors
            if isinstance(a.brain, IndustrialistBrain)
            and a.actor_type != ActorType.SERVICE
        ]
        if not inds:
            continue
        a = inds[0]
        print(f"\n--- planet {p.name} actor {a.name} chosen={a.brain.chosen_recipe_id}")
        cands = list(TARGETS)
        if a.brain.chosen_recipe_id:
            cands.append(a.brain.chosen_recipe_id)
        for pid in cands:
            proc = preg.get_process(pid)
            if not proc:
                continue
            d = decompose(a, p.market, a.brain, proc)
            print(
                f"  {pid:24} cost={str(d['cost']):>8} out={d['outval']:>8} "
                f"score={d['score']:>8} raw={d['raw']:>9}"
            )
            print(f"      in: {', '.join(d['inputs'])}")
            print(f"      fac: {', '.join(d['facs']) or '-'}")
            print(f"      out: {'; '.join(d['outs'])}")
        shown += 1
        if shown >= 3:
            break


def main():
    sim = Simulation()
    sim.setup_simple(
        num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
    )
    for t in range(1, TURNS + 1):
        sim.run_turn()
        if t in SAMPLES:
            sample(sim, t)


if __name__ == "__main__":
    main()
