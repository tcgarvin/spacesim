"""Why does no local industrialist enter make_medicine on medicine-starved planets?

Builds its own sim so brain internals, order books and planet attributes are
all visible at once. Run directly:

    uv run python notebooks/starved_medicine_probe.py

Identifies starved planets (no medicine asks + high health deprivation) and
dumps, per planet: population and recipe mix, facility/material ownership,
planet attributes, the chain order books, and a term-by-term decomposition of
_calculate_recipe_score for the medicine chain for a sample of local
industrialists, including which tier of _output_unit_value fired.
"""

import math
from collections import Counter, defaultdict

from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import GOVERNMENT_WAGE, MAX_IMPUTE_DEPTH
from spacesim2.core.brains.industrialist import (
    MIN_OUTPUT_DEPTH_UNITS,
    NEVER_TRADED_VALUE_CAP,
    OUTPUT_SALES_HORIZON_RUNS,
    IndustrialistBrain,
)
from spacesim2.core.simulation import Simulation

TURNS = 500
PLANETS = 16
ACTORS = 100
SAMPLE_INDUSTRIALISTS = 3
MAX_STARVED_DETAIL = 4

CHAIN = [
    "medicine",
    "refined_chemicals",
    "chemicals",
    "chemistry_lab",
    "glass",
    "silica",
    "simple_building_materials",
    "simple_tools",
    "biomass",
    "smelting_facility",
]
CHAIN_RECIPES = [
    "gather_biomass",
    "make_medicine",
    "refine_chemicals",
    "make_chemicals",
    "build_chemistry_lab",
    "make_glass",
    "mine_silica",
    "build_smelting_facility",
    "make_simple_building_materials",
]
ATTRS = ["biomass", "silica", "simple_building_materials", "common_metal_ore", "wood"]


def f(v):
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "INF"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def p(*a):
    print(*a, flush=True)


def output_tier(brain, actor, market, commodity, expected_quantity, memo):
    """Replicate _output_unit_value and report which branch fired."""
    horizon = max(
        MIN_OUTPUT_DEPTH_UNITS,
        math.ceil(expected_quantity * OUTPUT_SALES_HORIZON_RUNS),
    )
    bid, _ = market.get_bid_ask_spread(commodity)
    vol = market.get_30_day_average_volume(commodity)
    if bid is not None and vol >= horizon:
        return "1:liquid-bid", float(bid), horizon, vol
    depth_price = market.get_bid_price_at_depth(commodity, horizon)
    if depth_price is not None:
        return "2:depth", float(depth_price), horizon, vol
    if market.has_price_signal(commodity):
        return "3:avg-traded", market.get_30_day_average_price(commodity), horizon, vol
    reference = (
        float(bid) if bid is not None else float(market.get_avg_price(commodity))
    )
    imputed = brain._imputed_unit_cost(actor, market, commodity, 0, frozenset(), memo)
    if math.isinf(imputed):
        return "4:never-traded(ref)", reference, horizon, vol
    return (
        f"4:never-traded(cap ref={reference:.0f} imp={imputed:.1f})",
        min(reference, imputed * NEVER_TRADED_VALUE_CAP),
        horizon,
        vol,
    )


