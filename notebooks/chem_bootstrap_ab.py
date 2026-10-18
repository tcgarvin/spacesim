"""A/B: does the procurement-bid premium gate the medicine chain?

Baseline vs a monkeypatched IndustrialistBrain._buy_command that applies
PROCUREMENT_BOOTSTRAP_MARGIN whenever there is no resting ask, not only when
the good has never traded. Source is untouched; the patch lives here.

    uv run python notebooks/chem_bootstrap_ab.py
"""

import math
from collections import Counter

from spacesim2.core.actor import ActorType
from spacesim2.core.brains.industrialist import (
    PROCUREMENT_BOOTSTRAP_MARGIN,
    IndustrialistBrain,
)
from spacesim2.core.commands import PlaceBuyOrderCommand
from spacesim2.core.simulation import Simulation

TURNS = 500
PLANETS = 16
ACTORS = 100

_ORIGINAL_BUY = IndustrialistBrain._buy_command


def patched_buy_command(self, actor, market, commodity, quantity_to_buy, cache=None):
    """Bid imputed cost * bootstrap margin whenever nobody is offering."""
    if quantity_to_buy <= 0:
        return []
    asks = [
        o
        for o in market.sell_orders.get(commodity, [])
        if o.actor != actor and not o.cancelled
    ]
    if asks:
        return _ORIGINAL_BUY(self, actor, market, commodity, quantity_to_buy, cache)

    memo = cache.imputed_cost if cache is not None else {}
    imputed = self._imputed_unit_cost(actor, market, commodity, 0, frozenset(), memo)
    if math.isinf(imputed):
        return _ORIGINAL_BUY(self, actor, market, commodity, quantity_to_buy, cache)
    price = math.ceil(imputed * PROCUREMENT_BOOTSTRAP_MARGIN)
    if market.has_price_signal(commodity):
        price = max(price, market.get_avg_price(commodity))
    if price <= 0:
        return []
    affordable = min(quantity_to_buy, actor.money // price)
    if affordable <= 0:
        return []
    return [PlaceBuyOrderCommand(commodity, affordable, price)]


def run(label):
    sim = Simulation()
    sim.setup_simple(
        num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
    )
    for _ in range(TURNS):
        sim.run_turn()

    reg = sim.commodity_registry
    med = reg.get_commodity("medicine")
    chem = reg.get_commodity("chemicals")
    rchem = reg.get_commodity("refined_chemicals")

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
        f"industrialists={len(inds)} blocked_on_chosen={blocked} ({100 * blocked / len(inds):.0f}%)"
    )
    print("chosen recipes:", dict(chosen.most_common(8)))
    for name, c in (
        ("chemicals", chem),
        ("refined_chemicals", rchem),
        ("medicine", med),
    ):
        stock = sum(a.inventory.get_quantity(c) for a in sim.actors)
        vol = sum(p.market.get_30_day_average_volume(c) for p in sim.planets)
        print(f"  {name:20} world_stock={stock:>7} sum_vol30={vol:>7.1f}")

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
        f"starved(no-ask & >=20%)={starved}"
    )


if __name__ == "__main__":
    run("BASELINE")
    IndustrialistBrain._buy_command = patched_buy_command
    run("PATCHED: bootstrap premium whenever no ask")
