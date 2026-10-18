"""Probe industrialist recipe scoring on the live market after a long run.

Runs its own sim rather than using load_run:

    uv run python notebooks/chem_score_probe.py

For one representative industrialist per planet it prints the score and
expected profit the brain assigns to chemistry-lab recipes against the recipes
it picks, with a term-by-term cost breakdown showing which term kills the
chemistry chain.
"""

import contextlib
import io
import math
import random
from collections import Counter

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.actor import ActorType

TURNS = 300
PLANETS = 3
ACTORS = 100

TARGET_RECIPES = [
    "make_textiles",
    "build_textile_mill",
    "make_glass",
    "make_chemicals",
    "refine_chemicals",
    "make_medicine",
    "make_polymers",
    "process_food",
    "make_quality_clothing",
    "build_chemistry_lab",
    "build_chemical_plant",
]

WATCH_COMMODITIES = [
    "biomass",
    "fiber",
    "silica",
    "chemicals",
    "refined_chemicals",
    "glass",
    "medicine",
    "textiles",
    "polymers",
    "quality_clothing",
    "processed_food",
    "simple_building_materials",
    "common_metal",
    "nova_fuel_ore",
    "simple_tools",
]


def run() -> None:
    sim = create_and_setup_simulation(
        planets=PLANETS,
        actors=ACTORS,
        makers=2,
        ships=1,
    )
    # run_turn() prints a per-turn summary; swallow it so only the diagnostic
    # dump reaches stdout.
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(TURNS):
            sim.run_turn()

    reg = sim.commodity_registry
    proc_reg = sim.process_registry

    industrialists = [
        a
        for a in sim.actors
        if a.actor_type == ActorType.REGULAR
        and a.brain.__class__.__name__ == "IndustrialistBrain"
    ]
    print(f"turns={TURNS} planets={PLANETS} actors/planet={ACTORS}")
    print(f"total industrialists: {len(industrialists)}")

    chosen = Counter(a.brain.chosen_recipe_id for a in industrialists)
    print("\n=== chosen_recipe_id distribution (all industrialists) ===")
    for rid, n in chosen.most_common():
        print(f"  {rid!s:32} {n}")

    print("\n=== facility ownership (any actor) ===")
    for fac in [
        "textile_mill",
        "chemistry_lab",
        "chemical_plant",
        "smelting_facility",
        "metalworking_facility",
    ]:
        c = reg.get_commodity(fac)
        if not c:
            continue
        owners = sum(1 for a in sim.actors if a.inventory.get_quantity(c) > 0)
        print(f"  {fac:24} owners={owners}")

    # One representative industrialist per planet, scored on its own market.
    seen_planets = set()
    reps = []
    for a in industrialists:
        if a.planet and a.planet.name not in seen_planets:
            reps.append(a)
            seen_planets.add(a.planet.name)

    for actor in reps:
        market = actor.planet.market
        brain = actor.brain
        print("\n" + "=" * 70)
        print(
            f"REPRESENTATIVE industrialist on {actor.planet.name} "
            f"(money={actor.money}, chosen={brain.chosen_recipe_id}, "
            f"horizon={brain.facility_amortization_horizon})"
        )

        print("\n  live market (bid / ask / avg) + imputed unit cost:")
        for cid in WATCH_COMMODITIES:
            c = reg.get_commodity(cid)
            if not c:
                continue
            bid, ask = market.get_bid_ask_spread(c)
            avg = market.get_avg_price(c)
            imp = brain._imputed_unit_cost(actor, market, c, 0, frozenset(), {})
            print(
                f"    {cid:26} bid={_s(bid):>5} ask={_s(ask):>5} "
                f"avg={avg:>4} imputed={_f(imp):>7}"
            )

        print("\n  recipe scores (require_entry_margin=False -> raw profit):")
        for rid in TARGET_RECIPES:
            p = proc_reg.get_process(rid)
            if not p:
                continue
            _breakdown(actor, market, brain, p)


def _breakdown(actor, market, brain, process) -> None:
    """Reproduce _impute_recipe_cost and the output value term by term."""
    reg_out = process.outputs
    # Cost side.
    labor = 10.0  # GOVERNMENT_WAGE
    parts = [f"labor={labor:.1f}"]
    total = labor
    infinite = False
    for c, q in process.inputs.items():
        u = brain._imputed_unit_cost(actor, market, c, 1, frozenset(), {})
        if math.isinf(u):
            infinite = True
        total += u * q
        parts.append(f"{c.id}x{q}@{_f(u)}={_f(u * q)}")
    for tool in process.tools_required:
        if actor.inventory.has_quantity(tool, 1):
            continue
        u = brain._imputed_unit_cost(actor, market, tool, 1, frozenset(), {})
        total += u / 100.0
        parts.append(f"tool:{tool.id}/100={_f(u / 100.0)}")
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
        bc = brain._impute_recipe_cost(actor, market, bp, 1, frozenset(), {})
        amort = bc / brain.facility_amortization_horizon
        total += amort
        parts.append(
            f"fac:{fac.id} build={_f(bc)}/{brain.facility_amortization_horizon}"
            f"={_f(amort)}"
        )

    # Output side, as in _calculate_recipe_score.
    attr_mod = 1.0
    if process.resource_attribute and actor.planet and actor.planet.attributes:
        attr_mod = actor.planet.attributes.get_availability(
            process.resource_attribute.commodity
        )
    out_val = 0.0
    out_parts = []
    unsellable = False
    for c, q in reg_out.items():
        if not c.transportable:
            out_val += 50.0 * q
            out_parts.append(f"{c.id}x{q}=FACILITY(50)")
            continue
        bid, _ = market.get_bid_ask_spread(c)
        if bid is not None:
            price = bid
            src = "bid"
        else:
            price = market.get_avg_price(c)
            src = "avg"
            if price <= 0:
                unsellable = True
        out_val += price * q * attr_mod
        out_parts.append(f"{c.id}x{q}@{price}({src})={_f(price * q * attr_mod)}")

    score = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=False
    )
    entry = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=True
    )
    cost_str = "INF" if infinite else _f(total)
    print(
        f"    {process.id:22} cost={cost_str:>8} out={_f(out_val):>7} "
        f"raw_score={_f(score):>8} entry_score={_f(entry):>7}"
    )
    print(f"        cost: {' + '.join(parts)}")
    print(f"        out:  {' + '.join(out_parts)}  attr_mod={attr_mod:.2f}")


def _s(v):
    return "-" if v is None else str(v)


def _f(v):
    if v is None:
        return "-"
    if math.isinf(v):
        return "INF"
    return f"{v:.1f}"


if __name__ == "__main__":
    run()
