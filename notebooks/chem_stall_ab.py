"""A/B for the stalled-procurement premium and stuck-recipe abandonment.

Arm "before" monkeypatches the two new IndustrialistBrain behaviors off, so
both arms run the same source. Arm "after" is the code as written.

    uv run python notebooks/chem_stall_ab.py before 3
    uv run python notebooks/chem_stall_ab.py after 3
"""

import sys
from collections import Counter

from spacesim2.core.actor import ActorType
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.simulation import Simulation

TURNS = 500
PLANETS = 16
ACTORS = 100


def disable_new_behavior():
    IndustrialistBrain._market_is_stalled = staticmethod(
        lambda market, commodity: False
    )
    IndustrialistBrain._update_stuck_tracking = lambda self, actor: None


def run(label):
    sim = Simulation()
    sim.setup_simple(
        num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
    )
    for _ in range(TURNS):
        sim.run_turn()

    reg = sim.commodity_registry
    names = ["food", "chemicals", "refined_chemicals", "medicine"]
    goods = {n: reg.get_commodity(n) for n in names}
    med = goods["medicine"]

    print(f"\n===== {label} =====", flush=True)
    inds = [
        a
        for a in sim.actors
        if a.actor_type != ActorType.SERVICE and isinstance(a.brain, IndustrialistBrain)
    ]
    chosen = Counter(a.brain.chosen_recipe_id for a in inds)
    blocked = sum(
        1
        for a in inds
        if a.brain.chosen_recipe_id
        and not a.can_execute_process(a.brain.chosen_recipe_id)
    )
    print(
        f"industrialists={len(inds)} blocked_on_chosen={blocked} "
        f"({100 * blocked / max(1, len(inds)):.0f}%)"
    )
    print("chosen recipes:", dict(chosen.most_common(8)))
    for name, c in goods.items():
        stock = sum(a.inventory.get_quantity(c) for a in sim.actors)
        vol = sum(p.market.get_30_day_average_volume(c) for p in sim.planets)
        prices = [
            p.market.get_30_day_average_price(c)
            for p in sim.planets
            if p.market.has_price_signal(c)
        ]
        price = sum(prices) / len(prices) if prices else float("nan")
        print(
            f"  {name:20} world_stock={stock:>7} sum_vol30={vol:>7.1f} "
            f"price={price:>7.1f} (n={len(prices)})"
        )

    depr = []
    starved = 0
    for planet in sim.planets:
        regs = [a for a in planet.actors if a.actor_type != ActorType.SERVICE]
        d = 0
        for a in regs:
            for dr in a.drives:
                if dr.metrics.get_name() == "health" and dr.metrics.debt > 0.3:
                    d += 1
        pct = 100.0 * d / max(1, len(regs))
        depr.append(pct)
        askq = sum(
            o.quantity
            for o in planet.market.sell_orders.get(med, [])
            if not o.cancelled
        )
        if askq == 0 and pct >= 20:
            starved += 1
    depr.sort()
    print(
        f"  health deprivation%: mean={sum(depr) / len(depr):.1f} "
        f"max={depr[-1]:.1f} planets>=20%={sum(1 for x in depr if x >= 20)} "
        f"starved(no-ask & >=20%)={starved}",
        flush=True,
    )


if __name__ == "__main__":
    arm = sys.argv[1]
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if arm == "before":
        disable_new_behavior()
    for i in range(repeats):
        run(f"{arm} run{i + 1}")
