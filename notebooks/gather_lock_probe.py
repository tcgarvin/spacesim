"""Do actors on low-biomass planets sit in the food gate gathering forever?

Records every regular-actor economic action, bucketed by planet biomass,
plus end-of-window prices, pantry, and health-drive deprivation.

    uv run python notebooks/gather_lock_probe.py --turns 450 --planets 12
"""

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.simulation import Simulation

STATE: Dict[str, Any] = {"turn": 0}
CALLS: Counter = Counter()
ACTIONS: Dict[str, Counter] = defaultdict(Counter)
PANTRY: Dict[str, List[int]] = defaultdict(list)
WINDOW = 301


def bucket(v: float) -> str:
    return "lo<0.4" if v < 0.4 else ("mid" if v <= 0.7 else "hi>0.7")


def group(cmd: Any) -> str:
    if cmd is None:
        return "idle"
    if isinstance(cmd, ProcessCommand):
        pid = cmd.process_id
        if pid in ("gather_biomass", "make_food"):
            return pid
        return "other_recipe"
    if cmd.__class__.__name__ == "GovernmentWorkCommand":
        return "gov_work"
    return "other_cmd"


def install() -> None:
    o_run = Simulation.run_turn
    o_col = ColonistBrain.decide_economic_action
    o_ind = IndustrialistBrain.decide_economic_action

    def run_turn(self: Simulation) -> None:
        STATE["turn"] = self.current_turn + 1
        CALLS["run_turn"] += 1
        o_run(self)

    def wrap(orig: Any) -> Any:
        def inner(self: Any, actor: Actor) -> Any:
            cmd = orig(self, actor)
            if STATE["turn"] >= WINDOW and actor.actor_type is ActorType.REGULAR:
                CALLS["decide"] += 1
                bk = bucket(actor.planet.attributes.biomass)
                ACTIONS[bk][group(cmd)] += 1
                food = actor.sim.commodity_registry.get_commodity("food")
                PANTRY[bk].append(actor.inventory.get_available_quantity(food))
            return cmd

        return inner

    Simulation.run_turn = run_turn  # type: ignore[method-assign]
    ColonistBrain.decide_economic_action = wrap(o_col)  # type: ignore
    IndustrialistBrain.decide_economic_action = wrap(o_ind)  # type: ignore


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "-"


def report(sim: Simulation) -> None:
    labels = ("gather_biomass", "make_food", "gov_work", "other_recipe", "idle")
    print("\n[A] economic action shares by planet biomass bucket")
    print(f"{'bucket':<9}{'turns':>9}" + "".join(f"{x:>16}" for x in labels))
    for bk in ("lo<0.4", "mid", "hi>0.7"):
        c = ACTIONS[bk]
        tot = sum(c.values())
        if not tot:
            continue
        print(f"{bk:<9}{tot:>9}" + "".join(f"{pct(c[x], tot):>16}" for x in labels))

    print("\n[B] prices, pantry, and drives by bucket (end of run)")
    reg = sim.commodity_registry
    food = reg.get_commodity("food")
    bio = reg.get_commodity("biomass")
    rows: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for planet in sim.planets:
        bk = bucket(planet.attributes.biomass)
        for label, com in (("food_px", food), ("bio_px", bio)):
            px = planet.market.get_avg_price(com)
            if px:
                rows[bk][label].append(float(px))
    for actor in sim.actors:
        if actor.actor_type is not ActorType.REGULAR:
            continue
        bk = bucket(actor.planet.attributes.biomass)
        for drive in actor.drives:
            dn = drive.metrics.get_name()
            if dn in ("health", "food"):
                rows[bk][f"{dn}_h"].append(drive.metrics.health)
                rows[bk][f"{dn}_dep"].append(1.0 if drive.metrics.health < 0.5 else 0.0)

    def med(bk: str, k: str) -> float:
        vals = rows[bk][k]
        return statistics.fmean(vals) if vals else -1.0

    print(
        f"{'bucket':<9}{'food_px':>9}{'bio_px':>9}{'pantry':>9}"
        f"{'food_h':>9}{'health_h':>10}{'health_dep':>12}"
    )
    for bk in ("lo<0.4", "mid", "hi>0.7"):
        if not PANTRY[bk]:
            continue
        print(
            f"{bk:<9}{med(bk, 'food_px'):>9.1f}{med(bk, 'bio_px'):>9.1f}"
            f"{statistics.fmean(PANTRY[bk]):>9.2f}{med(bk, 'food_h'):>9.3f}"
            f"{med(bk, 'health_h'):>10.3f}{med(bk, 'health_dep'):>12.3f}"
        )


def main(argv: List[str]) -> int:
    global WINDOW
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=450)
    ap.add_argument("--planets", type=int, default=12)
    ap.add_argument("--actors", type=int, default=100)
    ap.add_argument("--window-start", type=int, default=301)
    args = ap.parse_args(argv)
    WINDOW = args.window_start
    install()
    sim = Simulation()
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    for turn in range(1, args.turns + 1):
        sim.run_turn()
        if turn % 100 == 0:
            print(f"... turn {turn}", flush=True)
    missing = [k for k in ("run_turn", "decide") if not CALLS[k]]
    if missing:
        raise RuntimeError(f"monkeypatch did not land: {missing} {dict(CALLS)}")
    print(f"### calls {dict(CALLS)}")
    report(sim)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
