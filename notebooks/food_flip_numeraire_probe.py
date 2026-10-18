"""Tier-1b probe: why did needs-drive health collapse after the food flip?

Arms (monkeypatch only, no source edits):
  base      current main
  pinfood   numeraire pinned to plain hand-cooked food (hypothesis B off)
  slowfood  prosperity food category at 1/60 and target 2 (hypothesis A off)

Run: uv run python notebooks/food_flip_numeraire_probe.py --arm base
"""

import argparse
import json
import statistics
from collections import Counter
from typing import Any, Dict, List

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core import commands as commands_mod
from spacesim2.core.actor_brain import ActorBrain, BrainCache
from spacesim2.core.drives import prosperity_drive as pd_mod
from spacesim2.core.drives.prosperity_drive import ProsperityCategory, ProsperityDrive

FOOD_LABOR = {"make_food", "gather_biomass", "farm_biomass", "process_food"}
MED_LABOR = {
    "make_medicine",
    "make_chemicals",
    "refine_chemicals",
    "make_advanced_medicine",
}
SHELTER_LABOR = {
    "make_building_materials_wood",
    "make_building_materials_metal",
    "harvest_wood",
    "make_prefab_housing",
}
CLOTHING_LABOR = {
    "gather_fiber",
    "make_clothing",
    "make_textiles",
    "make_quality_clothing",
}

PROCESS_RUNS: Counter = Counter()
PATCH_CALLS: Counter = Counter()


def patch_process_counter() -> None:
    original = commands_mod.ProcessCommand.execute

    def wrapped(self, actor):  # type: ignore[no-untyped-def]
        ok = original(self, actor)
        PATCH_CALLS["process"] += 1
        if ok:
            PROCESS_RUNS[self.process_id] += 1
        return ok

    commands_mod.ProcessCommand.execute = wrapped  # type: ignore[method-assign]


def patch_pinfood(sim_registry_food_id: str = "food") -> None:
    """Numeraire uses plain food only, as before commit 637e296."""
    original = ActorBrain._cheapest_effective_price

    def wrapped(self, actor, market, materials, cache=None):  # type: ignore[no-untyped-def]
        PATCH_CALLS["pinfood"] += 1
        plain = [m for m in materials if m.id == sim_registry_food_id]
        use = plain if plain else materials
        return original(self, actor, market, use, cache)

    ActorBrain._cheapest_effective_price = wrapped  # type: ignore[method-assign]


def patch_slowfood() -> None:
    new = tuple(
        ProsperityCategory(c.name, c.commodity_id, 1.0 / 60.0, 2)
        if c.name == "food"
        else c
        for c in pd_mod.PROSPERITY_CATEGORIES
    )
    pd_mod.PROSPERITY_CATEGORIES = new
    PATCH_CALLS["slowfood"] += 1


def drive_named(actor, name: str):  # type: ignore[no-untyped-def]
    for d in actor.drives:
        if d.metrics.get_name() == name and not isinstance(d, ProsperityDrive):
            return d
    return None


