"""Why rare-earth miners/refiners exit while luxury_goods demand is strong.

In-process Tier-1b probe. Samples rare_earth_ore / rare_earth market state,
recipe adoption counts, and per-planet score decomposition for
mine_rare_earth and refine_rare_earth, plus the reason each mine_rare_earth
exit fired (recipe-stuck cooldown, score<=0 exit check, or the 1% random
reroll).

    uv run python notebooks/rare_earth_chain_probe.py --turns 450 --planets 12 --out tmp/rare_earth_probe.json

Three monkeypatches collect what the objects do not expose after the fact:
``IndustrialistBrain._should_reevaluate_recipe`` (records whether the 1%
reroll fired this actor-turn), ``IndustrialistBrain.decide_economic_action``
(wraps the call to see chosen_recipe_id and recipe_cooldown_until before and
after, so a mine_rare_earth -> other transition can be attributed to
reroll/cooldown/score-exit), and none else. All wrap and call through; call
counts are asserted non-zero.
"""

import argparse
import math
import statistics
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import BrainCache, GOVERNMENT_WAGE
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 450
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
DEFAULT_SAMPLES = (150, 300, 450)
WATCH_RECIPES = ("mine_rare_earth", "refine_rare_earth", "make_luxury_goods")

# Filled by the monkeypatches.
REROLL_FLAG: Dict[int, bool] = {}  # id(brain) -> did the 1% reroll fire this turn
EXIT_REASONS: Counter[str] = Counter()  # reason -> count, for mine_rare_earth exits
DECIDE_CALLS = [0]
REROLL_CALLS = [0]


def install_patches() -> None:
    orig_should_reroll = IndustrialistBrain._should_reevaluate_recipe
    orig_decide = IndustrialistBrain.decide_economic_action

    def should_reroll(self: IndustrialistBrain) -> bool:
        REROLL_CALLS[0] += 1
        result = orig_should_reroll(self)
        REROLL_FLAG[id(self)] = result
        return result

    def decide(self: IndustrialistBrain, actor: Any) -> Any:
        DECIDE_CALLS[0] += 1
        old_recipe = self.chosen_recipe_id
        before_cooldown = set(self.recipe_cooldown_until)
        result = orig_decide(self, actor)
        if old_recipe == "mine_rare_earth" and self.chosen_recipe_id != old_recipe:
            reroll = REROLL_FLAG.get(id(self), False)
            newly_cooled = old_recipe in self.recipe_cooldown_until and (
                old_recipe not in before_cooldown
            )
            if newly_cooled:
                EXIT_REASONS["stuck_cooldown"] += 1
            elif reroll:
                EXIT_REASONS["random_reroll"] += 1
            else:
                EXIT_REASONS["score_exit_check"] += 1
        return result

    IndustrialistBrain._should_reevaluate_recipe = should_reroll  # type: ignore[method-assign]
    IndustrialistBrain.decide_economic_action = decide  # type: ignore[method-assign]


def industrialists_on(planet: Any) -> List[Any]:
    return [
        a
        for a in planet.actors
        if a.actor_type is ActorType.REGULAR and isinstance(a.brain, IndustrialistBrain)
    ]


def market_row(market: Any, commodity: Any) -> Dict[str, float]:
    bid, ask = market.get_bid_ask_spread(commodity)
    vol = market.get_30_day_average_volume(commodity)
    price = (
        market.get_30_day_average_price(commodity)
        if market.has_price_signal(commodity)
        else 0.0
    )
    return {
        "bid": float(bid) if bid is not None else -1.0,
        "ask": float(ask) if ask is not None else -1.0,
        "price": float(price),
        "vol30": float(vol),
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "INF"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def decompose(
    actor: Any, market: Any, brain: IndustrialistBrain, process: Any
) -> Dict[str, Any]:
    """Cost/value decomposition of one recipe for one industrialist.

    Mirrors notebooks/prosperity_blockers_probe.decompose, generalized off
    the prosperity-category output tiering (this probe's outputs are plain
    transportable goods, not drive materials).
    """
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
    attribute_modifier = 1.0
    if process.resource_attribute and actor.planet:
        attribute_modifier = actor.planet.attributes.get_availability(
            process.resource_attribute.commodity
        )
    for c, q in process.outputs.items():
        if not c.transportable:
            out_value += 50.0 * q
            outs.append(f"{c.id}=FACILITY(50)")
            continue
        expected_q = q * attribute_modifier
        price = brain._output_unit_value(actor, market, c, expected_q, memo)
        out_value += price * expected_q
        outs.append(
            f"{c.id}x{expected_q:.2f}(attr={attribute_modifier:.2f}) px={price:.1f}"
        )
    raw = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=False
    )
    entry = brain._calculate_recipe_score(
        actor, market, process, require_entry_margin=True
    )
    return {
        "cost": "INF" if infinite else round(cost, 1),
        "outval": round(out_value, 1),
        "raw_score": round(raw, 1) if math.isfinite(raw) else "INF",
        "entry_score": round(entry, 1) if math.isfinite(entry) else "INF",
        "inputs": inputs,
        "reqs": reqs,
        "outs": outs,
    }


