"""Why the colonist food gate (colonist.py:54-62) does not fire on miss turns.

Records, at decide_economic_action entry, the inventory figures the gate
actually reads (get_quantity) alongside the available figures, the two
can_execute_process results, and the command returned. Rows are kept only
for actors that went on to miss a meal that turn.

    uv run python notebooks/food_gate_probe.py --turns 450 --planets 12
"""

import argparse
import sys
from collections import Counter
from typing import Any, Dict, List

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import ProcessCommand
from spacesim2.core.simulation import Simulation

STATE: Dict[str, Any] = {"turn": 0}
CALLS: Counter = Counter()
PRE: Dict[str, Dict[str, Any]] = {}
ROWS: List[Dict[str, Any]] = []
WINDOW = 301


def snap(actor: Actor, brain_tag: str, cmd: Any) -> Dict[str, Any]:
    reg = actor.sim.commodity_registry
    food = reg.get_commodity("food")
    bio = reg.get_commodity("biomass")
    name = (
        cmd.process_id
        if isinstance(cmd, ProcessCommand)
        else ("idle" if cmd is None else cmd.__class__.__name__)
    )
    return {
        "brain": brain_tag,
        "food_qty": actor.inventory.get_quantity(food),
        "food_avail": actor.inventory.get_available_quantity(food),
        "bio_qty": actor.inventory.get_quantity(bio),
        "bio_avail": actor.inventory.get_available_quantity(bio),
        "can_make": actor.can_execute_process("make_food"),
        "can_gather": actor.can_execute_process("gather_biomass"),
        "cmd": name,
    }


def install() -> None:
    o_run, o_take = Simulation.run_turn, Actor.take_turn
    o_col, o_ind = (
        ColonistBrain.decide_economic_action,
        IndustrialistBrain.decide_economic_action,
    )

    def run_turn(self: Simulation) -> None:
        STATE["turn"] = self.current_turn + 1
        PRE.clear()
        CALLS["run_turn"] += 1
        o_run(self)

    def wrap(orig: Any, tag: str) -> Any:
        def inner(self: Any, actor: Actor) -> Any:
            cmd = orig(self, actor)
            if STATE["turn"] >= WINDOW:
                CALLS["decide"] += 1
                PRE[actor.name] = snap(actor, tag, cmd)
            return cmd

        return inner

    def take_turn(self: Actor) -> None:
        o_take(self)
        if (
            STATE["turn"] >= WINDOW
            and self.actor_type is ActorType.REGULAR
            and not self.food_consumed_this_turn
        ):
            row = PRE.get(self.name)
            if row is not None:
                CALLS["miss"] += 1
                ROWS.append(row)

    Simulation.run_turn = run_turn  # type: ignore[method-assign]
    Actor.take_turn = take_turn  # type: ignore[method-assign]
    ColonistBrain.decide_economic_action = wrap(o_col, "C")  # type: ignore
    IndustrialistBrain.decide_economic_action = wrap(o_ind, "I")  # type: ignore


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "-"


def gate_reason(r: Dict[str, Any]) -> str:
    """Which condition in colonist.py:54-62 blocked gather_biomass."""
    if r["food_qty"] >= 5:
        return "food_qty>=5 (gate skipped)"
    if r["can_make"]:
        return "make_food executable"
    if r["bio_qty"] >= 4:
        return "bio_qty>=4"
    if not r["can_gather"]:
        return "can_gather False"
    return "gate should have fired"


def report() -> None:
    print(f"\n### miss rows {len(ROWS)}  window {WINDOW}+")
    if not ROWS:
        return
    for tag in ("C", "I"):
        rows = [r for r in ROWS if r["brain"] == tag]
        if not rows:
            continue
        print(f"\n[G1] brain={tag}  rows={len(rows)}  blocking condition")
        for reason, n in Counter(gate_reason(r) for r in rows).most_common():
            print(f"  {reason:<28}{n:>7}{pct(n, len(rows)):>9}")
        print(f"[G2] brain={tag}  reserved-stock check")
        for label, key_q, key_a in (
            ("food", "food_qty", "food_avail"),
            ("biomass", "bio_qty", "bio_avail"),
        ):
            resv = sum(1 for r in rows if r[key_q] > r[key_a])
            print(
                f"  {label:<9} qty>avail {pct(resv, len(rows)):>8}"
                f"  median qty {sorted(r[key_q] for r in rows)[len(rows) // 2]:>3}"
                f"  median avail {sorted(r[key_a] for r in rows)[len(rows) // 2]:>3}"
            )
        print(f"[G3] brain={tag}  command actually returned (top 8)")
        for cmd, n in Counter(r["cmd"] for r in rows).most_common(8):
            print(f"  {cmd:<32}{n:>7}{pct(n, len(rows)):>9}")
        print(
            f"[G4] brain={tag}  joint (food_qty>=5, can_make, bio_qty>=4, can_gather)"
        )
        joint = Counter(
            (r["food_qty"] >= 5, r["can_make"], r["bio_qty"] >= 4, r["can_gather"])
            for r in rows
        )
        for key, n in joint.most_common(8):
            print(
                f"  f>=5={key[0]!s:<5} mk={key[1]!s:<5} b>=4={key[2]!s:<5}"
                f" gath={key[3]!s:<5}{n:>7}{pct(n, len(rows)):>9}"
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
        if turn % 50 == 0:
            print(f"... turn {turn} rows={len(ROWS)}", flush=True)
    missing = [k for k in ("run_turn", "decide", "miss") if not CALLS[k]]
    if missing:
        raise RuntimeError(f"monkeypatch did not land: {missing} {dict(CALLS)}")
    print(f"### calls {dict(CALLS)}")
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
