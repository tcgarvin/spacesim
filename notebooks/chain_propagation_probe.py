"""Tier-1b probe: does demand at the top of the medicine chain (medicine)
propagate down to its inputs (refined_chemicals, chemicals, glass, silica)?

Fixed config, self-contained, one compact JSON block. Meant to be run before
and after a change to industrialist input bidding, so the config and sample
turns must not depend on argv beyond the optional output path.
"""

import json
import sys
from collections import Counter

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core import market as market_mod
from spacesim2.core.actor import ActorType
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.drives.prosperity_drive import needs_are_met

TURNS = 200
PLANETS = 12
ACTORS = 100
SAMPLES = (100, 150, 200)

CHAIN = [
    "chemicals",
    "refined_chemicals",
    "glass",
    "silica",
    "medicine",
    "simple_building_materials",
]
VOLUME_TRACKED = ("refined_chemicals", "glass", "medicine")
RECIPE_IDS = (
    "make_medicine",
    "refine_chemicals",
    "make_glass",
    "make_chemicals",
    "build_chemistry_lab",
)
HEALTH_DEBT_THRESHOLD = 0.25

cumulative_volume: Counter = Counter()
total_transactions = 0

_orig_execute_transaction = market_mod.Market._execute_transaction


def _wrapped_execute_transaction(
    self, buyer, seller, commodity_type, quantity, *a, **k
):
    global total_transactions
    total_transactions += 1
    if commodity_type.id in VOLUME_TRACKED:
        cumulative_volume[commodity_type.id] += quantity
    return _orig_execute_transaction(
        self, buyer, seller, commodity_type, quantity, *a, **k
    )


market_mod.Market._execute_transaction = _wrapped_execute_transaction


def market_state(sim, commodity_id):
    """Aggregate one commodity's order-book state across all planets."""
    reg = sim.commodity_registry
    c = reg.get_commodity(commodity_id)
    bids: list[int] = []
    asks: list[int] = []
    bid_qty = 0
    ask_qty = 0
    volume = 0.0
    scarcity: list[float] = []
    for p in sim.planets:
        m = p.market
        bid, ask = m.get_bid_ask_spread(c)
        if bid is not None:
            bids.append(bid)
        if ask is not None:
            asks.append(ask)
        bid_qty += sum(q for _, q in m.get_bid_levels(c))
        ask_qty += sum(q for _, q in m.get_ask_levels(c))
        volume += m.get_30_day_average_volume(c)
        scarcity.append(m.scarcity_pressure_for(c))
    return {
        "mean_best_bid": round(sum(bids) / len(bids), 2) if bids else None,
        "mean_best_ask": round(sum(asks) / len(asks), 2) if asks else None,
        "planets_with_bid": len(bids),
        "planets_with_ask": len(asks),
        "resting_bid_qty": bid_qty,
        "resting_ask_qty": ask_qty,
        "avg_30d_volume": round(volume, 1),
        "mean_scarcity_pressure": round(sum(scarcity) / len(scarcity), 3)
        if scarcity
        else 0.0,
    }


def per_planet_chain(sim):
    reg = sim.commodity_registry
    commodities = [reg.get_commodity(cid) for cid in CHAIN]
    lab = reg.get_commodity("chemistry_lab")
    table = {}
    for p in sim.planets:
        m = p.market
        row = {}
        for cid, c in zip(CHAIN, commodities):
            bid, ask = m.get_bid_ask_spread(c)
            row[cid] = {
                "bid": bid,
                "ask": ask,
                "scarcity": round(m.scarcity_pressure_for(c), 3),
            }
        labs = sum(a.inventory.get_quantity(lab) for a in p.actors)
        row["chemistry_labs"] = labs
        table[p.name] = row
    return table


def sample(sim, turn):
    reg = sim.commodity_registry
    refined_chemicals = reg.get_commodity("refined_chemicals")

    market = {cid: market_state(sim, cid) for cid in CHAIN}

    labs_by_planet = {}
    for p in sim.planets:
        lab = reg.get_commodity("chemistry_lab")
        labs_by_planet[p.name] = sum(a.inventory.get_quantity(lab) for a in p.actors)
    planets_with_zero_labs = sum(1 for v in labs_by_planet.values() if v == 0)

    recipe_counts: Counter = Counter()
    make_medicine_actors = []
    for p in sim.planets:
        for a in p.actors:
            brain = getattr(a, "brain", None)
            if isinstance(brain, IndustrialistBrain):
                recipe_counts[brain.chosen_recipe_id] += 1
                if brain.chosen_recipe_id == "make_medicine":
                    make_medicine_actors.append((a, p))

    blocked_prices = []
    for a, p in make_medicine_actors:
        buys = p.market.get_actor_orders(a)["buy"]
        prices = [o.price for o in buys if o.commodity_type.id == "refined_chemicals"]
        if prices:
            blocked_prices.append(max(prices))

    health_values = []
    debt_over_threshold = 0
    gate_pass = 0
    n_actors = 0
    for p in sim.planets:
        for a in p.actors:
            if a.actor_type != ActorType.REGULAR:
                continue
            n_actors += 1
            for drive in a.drives:
                if drive.metrics.get_name() == "health":
                    health_values.append(drive.metrics.health)
                    if drive.metrics.debt > HEALTH_DEBT_THRESHOLD:
                        debt_over_threshold += 1
                    break
            if needs_are_met(a):
                gate_pass += 1

    return {
        "market": market,
        "cumulative_volume": dict(cumulative_volume),
        "labs": {
            "total": sum(labs_by_planet.values()),
            "planets_with_zero_labs": planets_with_zero_labs,
        },
        "recipe_counts": {k: v for k, v in recipe_counts.items() if k in RECIPE_IDS},
        "make_medicine_blocked_on_refined_chemicals": {
            "total_make_medicine_actors": len(make_medicine_actors),
            "with_open_bid": len(blocked_prices),
            "mean_bid_price": round(sum(blocked_prices) / len(blocked_prices), 1)
            if blocked_prices
            else None,
            "max_bid_price": max(blocked_prices) if blocked_prices else None,
        },
        "health": {
            "mean_health": round(sum(health_values) / len(health_values), 3)
            if health_values
            else None,
            "debt_over_0.25_share": round(debt_over_threshold / n_actors, 3)
            if n_actors
            else None,
        },
        "prosperity_gate_pass_share": round(gate_pass / n_actors, 3)
        if n_actors
        else None,
        "per_planet_chain": per_planet_chain(sim),
    }


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "tmp/chain_propagation.json"

    sim = create_and_setup_simulation(planets=PLANETS, actors=ACTORS, makers=2)

    turns_out: dict[str, dict] = {}
    for t in range(1, TURNS + 1):
        sim.run_turn()
        if t in SAMPLES:
            turns_out[str(t)] = sample(sim, t)

    if total_transactions == 0:
        raise RuntimeError(
            "transaction wrapper recorded no fills; patch may not have landed"
        )

    result = {
        "config": {
            "turns": TURNS,
            "planets": PLANETS,
            "actors": ACTORS,
            "samples": SAMPLES,
        },
        "turns": turns_out,
    }

    with open(out_path, "w") as f:
        json.dump(result, f, indent=1, default=str)

    print(json.dumps(result, default=str))
    print("wrote", out_path)


if __name__ == "__main__":
    main()