def breakdown(actor, market, brain, process, reg):
    """Term-by-term cost/value decomposition mirroring _calculate_recipe_score."""
    memo = {}
    total = float(GOVERNMENT_WAGE)
    parts = [f"labor={GOVERNMENT_WAGE}"]
    infinite = False
    for c, q in process.inputs.items():
        u = brain._imputed_unit_cost(actor, market, c, 1, frozenset(), memo)
        if math.isinf(u):
            infinite = True
        else:
            total += u * q
        parts.append(f"{c.id}x{q}@{f(u)}")
    for tool in process.tools_required:
        if actor.inventory.has_quantity(tool, 1):
            parts.append(f"tool:{tool.id}=OWNED")
            continue
        u = brain._imputed_unit_cost(actor, market, tool, 1, frozenset(), memo)
        if math.isinf(u):
            infinite = True
        else:
            total += u / 100.0
        parts.append(f"tool:{tool.id}/100={f(u / 100.0 if not math.isinf(u) else u)}")
    for fac in process.facilities_required:
        if actor.inventory.has_quantity(fac, 1):
            parts.append(f"fac:{fac.id}=OWNED")
            continue
        bpid = brain._get_build_process_for_facility(fac)
        bp = actor.sim.process_registry.get_process(bpid) if bpid else None
        if not bp:
            infinite = True
            parts.append(f"fac:{fac.id}=NO_BUILD")
            continue
        bc = brain._impute_recipe_cost(actor, market, bp, 1, frozenset(), memo)
        if math.isinf(bc):
            infinite = True
            parts.append(f"fac:{fac.id} build=INF")
        else:
            amort = bc / brain.facility_amortization_horizon
            total += amort
            parts.append(
                f"fac:{fac.id} build={f(bc)}/{brain.facility_amortization_horizon}"
                f"={f(amort)}"
            )

    attr_mod = 1.0
    if process.resource_attribute and actor.planet:
        attr_mod = actor.planet.attributes.get_availability(
            process.resource_attribute.commodity
        )

    out_val = 0.0
    out_parts = []
    zero_priced = False
    for c, q in process.outputs.items():
        if not c.transportable:
            out_val += 50.0 * q
            out_parts.append(f"{c.id}x{q}=FACILITY_NOTIONAL(50)")
            continue
        eq = q * attr_mod
        tier, price, horizon, vol = output_tier(brain, actor, market, c, eq, memo)
        if price <= 0:
            zero_priced = True
        out_val += price * eq
        out_parts.append(
            f"{c.id}x{q}(eq={eq:.2f}) tier={tier} px={f(price)} "
            f"horizon={horizon} vol30={vol:.1f} => {f(price * eq)}"
        )

    raw = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=False
    )
    entry = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=True
    )
    cost_s = "INF" if infinite else f(total)
    margin_ok = (not infinite) and out_val >= total * 1.2
    can = actor.can_execute_process(process.id)
    missing = []
    if not can:
        for c, q in process.requirements:
            if not actor.inventory.has_quantity(c, q):
                missing.append(f"{c.id}x{q}(have {actor.inventory.get_quantity(c)})")
    p(
        f"    {process.id:28} cost={cost_s:>8} out={f(out_val):>8} "
        f"raw={f(raw):>8} entry={f(entry):>8} margin_ok={margin_ok} "
        f"can_exec={can}"
    )
    p(f"        cost: {' + '.join(parts)}")
    for op in out_parts:
        p(f"        out:  {op}  (attr_mod={attr_mod:.2f})")
    if missing:
        p(f"        MISSING: {', '.join(missing)}")


