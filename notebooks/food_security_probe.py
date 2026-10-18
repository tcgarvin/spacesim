"""Are food-poor planets fed by ship trade, or do they drain by emigration?

Tier-1b in-process probe. One row per ``(turn, planet)``. It watches the
planets whose biomass mean cannot support hand food (hand food breaks even
at biomass ~0.167) and records, per sample turn, whether food arrives by
ship, what it costs, how hungry the residents are, and how many of them are
trying to leave.

Per planet and sample turn:

- ``biomass``: the planet's biomass mean (``planet.attributes.biomass``).
- ``population``: regular residents, service actors excluded. Passengers
  aboard a ship are in no ``planet.actors``, so they are excluded too,
  the same count ``analysis/summary.py`` treats as population.
- ``food`` / ``processed_food``: mean clearing price and total units over
  the last ``--window`` turns of ``planet.market.transaction_history``,
  plus the units of that volume a ``Ship`` sold here (ship imports), the
  test ``_summarize_trade`` uses.
- ``food_health`` / ``food_debt``: median ``FoodDrive`` metrics over the
  residents.
- ``leaving``: residents with a live migration intent, either a standing
  ``MigrationRequest`` or an open passage contract.
- ``departures`` / ``arrivals``: cumulative per planet, counted here.
  Departures come from ``sim.migration_log`` (a boarding is a departure);
  arrivals from a wrapper around ``migration.relocate_actor``, which the
  simulation has no per-planet counter for.

The printed table buckets planets by biomass mean and is the KPI to read.

Run (threaded actor phase, free-threaded interpreter):

    /home/timg/code/spacesim/.venv-ft/bin/python \
        /home/timg/code/spacesim/notebooks/food_security_probe.py \
        --turns 600 --planets 100 --warmup 100 --sample-every 50 \
        --workers 12 --out /home/timg/code/spacesim/tmp/food_security.json
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from spacesim.cli.common import create_and_setup_simulation
from spacesim.core import migration as migration_module
from spacesim.core.actor import ActorType
from spacesim.core.commodity import CommodityDefinition
from spacesim.core.contracts import PassengerPayload
from spacesim.core.market import Market
from spacesim.core.migration import MigrationRequest
from spacesim.core.planet import Planet
from spacesim.core.ship import Ship
from spacesim.core.simulation import Simulation

FOOD_IDS = ("food", "processed_food")
FOOD_DRIVE_NAME = "food"

# Hand food (gather biomass, then make_food) stops breaking even below this
# biomass mean, so a planet under it cannot feed itself by hand at all.
HAND_FOOD_BREAKEVEN = 0.167

# Bucket upper bounds on the planet's biomass mean. 0.0 is its own bucket:
# under the landing change roughly 1/6 of planets draw exactly zero.
BUCKET_EDGES: Sequence[Tuple[str, float, float]] = (
    ("biomass == 0", -1.0, 0.0),
    ("0 < b <= 0.17", 0.0, HAND_FOOD_BREAKEVEN),
    ("0.17 < b <= 0.4", HAND_FOOD_BREAKEVEN, 0.4),
    ("0.4 < b <= 1.0", 0.4, 1.0),
)

arrivals_by_planet: Counter[str] = Counter()
patch_calls: Counter[str] = Counter()


def install_patches() -> None:
    """Count relocations per destination planet, keeping behavior unchanged.

    ``contracts.py`` imports ``relocate_actor`` inside the delivery
    functions, so patching the module attribute is enough.
    """
    original = migration_module.relocate_actor

    def wrapped(actor, destination, land):  # type: ignore[no-untyped-def]
        patch_calls["relocate"] += 1
        arrivals_by_planet[destination.name] += 1
        return original(actor, destination, land)

    migration_module.relocate_actor = wrapped  # type: ignore[assignment]


def window_trade(
    market: Market, commodity: CommodityDefinition, cutoff: int
) -> Dict[str, Any]:
    """Mean clearing price, total units, and ship-sold units since ``cutoff``."""
    prices: List[int] = []
    units = 0
    ship_units = 0
    for tx in market.transaction_history:
        if tx.turn < cutoff or tx.commodity_type.id != commodity.id:
            continue
        prices.append(tx.price)
        units += tx.quantity
        if isinstance(tx.seller, Ship):
            ship_units += tx.quantity
    return {
        "price": round(statistics.fmean(prices), 2) if prices else None,
        "units": units,
        "ship_units": ship_units,
    }


def food_metrics(actors: Sequence[Any]) -> Tuple[Optional[float], Optional[float]]:
    """Median FoodDrive health and debt over ``actors``."""
    health: List[float] = []
    debt: List[float] = []
    for actor in actors:
        for drive in actor.drives:
            if drive.metrics.get_name() != FOOD_DRIVE_NAME:
                continue
            health.append(drive.metrics.health)
            debt.append(drive.metrics.debt)
    if not health:
        return None, None
    return round(statistics.median(health), 3), round(statistics.median(debt), 3)


def leaving_count(planet: Planet, residents: Sequence[Any]) -> int:
    """Residents with a live intent to leave: a request or an open passage."""
    posted = {
        contract.payload.actor.name
        for contract in planet.contracts.open_contracts()
        if isinstance(contract.payload, PassengerPayload)
    }
    return sum(
        1
        for actor in residents
        if isinstance(actor.migration_request, MigrationRequest) or actor.name in posted
    )


def sample(
    sim: Simulation, commodities: Dict[str, CommodityDefinition], window: int
) -> List[Dict[str, Any]]:
    cutoff = sim.current_turn - window
    departures = Counter(event.origin_name for event in sim.migration_log)
    rows: List[Dict[str, Any]] = []
    for planet in sim.planets:
        residents = [a for a in planet.actors if a.actor_type == ActorType.REGULAR]
        health, debt = food_metrics(residents)
        row: Dict[str, Any] = {
            "turn": sim.current_turn,
            "planet": planet.name,
            "biomass": round(planet.attributes.biomass, 4),
            "population": len(residents),
            "food_health": health,
            "food_debt": debt,
            "leaving": leaving_count(planet, residents),
            "departures_cum": departures.get(planet.name, 0),
            "arrivals_cum": arrivals_by_planet.get(planet.name, 0),
        }
        ship_food_units = 0
        for cid, commodity in commodities.items():
            trade = window_trade(planet.market, commodity, cutoff)
            row[f"{cid}_price"] = trade["price"]
            row[f"{cid}_units"] = trade["units"]
            row[f"{cid}_ship_units"] = trade["ship_units"]
            ship_food_units += trade["ship_units"]
        row["ship_food_units"] = ship_food_units
        rows.append(row)
    return rows


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    warmup: int,
    sample_every: int,
    window: int,
    workers: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0 or window < 1:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    if workers > 1:
        sim.parallel_workers = workers
    commodities: Dict[str, CommodityDefinition] = {}
    for cid in FOOD_IDS:
        commodity = sim.commodity_registry.get_commodity(cid)
        if commodity is None:
            raise ValueError(f"{cid} missing from the commodity registry")
        commodities[cid] = commodity

    # The last turn is always sampled, so a short run still yields a final
    # row to compare the first sample against.
    rows: List[Dict[str, Any]] = []
    for index in range(turns):
        sim.run_turn()
        due = sim.current_turn > warmup and sim.current_turn % sample_every == 0
        if not (due or index == turns - 1):
            continue
        sampled = sample(sim, commodities, window)
        rows.extend(sampled)
        print(
            f"turn {sim.current_turn}: {len(sampled)} planets, "
            f"leaving {sum(r['leaving'] for r in sampled)}, "
            f"ship food units {sum(r['ship_food_units'] for r in sampled)}",
            flush=True,
        )

    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "ships": len(sim.ships),
            "warmup": warmup,
            "sample_every": sample_every,
            "window": window,
            "workers": workers,
            "hand_food_breakeven": HAND_FOOD_BREAKEVEN,
        },
        "rows": rows,
        "migration_departures": sim.migration_departures,
        "migration_arrivals": sim.migration_arrivals,
        "arrivals_by_planet": dict(arrivals_by_planet),
        "patch_calls": dict(patch_calls),
    }


def _bucket(biomass: float) -> str:
    for label, low, high in BUCKET_EDGES:
        if low < biomass <= high:
            return label
    return BUCKET_EDGES[-1][0]


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return statistics.fmean(clean) if clean else None


def _fmt(value: Optional[float], width: int, places: int = 2) -> str:
    return f"{'-':>{width}}" if value is None else f"{value:>{width}.{places}f}"


def print_table(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["rows"]
    window = result["params"]["window"]
    turns = sorted({row["turn"] for row in rows})
    first, last = turns[0], turns[-1]
    first_pop = {r["planet"]: r["population"] for r in rows if r["turn"] == first}
    final = [r for r in rows if r["turn"] == last]

    print(
        f"\nfinal sample turn {last} (first {first}), "
        f"{window}-turn window, {len(final)} planets"
    )
    header = (
        f"{'bucket':>16} {'n':>3} {'pop':>7} {'dpop':>7} {'food_px':>8} "
        f"{'pf_px':>8} {'ship_in/t':>10} {'debt':>6} {'leaving':>8}"
    )
    print(header)
    for label, _, _ in BUCKET_EDGES:
        bucket = [r for r in final if _bucket(r["biomass"]) == label]
        if not bucket:
            print(f"{label:>16} {0:>3}")
            continue
        pop = statistics.fmean([r["population"] for r in bucket])
        dpop = statistics.fmean(
            [
                r["population"] - first_pop.get(r["planet"], r["population"])
                for r in bucket
            ]
        )
        print(
            f"{label:>16} {len(bucket):>3} {_fmt(pop, 7, 1)} {_fmt(dpop, 7, 1)} "
            f"{_fmt(_mean([r['food_price'] for r in bucket]), 8)} "
            f"{_fmt(_mean([r['processed_food_price'] for r in bucket]), 8)} "
            f"{_fmt(statistics.fmean([r['ship_food_units'] for r in bucket]) / window, 10, 3)} "
            f"{_fmt(_mean([r['food_debt'] for r in bucket]), 6, 3)} "
            f"{_fmt(statistics.fmean([r['leaving'] for r in bucket]), 8, 2)}"
        )
    print(
        f"migration: departures={result['migration_departures']} "
        f"arrivals={result['migration_arrivals']} "
        f"relocations_observed={result['patch_calls'].get('relocate', 0)}"
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=600)
    parser.add_argument("--planets", type=int, default=100)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--out", type=Path, default=Path("tmp/food_security_probe.json")
    )
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        warmup=args.warmup,
        sample_every=args.sample_every,
        window=args.window,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