def sample_table(sim: Simulation, turn: int) -> Dict[str, Any]:
    reg = sim.commodity_registry
    proc_reg = sim.process_registry
    ore = reg.get_commodity("rare_earth_ore")
    rare = reg.get_commodity("rare_earth")
    lab = reg.get_commodity("chemistry_lab")

    rows = []
    print(f"\n{'=' * 118}\nTURN {turn}")
    header = (
        f"{'planet':>8} {'ore_attr':>8} {'ore_bid':>7} {'ore_ask':>7} {'ore_px':>7} "
        f"{'ore_v30':>7} {'re_bid':>7} {'re_ask':>7} {'re_px':>7} {'re_v30':>7} "
        f"{'mine#':>6} {'refine#':>8} {'lux#':>5} {'labs':>5}"
    )
    print(header)
    for p in sim.planets:
        ore_row = market_row(p.market, ore) if ore else {}
        rare_row = market_row(p.market, rare) if rare else {}
        inds = industrialists_on(p)
        counts = Counter(a.brain.chosen_recipe_id for a in inds)
        lab_owners = sum(
            1 for a in p.actors if lab and a.inventory.get_quantity(lab) > 0
        )
        row = {
            "planet": p.name,
            "ore_attr": round(p.attributes.rare_earth_ore, 3),
            "ore": ore_row,
            "rare_earth": rare_row,
            "mine_rare_earth": counts.get("mine_rare_earth", 0),
            "refine_rare_earth": counts.get("refine_rare_earth", 0),
            "make_luxury_goods": counts.get("make_luxury_goods", 0),
            "chemistry_lab_owners": lab_owners,
        }
        rows.append(row)
        print(
            f"{p.name:>8} {row['ore_attr']:>8.3f} {ore_row.get('bid', -1):>7.0f} "
            f"{ore_row.get('ask', -1):>7.0f} {ore_row.get('price', 0):>7.0f} "
            f"{ore_row.get('vol30', 0):>7.2f} {rare_row.get('bid', -1):>7.0f} "
            f"{rare_row.get('ask', -1):>7.0f} {rare_row.get('price', 0):>7.0f} "
            f"{rare_row.get('vol30', 0):>7.2f} {row['mine_rare_earth']:>6} "
            f"{row['refine_rare_earth']:>8} {row['make_luxury_goods']:>5} "
            f"{row['chemistry_lab_owners']:>5}"
        )

    totals = Counter()
    for r in rows:
        for k in WATCH_RECIPES:
            totals[k] += r[k]
    print(
        "\ntotals: "
        + ", ".join(f"{k}={totals[k]}" for k in WATCH_RECIPES)
        + f"  ore_attr_median={statistics.median(p.attributes.rare_earth_ore for p in sim.planets):.2f}"
    )
    return {"turn": turn, "rows": rows, "totals": dict(totals)}


def print_decompositions(sim: Simulation) -> None:
    mine = sim.process_registry.get_process("mine_rare_earth")
    refine = sim.process_registry.get_process("refine_rare_earth")
    print("\nrecipe decomposition (first industrialist on each planet):")
    for p in sim.planets:
        inds = industrialists_on(p)
        if not inds:
            continue
        a = inds[0]
        for proc in (mine, refine):
            if proc is None:
                continue
            d = decompose(a, p.market, a.brain, proc)
            print(
                f"  {p.name:>8} {proc.id:20} attr={p.attributes.rare_earth_ore:.2f} "
                f"cost={str(d['cost']):>7} outval={d['outval']:>7} "
                f"raw={str(d['raw_score']):>8} entry={str(d['entry_score']):>8}"
            )
            print(f"      in: {', '.join(d['inputs']) or '-'}")
            print(f"      req: {', '.join(d['reqs']) or '-'}")
            print(f"      out: {'; '.join(d['outs'])}")


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
    sim = create_and_setup_simulation(
        planets=args.planets, actors=args.actors, makers=2
    )

    for t in range(1, args.turns + 1):
        sim.run_turn()
        if t in samples:
            sample_table(sim, t)
            if t == max(samples):
                print_decompositions(sim)

    print(f"\nmine_rare_earth exit reasons (cumulative over run): {dict(EXIT_REASONS)}")
    if DECIDE_CALLS[0] == 0 or REROLL_CALLS[0] == 0:
        raise RuntimeError(
            f"monkeypatch did not land: decide={DECIDE_CALLS[0]} reroll={REROLL_CALLS[0]}"
        )
    print(
        f"\npatch calls: decide_economic_action={DECIDE_CALLS[0]} reroll_check={REROLL_CALLS[0]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