def main():
    sim = Simulation()
    sim.setup_simple(
        num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
    )
    for _ in range(TURNS):
        sim.run_turn()

    reg = sim.commodity_registry
    proc_reg = sim.process_registry
    com = {c: reg.get_commodity(c) for c in CHAIN}
    med = com["medicine"]

    p(
        f"=== {PLANETS} planets x {ACTORS} actors, {TURNS} turns, MAX_IMPUTE_DEPTH={MAX_IMPUTE_DEPTH} ==="
    )

    rows = []
    for planet in sim.planets:
        mkt = planet.market
        regulars = [a for a in planet.actors if a.actor_type != ActorType.SERVICE]
        mms = [a for a in planet.actors if a.actor_type == ActorType.SERVICE]
        buys = [o for o in mkt.buy_orders.get(med, []) if not o.cancelled]
        sells = [o for o in mkt.sell_orders.get(med, []) if not o.cancelled]
        bid = max((o.price for o in buys), default=0)
        ask = min((o.price for o in sells), default=0)
        stock = sum(a.inventory.get_quantity(med) for a in planet.actors)
        depr = 0
        for a in regulars:
            for d in a.drives:
                if d.metrics.get_name() == "health" and d.metrics.debt > 0.3:
                    depr += 1
        lab = reg.get_commodity("chemistry_lab")
        smelt = reg.get_commodity("smelting_facility")
        rows.append(
            dict(
                name=planet.name,
                bid=bid,
                bidq=sum(o.quantity for o in buys),
                ask=ask,
                askq=sum(o.quantity for o in sells),
                stock=stock,
                depr=100.0 * depr / max(1, len(regulars)),
                labs=sum(1 for a in planet.actors if a.inventory.get_quantity(lab) > 0),
                smelters=sum(
                    1 for a in planet.actors if a.inventory.get_quantity(smelt) > 0
                ),
                silica=planet.attributes.silica,
                biomass=planet.attributes.biomass,
                sbm=planet.attributes.simple_building_materials,
                cmo=planet.attributes.common_metal_ore,
                n=len(regulars),
                mm=len(mms),
                planet=planet,
                vol30=mkt.get_30_day_average_volume(med),
            )
        )

    p("\n--- per-planet medicine + chain state ---")
    p(
        f"{'planet':>12} {'bid':>5} {'bidQ':>5} {'ask':>5} {'askQ':>5} {'stock':>6} "
        f"{'depr%':>6} {'labs':>5} {'smelt':>6} {'silica':>7} {'biomass':>8} "
        f"{'sbm':>5} {'cmo':>5} {'vol30':>6}"
    )
    for r in sorted(rows, key=lambda r: -r["depr"]):
        p(
            f"{r['name']:>12} {r['bid']:>5} {r['bidq']:>5} {r['ask']:>5} {r['askq']:>5} "
            f"{r['stock']:>6} {r['depr']:>6.0f} {r['labs']:>5} {r['smelters']:>6} "
            f"{r['silica']:>7.2f} {r['biomass']:>8.2f} {r['sbm']:>5.2f} "
            f"{r['cmo']:>5.2f} {r['vol30']:>6.1f}"
        )

    starved = [r for r in rows if r["askq"] == 0 and r["depr"] >= 20]
    if not starved:
        starved = sorted(rows, key=lambda r: -r["depr"])[:MAX_STARVED_DETAIL]
    starved = sorted(starved, key=lambda r: -r["depr"])[:MAX_STARVED_DETAIL]
    p(f"\nstarved planets: {[r['name'] for r in starved]}")

    # Galaxy-wide correlation: silica vs local lab count.
    p("\n--- galaxy: silica attr vs local chemistry_lab owners ---")
    lab = reg.get_commodity("chemistry_lab")
    lo = [r for r in rows if r["silica"] < 0.35]
    hi = [r for r in rows if r["silica"] >= 0.35]
    for label, grp in (("silica<0.35", lo), ("silica>=0.35", hi)):
        if not grp:
            continue
        p(
            f"  {label:12} planets={len(grp):>2} mean_labs="
            f"{sum(g['labs'] for g in grp) / len(grp):>5.1f} "
            f"mean_depr%={sum(g['depr'] for g in grp) / len(grp):>5.1f} "
            f"mean_stock={sum(g['stock'] for g in grp) / len(grp):>7.1f}"
        )

    for r in starved:
        planet = r["planet"]
        mkt = planet.market
        p("\n" + "=" * 78)
        p(
            f"STARVED PLANET {planet.name}: depr={r['depr']:.0f}% medbid={r['bid']} "
            f"depth={r['bidq']} stock={r['stock']} labs={r['labs']} "
            f"smelters={r['smelters']}"
        )
        p(
            "  attributes: "
            + " ".join(
                f"{a}={planet.attributes.get_availability(a):.2f}" for a in ATTRS
            )
        )

        regulars = [a for a in planet.actors if a.actor_type != ActorType.SERVICE]
        inds = [a for a in regulars if isinstance(a.brain, IndustrialistBrain)]
        cols = [a for a in regulars if not isinstance(a.brain, IndustrialistBrain)]
        p(f"  population: {len(inds)} industrialists, {len(cols)} colonists")
        chosen = Counter(getattr(a.brain, "chosen_recipe_id", None) for a in inds)
        p("  industrialist chosen_recipe_id:")
        for rid, n in chosen.most_common(10):
            p(f"    {rid!s:30} {n}")

        blocked = Counter()
        for a in inds:
            rid = getattr(a.brain, "chosen_recipe_id", None)
            if rid and not a.can_execute_process(rid):
                pr = proc_reg.get_process(rid)
                miss = [
                    c.id
                    for c, q in pr.requirements
                    if not a.inventory.has_quantity(c, q)
                ]
                blocked[(rid, tuple(sorted(miss)))] += 1
        p("  industrialists blocked on chosen recipe (recipe, missing): count")
        for (rid, miss), n in blocked.most_common(8):
            p(f"    {rid:26} missing={','.join(miss):40} {n}")

        p("  local stock of chain goods (all planet actors):")
        for cid, c in com.items():
            if not c:
                continue
            tot = sum(a.inventory.get_quantity(c) for a in planet.actors)
            holders = sum(1 for a in planet.actors if a.inventory.get_quantity(c) > 0)
            p(f"    {cid:28} units={tot:>6} holders={holders:>4}")

        p("  local order book / price signal:")
        for cid, c in com.items():
            if not c or not c.transportable:
                continue
            bid, ask = mkt.get_bid_ask_spread(c)
            p(
                f"    {cid:28} bid={f(bid):>6} ask={f(ask):>6} "
                f"avg={mkt.get_avg_price(c):>5} signal={mkt.has_price_signal(c)} "
                f"vol30={mkt.get_30_day_average_volume(c):>6.1f} "
                f"scarcity={mkt.scarcity_pressure_for(c):.2f}"
            )

        # Market maker medicine behavior.
        mms = [a for a in planet.actors if a.actor_type == ActorType.SERVICE]
        for mm in mms:
            orders = mkt.get_actor_orders(mm)
            mb = [
                (o.price, o.quantity) for o in orders["buy"] if o.commodity_type == med
            ]
            ms = [
                (o.price, o.quantity) for o in orders["sell"] if o.commodity_type == med
            ]
            p(
                f"  MM {mm.name}: money={mm.money} med_stock="
                f"{mm.inventory.get_quantity(med)} med_bids={mb} med_asks={ms}"
            )

        # Who is resting the medicine bids?
        buys = sorted(
            [o for o in mkt.buy_orders.get(med, []) if not o.cancelled],
            key=lambda o: -o.price,
        )
        kinds = defaultdict(lambda: [0, 0])
        for o in buys:
            k = (
                "market_maker"
                if o.actor.actor_type == ActorType.SERVICE
                else (
                    "industrialist"
                    if isinstance(o.actor.brain, IndustrialistBrain)
                    else "colonist"
                )
            )
            kinds[k][0] += o.quantity
            kinds[k][1] = max(kinds[k][1], o.price)
        p("  medicine resting bids by kind (qty, max px): " + str(dict(kinds)))

        sample = sorted(inds, key=lambda a: -a.money)[:SAMPLE_INDUSTRIALISTS]
        for actor in sample:
            brain = actor.brain
            p(
                f"\n  --- industrialist {actor.name} money={actor.money} "
                f"chosen={brain.chosen_recipe_id} "
                f"fac_horizon={brain.facility_amortization_horizon} ---"
            )
            recipes = list(CHAIN_RECIPES)
            if brain.chosen_recipe_id and brain.chosen_recipe_id not in recipes:
                recipes.append(brain.chosen_recipe_id)
            for rid in recipes:
                proc = proc_reg.get_process(rid)
                if proc:
                    breakdown(actor, mkt, brain, proc, reg)
            # What the full selection pass would rank now.
            scores = []
            memo = {}
            for proc in proc_reg.all_processes():
                s = brain._calculate_recipe_score(actor, mkt, proc, memo)
                if s > 0:
                    scores.append((proc.id, s))
            scores.sort(key=lambda t: -t[1])
            p(
                "    selection pass top-8 (entry-margin scores): "
                + ", ".join(f"{i}={s:.1f}" for i, s in scores[:8])
            )
            p(f"    viable recipe count={len(scores)}")


if __name__ == "__main__":
    main()
