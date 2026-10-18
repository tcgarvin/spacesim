"""Why en-route refuel stops are refused: the first criterion that rejects each node.

Tier-1b in-process probe. Wraps ``Ship._take_route_stop`` to see every
intermediate route node a ship passes, and re-evaluates
``TraderBrain.wants_refuel_stop``'s criteria in the order the code applies
them so each node is attributed to the first one that rejected it. Records the
tank fraction of nodes rejected on the tank gate and the ask/reference ratio
of nodes rejected on a price gate. Reads the simulation only; changes nothing.

Run:
    uv run python notebooks/ship_refuel_stop_probe.py --turns 150 --planets 40
"""

import argparse
import json
import math
from collections import Counter
from statistics import median
from typing import Any, List

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.ship import (
    DISTANCE_PER_TURN,
    FUEL_BUNKER_BUDGET_FRACTION_CHEAP,
    FUEL_BUNKER_PREMIUM,
    FUEL_STOP_TANK_FRACTION,
    REFUEL_STOP_MAX_SHORTFALL,
    Ship,
    milli_to_units_ceil,
)

REASONS = Counter()
TANK_FRACTIONS: List[float] = []
PRICE_RATIOS: List[float] = []
DEPARTURES = {"total": 0, "with_intermediate": 0}
NODES = {"passed": 0}


def _classify(brain: Any, planet: Any, refund: int, shortfall: int, stop_turns: int):
    """The first criterion in wants_refuel_stop that rejects this node.

    Mirrors the method's order exactly: fuel commodity, tank fraction, resting
    ask, price signal, 30-day average, galaxy premium, galaxy reference,
    affordability of the shortfall, fillable depth, saving against the trip.
    Returns (reason, detail) with reason "accept" when nothing rejects it.
    """
    ship = brain.ship
    fuel_commodity = brain._fuel_commodity()
    if fuel_commodity is None:
        return "no_fuel_commodity", None

    tank_after_refund = ship.fuel + refund
    tank_fraction = tank_after_refund / ship.fuel_capacity
    if tank_after_refund >= FUEL_STOP_TANK_FRACTION * ship.fuel_capacity:
        return "tank_at_or_above_fraction", tank_fraction

    market = planet.market
    _, ask = market.get_bid_ask_spread(fuel_commodity)
    if ask is None or ask <= 0:
        return "no_fuel_ask", None
    if not market.has_price_signal(fuel_commodity):
        return "no_price_signal", None

    reference = brain._fuel_value_reference()
    ratio = ask / reference if reference else None

    if ask > market.get_30_day_average_price(fuel_commodity):
        return "ask_above_local_avg30", ratio
    if reference is None:
        return "no_galaxy_reference", None
    if ask > math.ceil(reference * FUEL_BUNKER_PREMIUM):
        return "ask_above_premium", ratio
    if reference - ask <= 0:
        return "ask_at_or_above_reference", ratio

    bid = max(ask, brain._flow_value(market, fuel_commodity) or 0)
    shortfall_units = milli_to_units_ceil(shortfall)
    if ship.money < shortfall_units * bid:
        return "cannot_afford_shortfall", ratio
    fillable = brain._bunker_fillable_units(bid, planet, tank_after_refund)
    if fillable < shortfall_units + 1:
        return "fillable_too_small", ratio
    saving = (fillable - shortfall_units) * (reference - ask)
    if saving <= brain._departed_trip_turn_value * stop_turns:
        return "saving_below_trip_value", ratio
    return "accept", ratio


_orig_take = Ship._take_route_stop
_orig_start = Ship.start_journey


def _take_route_stop(self: Ship, previous_progress: float) -> bool:
    destination = self.destination
    route = self.route
    cumulative = self.route_cumulative_distance
    if destination is not None and len(route) >= 3 and len(cumulative) == len(route):
        total = cumulative[-1]
        covered = self.travel_progress * total
        previously_covered = previous_progress * total
        for index in range(1, len(route) - 1):
            reached = cumulative[index]
            if not (previously_covered < reached <= covered):
                continue
            node = route[index]
            if node is destination or node is self.planet:
                continue
            NODES["passed"] += 1
            refund = self.route_fuel_charged - self.fuel_required(reached)
            shortfall = max(0, self.fuel_required(total - reached) - refund)
            if shortfall > REFUEL_STOP_MAX_SHORTFALL:
                REASONS["shortfall_over_cap"] += 1
                continue
            stop_turns = (
                1
                + math.ceil(reached / DISTANCE_PER_TURN)
                + math.ceil((total - reached) / DISTANCE_PER_TURN)
                - math.ceil(total / DISTANCE_PER_TURN)
            )
            reason, detail = _classify(self.brain, node, refund, shortfall, stop_turns)
            REASONS[reason] += 1
            if reason == "tank_at_or_above_fraction" and detail is not None:
                TANK_FRACTIONS.append(detail)
            elif reason.startswith("ask_") and detail is not None:
                PRICE_RATIOS.append(detail)
            if reason == "accept":
                break
    return _orig_take(self, previous_progress)


def _start_journey(self: Ship, destination: Any, resuming: bool = False) -> bool:
    started = _orig_start(self, destination, resuming=resuming)
    if started:
        DEPARTURES["total"] += 1
        if len(self.route) > 2:
            DEPARTURES["with_intermediate"] += 1
    return started


Ship._take_route_stop = _take_route_stop  # type: ignore[method-assign]
Ship.start_journey = _start_journey  # type: ignore[method-assign]


def _quartiles(values: List[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "n": n,
        "p25": round(ordered[n // 4], 3),
        "median": round(median(ordered), 3),
        "p75": round(ordered[(3 * n) // 4], 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=150)
    parser.add_argument("--planets", type=int, default=40)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--makers", type=int, default=2)
    parser.add_argument("--ships", type=int, default=1)
    args = parser.parse_args()

    sim = create_and_setup_simulation(
        planets=args.planets,
        actors=args.actors,
        makers=args.makers,
        ships=args.ships,
    )
    for _ in range(args.turns):
        sim.run_turn()

    stops = sum(ship.refuel_stops for ship in sim.ships)
    print(
        json.dumps(
            {
                "turns": args.turns,
                "planets": args.planets,
                "ships": len(sim.ships),
                "departures": DEPARTURES["total"],
                "departures_with_intermediate": DEPARTURES["with_intermediate"],
                "nodes_passed": NODES["passed"],
                "stops_taken": stops,
                "reasons": dict(REASONS.most_common()),
                "tank_fraction_of_tank_rejections": _quartiles(TANK_FRACTIONS),
                "ask_over_reference_of_price_rejections": _quartiles(PRICE_RATIOS),
                "constants": {
                    "FUEL_STOP_TANK_FRACTION": FUEL_STOP_TANK_FRACTION,
                    "REFUEL_STOP_MAX_SHORTFALL": REFUEL_STOP_MAX_SHORTFALL,
                    "FUEL_BUNKER_PREMIUM": FUEL_BUNKER_PREMIUM,
                    "FUEL_BUNKER_BUDGET_FRACTION_CHEAP": (
                        FUEL_BUNKER_BUDGET_FRACTION_CHEAP
                    ),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
