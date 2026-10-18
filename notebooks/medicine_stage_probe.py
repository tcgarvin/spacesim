"""Tier-1b probe: where do chosen-medicine actors actually spend their turns?"""

import json
from collections import Counter, defaultdict

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core import commands as cmd_mod
from spacesim2.core.brains.industrialist import IndustrialistBrain

TURNS = 200
PLANETS = 12
ACTORS = 100
SAMPLES = {50, 100, 150, 200}

stage_counts: dict[int, Counter] = defaultdict(Counter)
chosen_count: dict[int, int] = defaultdict(int)
attempts: Counter = Counter()
successes: Counter = Counter()
med_asks: list[
    tuple[int, str, int, int, int]
] = []  # turn, planet, qty, price, best_bid
wrapper_calls = 0
sell_calls = 0

_orig_decide = IndustrialistBrain.decide_economic_action
_orig_market = IndustrialistBrain.decide_market_actions
_orig_exec = cmd_mod.ProcessCommand.execute

NEED_PROCS = {"make_food", "gather_biomass", "make_clothing", "gather_fiber"}


def _classify(brain, actor, out):
    reg = actor.sim.commodity_registry
    proc = actor.sim.process_registry.get_process("make_medicine")
    if isinstance(out, cmd_mod.ProcessCommand):
        pid = out.process_id
        if pid == "make_medicine":
            return "run_medicine"
        if pid == "build_chemistry_lab":
            return "build_lab"
        if pid == "make_simple_tools":
            return "make_tools"
        if pid in NEED_PROCS:
            return "own_needs"
        return f"other_proc:{pid}"
    # GovernmentWork: why could the recipe not run?
    lab = reg.get_commodity("chemistry_lab")
    tools = reg.get_commodity("simple_tools")
    if not actor.inventory.has_quantity(lab, 1):
        bp = actor.sim.process_registry.get_process("build_chemistry_lab")
        missing = [
            c.id for c, q in bp.inputs.items() if not actor.inventory.has_quantity(c, q)
        ]
        if not actor.inventory.has_quantity(tools, 1):
            missing.append("simple_tools")
        return (
            "no_lab_missing:" + ",".join(sorted(missing))
            if missing
            else "no_lab_can_build"
        )
    missing = [
        c.id for c, q in proc.inputs.items() if not actor.inventory.has_quantity(c, q)
    ]
    if not missing:
        return "has_all_but_idle"
    # is a buy order standing for the missing input?
    mkt = actor.planet.market
    open_buys = {o.commodity_type.id for o in mkt.get_actor_orders(actor)["buy"]}
    tag = ",".join(sorted(missing))
    waiting = all(m in open_buys for m in missing)
    return f"blocked_inputs:{tag}:" + ("bid_open" if waiting else "no_bid")


def _wrapped_decide(self, actor):
    global wrapper_calls
    wrapper_calls += 1
    out = _orig_decide(self, actor)
    if self.chosen_recipe_id == "make_medicine":
        t = actor.sim.current_turn
        chosen_count[t] += 1
        stage_counts[t][_classify(self, actor, out)] += 1
    return out


def _wrapped_market(self, actor):
    global sell_calls
    sell_calls += 1
    cmds = _orig_market(self, actor)
    if self.chosen_recipe_id == "make_medicine" and actor.planet:
        mkt = actor.planet.market
        med = actor.sim.commodity_registry.get_commodity("medicine")
        bids = mkt.get_bid_levels(med)
        best_bid = max((p for p, _ in bids), default=0) if bids else 0
        for c in cmds:
            if (
                isinstance(c, cmd_mod.PlaceSellOrderCommand)
                and c.commodity_type.id == "medicine"
            ):
                med_asks.append(
                    (
                        actor.sim.current_turn,
                        actor.planet.name,
                        c.quantity,
                        c.price,
                        best_bid,
                    )
                )
    return cmds


def _wrapped_exec(self, actor):
    ok = _orig_exec(self, actor)
    if self.process_id in (
        "make_medicine",
        "refine_chemicals",
        "build_chemistry_lab",
        "make_chemicals",
        "make_glass",
        "build_smelting_facility",
    ):
        attempts[self.process_id] += 1
        if ok:
            successes[self.process_id] += 1
    return ok


IndustrialistBrain.decide_economic_action = _wrapped_decide
IndustrialistBrain.decide_market_actions = _wrapped_market
cmd_mod.ProcessCommand.execute = _wrapped_exec


def main() -> None:
    sim = create_and_setup_simulation(planets=PLANETS, actors=ACTORS, makers=2)
    reg = sim.commodity_registry
    med = reg.get_commodity("medicine")
    ints = [
        reg.get_commodity(c)
        for c in (
            "refined_chemicals",
            "chemicals",
            "biomass",
            "glass",
            "silica",
            "simple_building_materials",
        )
    ]
    per_planet = {}
    for t in range(1, TURNS + 1):
        sim.run_turn()
        if t in SAMPLES:
            print(
                f"turn {t}: chosen={chosen_count.get(t, 0)} stages={dict(stage_counts[t].most_common(8))}",
                flush=True,
            )
    if wrapper_calls == 0 or sell_calls == 0:
        raise RuntimeError("patch did not land")

    for p in sim.planets:
        m = p.market
        asks = m.get_ask_levels(med)
        bids = m.get_bid_levels(med)
        held = 0
        labs = 0
        by_type = Counter()
        holders = []
        for a in sim.actors:
            if a.planet is p:
                h = a.inventory.get_quantity(med)
                held += h
                if h:
                    by_type[str(a.actor_type)] += h
                    brain = getattr(a, "brain", None)
                    orders = p.market.get_actor_orders(a)["sell"]
                    listed = sum(
                        o.quantity for o in orders if o.commodity_type.id == "medicine"
                    )
                    holders.append(
                        (
                            a.name,
                            str(a.actor_type),
                            h,
                            listed,
                            getattr(brain, "chosen_recipe_id", None),
                        )
                    )
                lab = sim.commodity_registry.get_commodity("chemistry_lab")
                if lab:
                    labs += a.inventory.get_quantity(lab)
        per_planet[p.name] = {
            "med_ask_qty": sum(q for _, q in asks),
            "med_best_ask": min((pr for pr, _ in asks), default=None),
            "med_best_bid": max((pr for pr, _ in bids), default=None),
            "med_bid_qty": sum(q for _, q in bids),
            "med_held_by_actors": held,
            "chemistry_labs": labs,
            "held_by_type": dict(by_type),
            "holders": sorted(holders, key=lambda x: -x[2])[:6],
            "market_intermediates": {
                c.id: sum(q for _, q in m.get_ask_levels(c)) for c in ints if c
            },
        }
    out = {
        "wrapper_calls": wrapper_calls,
        "attempts": dict(attempts),
        "successes": dict(successes),
        "stage_counts": {str(t): dict(stage_counts[t]) for t in sorted(SAMPLES)},
        "chosen_count": {str(t): chosen_count.get(t, 0) for t in sorted(SAMPLES)},
        "med_asks_placed": len(med_asks),
        "med_ask_sample": med_asks[-15:],
        "per_planet": per_planet,
    }
    path = "tmp/medicine_stage.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(
        json.dumps(
            {k: out[k] for k in ("attempts", "successes", "med_asks_placed")}, indent=1
        )
    )
    print("wrote", path)


if __name__ == "__main__":
    main()