def sample(sim, turn: int) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
    reg = sim.commodity_registry
    medicine = reg.get_commodity("medicine")
    plant = reg.get_commodity("chemical_plant")
    processed = reg.get_commodity("processed_food")
    food = reg.get_commodity("food")

    plant_planets = set()
    for actor in sim.actors:
        if actor.inventory.get_quantity(plant) > 0:
            plant_planets.add(actor.planet.name)

    rows: List[Dict[str, Any]] = []
    for actor in sim.actors:
        brain = actor.brain
        if not isinstance(brain, ActorBrain):
            continue
        hd = drive_named(actor, "health")
        fd = drive_named(actor, "food")
        if hd is None or fd is None:
            continue
        market = actor.planet.market
        cache = BrainCache()
        lam = brain._value_of_money(actor, market, cache)
        if lam <= 0:
            continue
        wtp = brain._drive_willingness_to_pay(actor, market, hd, medicine, lam, cache)
        _, ask = market.get_bid_ask_spread(medicine)
        rows.append(
            {
                "planet": actor.planet.name,
                "plant": actor.planet.name in plant_planets,
                "lam": lam,
                "wtp": wtp,
                "ask": ask,
                "numeraire": brain._numeraire_price(actor, market, cache),
                "health": hd.metrics.health,
                "med_units": actor.inventory.get_quantity(medicine),
                "shelter": getattr(
                    drive_named(actor, "shelter"), "metrics", hd.metrics
                ).health,
                "clothing": getattr(
                    drive_named(actor, "clothing"), "metrics", hd.metrics
                ).health,
                "food_h": fd.metrics.health,
            }
        )

    def agg(sel: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not sel:
            return {"n": 0}
        asks = [r["ask"] for r in sel if r["ask"] is not None]
        below = [r for r in sel if r["ask"] is not None and r["wtp"] < r["ask"]]
        return {
            "n": len(sel),
            "lam_med": round(statistics.median(r["lam"] for r in sel), 5),
            "numeraire_med": round(statistics.median(r["numeraire"] for r in sel), 2),
            "wtp_med": round(statistics.median(r["wtp"] for r in sel), 1),
            "ask_med": round(statistics.median(asks), 1) if asks else None,
            "ask_present_share": round(len(asks) / len(sel), 2),
            "share_bid_below_ask": round(len(below) / len(asks), 2) if asks else None,
            "health_mean": round(statistics.fmean(r["health"] for r in sel), 3),
            "shelter_mean": round(statistics.fmean(r["shelter"] for r in sel), 3),
            "clothing_mean": round(statistics.fmean(r["clothing"] for r in sel), 3),
            "food_mean": round(statistics.fmean(r["food_h"] for r in sel), 3),
            "med_units_mean": round(statistics.fmean(r["med_units"] for r in sel), 2),
        }

    market0 = sim.planets[0].market
    return {
        "turn": turn,
        "all": agg(rows),
        "plant": agg([r for r in rows if r["plant"]]),
        "no_plant": agg([r for r in rows if not r["plant"]]),
        "plant_planets": len(plant_planets),
        "med_vol30_mean": round(
            statistics.fmean(
                p.market.get_30_day_average_volume(medicine) for p in sim.planets
            ),
            2,
        ),
        "med_price30_mean": round(
            statistics.fmean(
                p.market.get_30_day_average_price(medicine) for p in sim.planets
            ),
            2,
        ),
        "pf_price30_mean": round(
            statistics.fmean(
                p.market.get_30_day_average_price(processed) for p in sim.planets
            ),
            2,
        ),
        "food_price30_mean": round(
            statistics.fmean(
                p.market.get_30_day_average_price(food) for p in sim.planets
            ),
            2,
        ),
        "labor": labor_shares(),
        "_unused": market0 is None,
    }


def labor_shares() -> Dict[str, float]:
    total = sum(PROCESS_RUNS.values())
    if total == 0:
        return {}

    def share(ids: set) -> float:
        return round(sum(PROCESS_RUNS[i] for i in ids) / total, 4)

    return {
        "total_runs": total,
        "food": share(FOOD_LABOR),
        "make_food": round(PROCESS_RUNS["make_food"] / total, 4),
        "gather_biomass": round(PROCESS_RUNS["gather_biomass"] / total, 4),
        "process_food": round(PROCESS_RUNS["process_food"] / total, 4),
        "medicine_chain": share(MED_LABOR),
        "shelter_chain": share(SHELTER_LABOR),
        "clothing_chain": share(CLOTHING_LABOR),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="base", choices=["base", "pinfood", "slowfood"])
    ap.add_argument("--turns", type=int, default=300)
    ap.add_argument("--planets", type=int, default=12)
    ap.add_argument("--actors", type=int, default=100)
    ap.add_argument("--samples", default="150,300")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.arm == "pinfood":
        patch_pinfood()
    elif args.arm == "slowfood":
        patch_slowfood()
    patch_process_counter()

    sample_turns = {int(t) for t in args.samples.split(",")}
    sim = create_and_setup_simulation(args.planets, args.actors, 2, 2, 1)

    out: List[Dict[str, Any]] = []
    for turn in range(1, args.turns + 1):
        sim.run_turn()
        if turn in sample_turns:
            out.append(sample(sim, turn))
            print(f"sampled turn {turn}", flush=True)

    if PATCH_CALLS["process"] == 0:
        raise SystemExit("process wrapper never fired")
    if args.arm == "pinfood" and PATCH_CALLS["pinfood"] == 0:
        raise SystemExit("pinfood patch never fired")
    if args.arm == "slowfood" and PATCH_CALLS["slowfood"] == 0:
        raise SystemExit("slowfood patch never fired")

    result = {"arm": args.arm, "turns": args.turns, "samples": out}
    print(json.dumps(result, indent=1))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main()
