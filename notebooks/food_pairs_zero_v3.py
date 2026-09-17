"""Why ships do not feed the zero-biomass planets.

Tier-1b in-process probe, a variant of
``notebooks/processed_food_export_pairs.py``. Destinations are fixed to the
planets that cannot make food by hand: ``attributes.biomass == 0.0`` (tagged
``zero``) and ``0 < biomass <= LOW_BIOMASS_MAX`` (tagged ``low``). Both food
commodities are traced, ``food`` and ``processed_food``, from the cheapest
origins that can actually supply them and have a trader docked.

Each pair is walked through the planner's real gates in order by calling the
real methods (``_trip_fuel_economics``, ``_pair_economics``,
``_origin_acquisition``, ``_evaluate_trade_opportunity``,
``_plan_acceptable``, ``_best_plan_from``); the probe only recomputes the
numbers behind each gate so the rejection can be read, and counts any
sub-gate it cannot attribute in ``gate_attribution_mismatches``.

Alongside the pair rows it records a buyer-side row per hungry planet per
sample: population, median actor money, and the resting bid book and planner
demand estimate for each food commodity.

Run:

    PYTHONPATH=/home/timg/code/spacesim2 \
    /home/timg/code/spacesim2/.venv-ft/bin/python \
        /home/timg/code/spacesim2/notebooks/food_pairs_zero_planets.py \
        --turns 400 --planets 50 --warmup 100 --sample-every 50 \
        --workers 6 --out /home/timg/code/spacesim2/tmp/food_pairs_zero.json
"""

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.commodity import CommodityDefinition
from spacesim2.core.market import Market
from spacesim2.core.navigation import Navigator, get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.ship import (
    DEMAND_HORIZON_TURNS,
    FUEL_MILLI,
    MAINTENANCE_CHANCE,
    MAINTENANCE_FUEL_MILLI,
    SELL_PRICE_HAIRCUT,
    SPECULATIVE_PLAN_CAP,
    Ship,
    ShipStatus,
    TradePlan,
    TraderBrain,
    milli_to_units_ceil,
    milli_to_units_floor,
    units_to_milli,
)
from spacesim2.core.simulation import Simulation

FOOD_IDS = ("food", "processed_food")
LOW_BIOMASS_MAX = 0.17
DEFAULT_CHEAP_ASK_MAX = 4

ship_purchases: Counter[str] = Counter()
ship_sales: Counter[str] = Counter()
arrivals: Counter[str] = Counter()
patch_calls: Counter[str] = Counter()
gate_attribution_mismatches: Counter[str] = Counter()

# Biomass tag per planet name, filled once the galaxy exists.
planet_tag: Dict[str, str] = {}
# One row per departure that decide_travel returned a destination for.
departures: List[Dict[str, Any]] = []
# One row per arrival, carrying the departure row that produced it.
arrival_rows: List[Dict[str, Any]] = []
# Ship name -> the departure row it is currently flying.
in_flight: Dict[str, Dict[str, Any]] = {}
# Ship sales of the food commodities, keyed by (planet tag, commodity id).
sales_by_tag: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
# Trip each ship is currently docked from, so sales can be credited to it.
docked_trip: Dict[str, Dict[str, Any]] = {}
# Which cap bound a food plan's quantity, by destination tag.
cap_binding: Counter[str] = Counter()
# Running sums of each cap for food plans, by destination tag.
cap_sums: Dict[str, Counter[str]] = {}
# Fleet position snapshots taken at each sample.
fleet_rows: List[Dict[str, Any]] = []


def biomass_tag(planet: Optional[Planet]) -> str:
    if planet is None:
        return "none"
    return planet_tag.get(planet.name, "rich")


def bucket_of(biomass: float) -> str:
    if biomass == 0.0:
        return "zero"
    if biomass <= LOW_BIOMASS_MAX:
        return "low"
    if biomass <= 0.4:
        return "mid"
    return "rich"


def install_patches() -> None:
    """Wrap transaction execution, travel choice and arrival, keeping behavior."""
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey
    original_travel = TraderBrain.decide_travel
    original_evaluate = TraderBrain._evaluate_trade_opportunity

    def wrapped_evaluate(
        self, origin, destination, commodity, pair=None, acquisition=None
    ):  # type: ignore[no-untyped-def]
        patch_calls["evaluate"] += 1
        plan = original_evaluate(
            self,
            origin=origin,
            destination=destination,
            commodity=commodity,
            pair=pair,
            acquisition=acquisition,
        )
        if plan is None or commodity.id not in FOOD_IDS:
            return plan
        # Recompute the four caps _evaluate_trade_opportunity took a min over,
        # using the same helpers, so the binding one can be named.
        pair_now = (
            pair if pair is not None else self._pair_economics(origin, destination)
        )
        acq = (
            acquisition
            if acquisition is not None
            else self._origin_acquisition(origin, commodity)
        )
        if pair_now is None or acq is None:
            return plan
        bid_levels = [
            (price, qty)
            for price, qty in destination.market.get_bid_levels(commodity)
            if price > acq.entry_price
        ]
        depth = sum(qty for _, qty in bid_levels)
        _, flow_qty = self._creditable_flow(
            destination.market, commodity, acq.entry_price
        )
        sellable = depth + flow_qty if depth + flow_qty > 0 else SPECULATIVE_PLAN_CAP
        caps = {
            "cash": pair_now.money_for_trading // max(1, acq.bid_price),
            "hold": pair_now.max_by_cargo,
            "sellable": sellable,
            "acquirable": acq.acquirable,
        }
        tag = biomass_tag(destination)
        binding = min(caps, key=lambda k: caps[k])
        cap_binding[f"{tag}|{commodity.id}|{binding}"] += 1
        sums = cap_sums.setdefault(f"{tag}|{commodity.id}", Counter())
        for key, value in caps.items():
            sums[key] += value
        sums["quantity"] += plan.quantity
        sums["n"] += 1
        return plan

    def wrapped_execute(
        self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
    ):  # type: ignore[no-untyped-def]
        patch_calls["execute"] += 1
        result = original_execute(
            self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
        )
        if isinstance(buyer, Ship):
            ship_purchases[commodity_type.id] += quantity
        if isinstance(seller, Ship):
            ship_sales[commodity_type.id] += quantity
            if commodity_type.id in FOOD_IDS:
                key = (biomass_tag(seller.planet), commodity_type.id)
                sales_by_tag.setdefault(key, []).append((quantity, price))
                trip = docked_trip.get(seller.name)
                if trip is not None:
                    sold = trip.setdefault("sold", {})
                    sold[commodity_type.id] = sold.get(commodity_type.id, 0) + quantity
        return result

    def wrapped_travel(self):  # type: ignore[no-untyped-def]
        patch_calls["decide_travel"] += 1
        ship = self.ship
        origin = ship.planet
        destination = original_travel(self)
        if destination is None:
            return None
        plan = self._current_plan
        plan_kind = type(plan).__name__ if plan is not None else "none"
        row: Dict[str, Any] = {
            "turn": ship.simulation.current_turn,
            "ship": ship.name,
            "origin": origin.name if origin else None,
            "destination": destination.name,
            "dest_tag": biomass_tag(destination),
            "plan_kind": plan_kind,
            "plan_loaded": self._plan_loaded,
            "distance": round(self._nav.distance(origin, destination), 3)
            if origin
            else None,
            "ship_money": ship.money,
            "cargo": {c.id: q for c, q in ship.cargo.commodities.items() if q > 0},
            "contract_hold": sum(
                c.payload.hold_units for c in ship.contracts if c.payload.hold_units > 0
            ),
            "n_contracts": len(ship.contracts),
        }
        if plan_kind == "TradePlan":
            row["plan"] = {
                "commodity": plan.commodity.id,
                "destination": plan.destination.name,
                "quantity": plan.quantity,
                "buy": plan.purchase_price_per_unit,
                "sell": plan.expected_sell_price_per_unit,
                "profit": plan.expected_profit,
            }
        elif plan_kind == "ContractPlan":
            row["plan"] = {
                "destination": plan.destination.name,
                "hold_units": plan.hold_units,
                "payment": plan.advance_total + plan.on_delivery_total,
            }
        if plan_kind == "TradePlan":
            row["loaded"] = row["cargo"].get(plan.commodity.id, 0)
        departures.append(row)
        docked_trip.pop(ship.name, None)
        in_flight[ship.name] = row
        return destination

    def wrapped_update(self):  # type: ignore[no-untyped-def]
        patch_calls["update_journey"] += 1
        arrived = original_update(self)
        if arrived and self.planet is not None:
            arrivals[self.planet.name] += 1
            dep = in_flight.pop(self.name, None)
            if dep is not None:
                docked_trip[self.name] = dep
            arrival_rows.append(
                {
                    "turn": self.simulation.current_turn,
                    "ship": self.name,
                    "planet": self.planet.name,
                    "tag": biomass_tag(self.planet),
                    "trip_kind": dep["plan_kind"] if dep else "unknown",
                    "origin": dep["origin"] if dep else None,
                    "contract_hold": dep["contract_hold"] if dep else 0,
                    "food_aboard": sum(
                        q for c, q in self.cargo.commodities.items() if c.id in FOOD_IDS
                    ),
                    "cargo_units": self.cargo.get_total_quantity(),
                    "planned_food": (
                        dep["plan"]["quantity"]
                        if dep
                        and dep.get("plan")
                        and dep["plan"].get("commodity") in FOOD_IDS
                        else 0
                    ),
                }
            )
        return arrived

    TraderBrain._evaluate_trade_opportunity = wrapped_evaluate  # type: ignore[assignment]
    Market._execute_transaction = wrapped_execute  # type: ignore[assignment]
    Ship.update_journey = wrapped_update  # type: ignore[assignment]
    TraderBrain.decide_travel = wrapped_travel  # type: ignore[assignment]


def _book(market: Market, commodity: CommodityDefinition) -> Dict[str, Any]:
    best_bid, best_ask = market.get_bid_ask_spread(commodity)
    bid_levels = market.get_bid_levels(commodity)
    ask_levels = market.get_ask_levels(commodity)
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth": sum(q for _, q in bid_levels),
        "ask_depth": sum(q for _, q in ask_levels),
        "avg_price": market.get_avg_price(commodity),
        "has_signal": market.has_price_signal(commodity),
        "flow_30d": market.get_30_day_average_volume(commodity),
    }


def trip_diagnostics(
    brain: TraderBrain, origin: Planet, destination: Planet
) -> Dict[str, Any]:
    """Every number ``_trip_fuel_economics`` and ``_pair_economics`` turn on.

    Recomputed from the ship's own helper methods so the rejection can be
    read; the verdict itself always comes from the real methods.
    """
    ship = brain.ship
    out: Dict[str, Any] = {}
    fuel_commodity = brain._fuel_commodity()
    if fuel_commodity is None:
        out["fuel_commodity"] = None
        return out

    distance = brain._nav.distance(origin, destination)
    fuel_one_way = ship.fuel_required(distance)
    fuel_round_trip = fuel_one_way * 2

    _, fuel_ask = origin.market.get_bid_ask_spread(fuel_commodity)
    fuel_price = (
        fuel_ask
        if fuel_ask is not None
        else origin.market.get_avg_price(fuel_commodity)
    )
    if fuel_price is None or fuel_price <= 0:
        fuel_price = 10

    current_fuel = ship.fuel
    round_trip_shortfall = max(0, fuel_round_trip - current_fuel)
    arrival_requirement = brain._arrival_fuel_requirement(destination, origin)
    fuel_wanted = max(fuel_round_trip, fuel_one_way + arrival_requirement)
    tank_room = ship.fuel_capacity - current_fuel
    fuel_to_buy = min(
        milli_to_units_ceil(min(fuel_wanted - current_fuel, tank_room)),
        milli_to_units_floor(tank_room),
    )
    fuel_after_arrival = current_fuel + units_to_milli(fuel_to_buy) - fuel_one_way
    fuel_cost = fuel_to_buy * fuel_price
    maintenance_cost = math.ceil(
        2 * MAINTENANCE_CHANCE * MAINTENANCE_FUEL_MILLI * fuel_price / FUEL_MILLI
    )
    fuel_left_after_trip = max(current_fuel, fuel_round_trip) - fuel_round_trip
    reserve_need = brain._fuel_reserve_need()
    refuel_shortfall = max(0, reserve_need - fuel_left_after_trip)
    refuel_floor = milli_to_units_ceil(refuel_shortfall) * fuel_price
    money_for_trading = int(
        (ship.money - fuel_cost - refuel_floor - maintenance_cost) * 0.9
    )

    out.update(
        {
            "distance": round(distance, 3),
            "fuel_one_way": milli_to_units_ceil(fuel_one_way),
            "fuel_round_trip": milli_to_units_ceil(fuel_round_trip),
            "fuel_price_at_origin": fuel_price,
            "fuel_ask_at_origin": fuel_ask,
            "ship_money": ship.money,
            "ship_fuel": milli_to_units_ceil(current_fuel),
            "ship_fuel_capacity": milli_to_units_ceil(ship.fuel_capacity),
            "cargo_space": ship.cargo_capacity - ship.cargo.get_total_quantity(),
            "round_trip_shortfall": milli_to_units_ceil(round_trip_shortfall),
            "arrival_fuel_requirement": milli_to_units_ceil(arrival_requirement),
            "fuel_to_buy": fuel_to_buy,
            "fuel_cost": fuel_cost,
            "fuel_after_arrival": milli_to_units_ceil(fuel_after_arrival),
            "fuel_purchasable_at_origin": brain._fuel_purchasable_at(origin),
            "fuel_purchasable_at_destination": brain._fuel_purchasable_at(destination),
            "local_fuel_bunkerable": brain._local_fuel_bunkerable(origin),
            "can_reach_station": brain._can_reach_station(),
            "fuel_safe_destination": brain._fuel_safe_destination(
                destination, origin, fuel_after_arrival
            ),
            "maintenance_cost": maintenance_cost,
            "fuel_reserve_need": milli_to_units_ceil(reserve_need),
            "refuel_floor": refuel_floor,
            "money_for_trading": money_for_trading,
            "ship_is_distressed": brain.is_distressed,
        }
    )
    return out


def attribute_trip_gate(diag: Dict[str, Any]) -> str:
    """Which ``_trip_fuel_economics`` gate rejected the pair, from ``diag``."""
    if diag.get("fuel_commodity", "present") is None:
        return "no_fuel_commodity"
    if diag["round_trip_shortfall"] > 0 and not diag["fuel_purchasable_at_origin"]:
        return "fuel_not_buyable_at_origin"
    if diag["fuel_to_buy"] > 0 and not diag["fuel_purchasable_at_origin"]:
        return "fuel_not_buyable_at_origin"
    if (
        diag["fuel_to_buy"] > 0
        and not diag["local_fuel_bunkerable"]
        and diag["can_reach_station"]
    ):
        return "fuel_spiked_prefer_station"
    if not diag["fuel_safe_destination"]:
        return "destination_not_fuel_safe"
    return "unattributed_trip_gate"


def destination_demand(
    brain: TraderBrain,
    destination: Planet,
    commodity: CommodityDefinition,
    entry_price: int,
) -> Dict[str, Any]:
    """Destination-side demand exactly as ``_evaluate_trade_opportunity`` sees it."""
    market = destination.market
    all_bid_levels = market.get_bid_levels(commodity)
    qualifying = [(p, q) for p, q in all_bid_levels if p > entry_price]
    depth = sum(q for _, q in qualifying)
    flow_value = brain._flow_value(market, commodity)
    flow_px = int(flow_value * SELL_PRICE_HAIRCUT) if flow_value else 0
    flow_per_turn = brain._recent_flow_per_turn(market, commodity)
    flow_qty = 0
    if flow_px > entry_price:
        flow_qty = int(flow_per_turn * DEMAND_HORIZON_TURNS)
    if depth + flow_qty > 0:
        sellable = depth + flow_qty
        sellable_source = "book_plus_flow"
    elif flow_px > entry_price:
        sellable = SPECULATIVE_PLAN_CAP
        sellable_source = "speculative_cap"
    else:
        sellable = 0
        sellable_source = "none"
    best_bid, best_ask = market.get_bid_ask_spread(commodity)
    return {
        "dest_best_bid": best_bid,
        "dest_best_ask": best_ask,
        "dest_bid_depth_total": sum(q for _, q in all_bid_levels),
        "dest_bid_depth_above_entry": depth,
        "dest_bid_levels_above_entry": qualifying[:5],
        "dest_has_price_signal": market.has_price_signal(commodity),
        "dest_avg_price": market.get_avg_price(commodity),
        "dest_flow_value": flow_value,
        "dest_flow_px_haircut": flow_px,
        "dest_flow_per_turn": round(flow_per_turn, 3),
        "dest_flow_qty": flow_qty,
        "sellable": sellable,
        "sellable_source": sellable_source,
    }


def staple_price(market: Market, commodity: CommodityDefinition) -> float:
    """What a seller could get here: best resting bid, else the clearing price."""
    best_bid, _ = market.get_bid_ask_spread(commodity)
    avg = market.get_avg_price(commodity) if market.has_price_signal(commodity) else 0
    return float(max(best_bid or 0, avg or 0))


def hungry_planets(sim: Simulation) -> List[Tuple[Planet, str, float]]:
    """Planets that cannot feed themselves by hand, with their biomass tag."""
    rows: List[Tuple[Planet, str, float]] = []
    for planet in sim.planets:
        biomass = planet.attributes.biomass
        if biomass == 0.0:
            rows.append((planet, "zero", biomass))
        elif biomass <= LOW_BIOMASS_MAX:
            rows.append((planet, "low", biomass))
    rows.sort(key=lambda row: (row[1] != "zero", row[2]))
    return rows


def sample_origin(
    brain: TraderBrain,
    nav: Navigator,
    origin: Planet,
    commodity: CommodityDefinition,
    sim: Simulation,
    targets: List[Tuple[Planet, str, float]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rows for every hungry destination from ``origin``, plus the origin summary."""
    summary: Dict[str, Any] = {
        "turn": sim.current_turn,
        "origin": origin.name,
        "commodity": commodity.id,
        "has_any_trade_signal": nav.has_any_trade_signal(),
        "exportable_here": commodity in nav.exportable_commodities(origin),
        "entry_price": None,
        "bid_price": None,
        "flow_price": None,
        "acquirable": None,
        "n_candidate_destinations": 0,
        "hungry_in_shortlist": 0,
        "best_plan_any_commodity": None,
    }
    listing_here = {
        order.commodity_type
        for order in origin.market.get_actor_orders(brain.ship)["sell"]
    }
    summary["ship_listing_commodity_here"] = commodity in listing_here

    overall = brain._best_plan_from(origin)
    if overall is not None:
        summary["best_plan_any_commodity"] = {
            "commodity": overall.commodity.id,
            "destination": overall.destination.name,
            "expected_profit": overall.expected_profit,
            "quantity": overall.quantity,
            "margin": round(overall.profit_margin, 4),
            "purchase_price": overall.purchase_price_per_unit,
            "sell_price": overall.expected_sell_price_per_unit,
        }
    if not summary["has_any_trade_signal"]:
        return [], summary
    if not summary["exportable_here"]:
        summary["blocked"] = "commodity_not_exportable_here"
        return [], summary

    acquisition = brain._origin_acquisition(origin, commodity)
    if acquisition is None:
        summary["blocked"] = "commodity_not_acquirable_here"
        return [], summary
    summary["entry_price"] = acquisition.entry_price
    summary["bid_price"] = acquisition.bid_price
    summary["flow_price"] = acquisition.flow_price
    summary["acquirable"] = acquisition.acquirable

    candidate_set = set(nav.candidate_destinations(origin, commodity))
    summary["n_candidate_destinations"] = len(candidate_set)

    # Shortlist mechanics: where each destination sits in the demand ranking
    # and in the proximity ordering the shortlist's nearest-M half walks.
    index = nav._trade_signal_index()
    demand_ranked = list(index.demand_ranked.get(commodity, ()))
    demand_rank = {planet: i for i, planet in enumerate(demand_ranked)}
    demand_set = index.demand_planets.get(commodity, frozenset())
    near_rank: Dict[Planet, int] = {}
    seen_near = 0
    for planet in nav.planets_by_proximity(origin):
        if planet in demand_set and planet is not origin:
            near_rank[planet] = seen_near
            seen_near += 1
    summary["n_demand_planets"] = len(demand_ranked)

    rows: List[Dict[str, Any]] = []
    for destination, tag, biomass in targets:
        if destination is origin:
            continue
        route = nav.route(origin, destination)
        row: Dict[str, Any] = {
            "turn": sim.current_turn,
            "origin": origin.name,
            "destination": destination.name,
            "dest_tag": tag,
            "dest_biomass": round(biomass, 4),
            "commodity": commodity.id,
            "lane_hops": max(0, len(route) - 1),
            "route_distance": round(nav.distance(origin, destination), 3),
            "in_candidate_destinations": destination in candidate_set,
            "is_demand_planet": destination in demand_set,
            "demand_rank": demand_rank.get(destination),
            "near_rank": near_rank.get(destination),
            "n_demand_planets": len(demand_ranked),
            "entry_price": acquisition.entry_price,
            "bid_price": acquisition.bid_price,
            "acquirable": acquisition.acquirable,
        }
        row.update(
            destination_demand(brain, destination, commodity, acquisition.entry_price)
        )
        diag = trip_diagnostics(brain, origin, destination)
        row["pair"] = diag

        trip = brain._trip_fuel_economics(origin, destination)
        if trip is None:
            gate = attribute_trip_gate(diag)
            if gate == "unattributed_trip_gate":
                gate_attribution_mismatches["trip_none"] += 1
            row["outcome"] = gate
            row["plan"] = None
            rows.append(row)
            continue

        pair = brain._pair_economics(origin, destination)
        if pair is None:
            if diag["money_for_trading"] > 0:
                gate_attribution_mismatches["pair_none_but_money_positive"] += 1
            row["outcome"] = "no_money_for_trading"
            row["plan"] = None
            rows.append(row)
            continue

        plan = brain._evaluate_trade_opportunity(
            origin=origin,
            destination=destination,
            commodity=commodity,
            pair=pair,
            acquisition=acquisition,
        )
        if plan is None:
            if row["sellable"] <= 0:
                row["outcome"] = "no_sellable_demand"
            elif acquisition.acquirable <= 0:
                row["outcome"] = "origin_supply_zero"
            else:
                row["outcome"] = "quantity_zero_after_budget"
            row["plan"] = None
            rows.append(row)
            continue

        accepted = brain._plan_acceptable(plan)
        row["plan"] = {
            "quantity": plan.quantity,
            "purchase_price": plan.purchase_price_per_unit,
            "sell_price": plan.expected_sell_price_per_unit,
            "expected_profit": plan.expected_profit,
            "expected_revenue": plan.expected_revenue,
            "total_purchase_cost": plan.total_purchase_cost,
            "total_fuel_cost": plan.total_fuel_cost,
            "expected_maintenance_cost": plan.expected_maintenance_cost,
            "margin": round(plan.profit_margin, 4),
            "min_margin": TradePlan.MIN_MARGIN,
            "return_leg_fuel_cost": plan.return_leg_fuel_cost,
            "accepted": accepted,
        }
        if not accepted:
            row["outcome"] = "plan_margin_rejected"
        elif commodity in listing_here:
            row["outcome"] = "skipped_ship_lists_commodity_here"
        elif not row["in_candidate_destinations"]:
            row["outcome"] = "accepted_but_not_shortlisted"
        else:
            winner = summary["best_plan_any_commodity"]
            if (
                winner is not None
                and winner["commodity"] == commodity.id
                and winner["destination"] == destination.name
            ):
                row["outcome"] = "plan_won"
            else:
                row["outcome"] = "plan_lost_ranking"
                row["winner"] = winner
        rows.append(row)

    summary["hungry_in_shortlist"] = sum(
        1 for r in rows if r["in_candidate_destinations"]
    )
    return rows, summary


def cheap_origins(
    sim: Simulation, commodity: CommodityDefinition, nav: Navigator, cheap_ask_max: int
) -> List[Tuple[Planet, int, int]]:
    """Planets the commodity can be acquired at, cheapest entry price first."""
    rows: List[Tuple[Planet, int, int]] = []
    for planet in sim.planets:
        if commodity not in nav.exportable_commodities(planet):
            continue
        _, ask = planet.market.get_bid_ask_spread(commodity)
        depth = sum(q for _, q in planet.market.get_ask_levels(commodity))
        avg = planet.market.get_avg_price(commodity)
        entry = ask if ask is not None and ask > 0 else (avg or 0)
        if entry <= 0:
            continue
        rows.append((planet, int(entry), depth))
    rows.sort(key=lambda row: (row[1] > cheap_ask_max, row[1], -row[2]))
    return rows


def pick_evaluator(sim: Simulation) -> Optional[Ship]:
    """A representative docked ship: median money among docked traders."""
    docked = [
        s
        for s in sim.ships
        if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
    ]
    pool = docked or [s for s in sim.ships if isinstance(s.brain, TraderBrain)]
    if not pool:
        return None
    pool.sort(key=lambda s: s.money)
    return pool[len(pool) // 2]


def buyer_rows(
    sim: Simulation,
    targets: List[Tuple[Planet, str, float]],
    commodities: List[CommodityDefinition],
    brain: TraderBrain,
    entry_by_commodity: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Per hungry planet: who is buying, at what price, and what the planner sees."""
    rows: List[Dict[str, Any]] = []
    for planet, tag, biomass in targets:
        residents = [a for a in sim.actors if a.planet is planet]
        money = sorted(a.money for a in residents)
        row: Dict[str, Any] = {
            "turn": sim.current_turn,
            "planet": planet.name,
            "tag": tag,
            "biomass": round(biomass, 4),
            "population": len(residents),
            "median_actor_money": money[len(money) // 2] if money else None,
            "ships_docked": len(planet.ships),
        }
        resident_set = set(residents)
        for commodity in commodities:
            market = planet.market
            bid_levels = market.get_bid_levels(commodity)
            entry = entry_by_commodity.get(commodity.id, 0)
            live_bids = [
                o for o in market.buy_orders.get(commodity, []) if not o.cancelled
            ]
            bidders = {id(o.actor): o.actor for o in live_bids}.values()
            bidder_money = sorted(getattr(a, "money", 0) for a in bidders)
            bidder_reserved = sum(getattr(a, "reserved_money", 0) for a in bidders)
            bid_value = sum(o.quantity * o.price for o in live_bids)
            ages = [sim.current_turn - o.created_turn for o in live_bids]
            resident_bid_qty = sum(
                o.quantity for o in live_bids if o.actor in resident_set
            )
            demand = destination_demand(brain, planet, commodity, entry)
            avg = market.get_avg_price(commodity)
            row[commodity.id] = {
                "bid_levels": len(bid_levels),
                "bid_depth": sum(q for _, q in bid_levels),
                "best_bid": bid_levels[0][0] if bid_levels else None,
                "ask_depth": sum(q for _, q in market.get_ask_levels(commodity)),
                "avg_price": avg,
                "has_signal": market.has_price_signal(commodity),
                "stock_held": sum(
                    a.inventory.get_quantity(commodity) for a in residents
                ),
                "assumed_entry_price": entry,
                "bid_depth_above_entry": demand["dest_bid_depth_above_entry"],
                "flow_per_turn": demand["dest_flow_per_turn"],
                "flow_qty": demand["dest_flow_qty"],
                "haircut_flow_px": demand["dest_flow_px_haircut"],
                "haircut_beats_entry": demand["dest_flow_px_haircut"] > entry,
                "sellable": demand["sellable"],
                "sellable_source": demand["sellable_source"],
                "n_bidders": len(bidder_money),
                "bidder_money_median": (
                    bidder_money[len(bidder_money) // 2] if bidder_money else None
                ),
                "bidder_money_total": sum(bidder_money),
                "bidder_reserved_total": bidder_reserved,
                "bid_value": bid_value,
                "bid_age_median": _median(ages),
                "bid_age_max": max(ages) if ages else None,
                "resident_bid_qty": resident_bid_qty,
                "volume_30d": market.get_30_day_average_volume(commodity),
            }
        rows.append(row)
    return rows


def sample(
    sim: Simulation,
    commodities: List[CommodityDefinition],
    cheap_ask_max: int,
    max_origins: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    fallback = pick_evaluator(sim)
    if fallback is None:
        return [], [], []
    nav = get_navigator(sim)
    targets = hungry_planets(sim)
    pair_rows: List[Dict[str, Any]] = []
    origin_rows: List[Dict[str, Any]] = []
    entry_by_commodity: Dict[str, int] = {}

    for commodity in commodities:
        origins = cheap_origins(sim, commodity, nav, cheap_ask_max)
        with_ships = [
            row
            for row in origins
            if any(
                s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
                for s in row[0].ships
            )
        ]
        selected = (with_ships or origins)[:max_origins]
        if selected:
            entry_by_commodity[commodity.id] = selected[0][1]
        for planet, entry_seen, depth in selected:
            local_ships = [
                s
                for s in planet.ships
                if s.status is ShipStatus.DOCKED and isinstance(s.brain, TraderBrain)
            ]
            local = max(local_ships, key=lambda s: s.money) if local_ships else None
            evaluator = local.brain if local is not None else fallback.brain
            rows, summary = sample_origin(
                evaluator, nav, planet, commodity, sim, targets
            )
            summary.update(
                {
                    "ask_depth": depth,
                    "origin_entry_seen": entry_seen,
                    "ships_docked": len(planet.ships),
                    "evaluator_is_local": local is not None,
                    "evaluator_money": evaluator.ship.money,
                    "evaluator_fuel": milli_to_units_ceil(evaluator.ship.fuel),
                    "book": _book(planet.market, commodity),
                    "n_sampled_destinations": len(rows),
                }
            )
            for row in rows:
                row["origin_ask_depth"] = depth
                row["evaluator_is_local"] = local is not None
            pair_rows.extend(rows)
            origin_rows.append(summary)

    # Where the fleet is: docked at a cheap food origin, docked elsewhere, or
    # in flight. The cheap origins are the ones this sample selected for food.
    cheap_names = {r["origin"] for r in origin_rows if r["commodity"] == "food"}
    fleet: Counter[str] = Counter()
    for ship in sim.ships:
        if not isinstance(ship.brain, TraderBrain):
            continue
        fleet["fleet"] += 1
        if ship.status is not ShipStatus.DOCKED:
            fleet["in_flight"] += 1
        elif ship.planet is not None and ship.planet.name in cheap_names:
            fleet["docked_cheap_food_origin"] += 1
        else:
            fleet["docked_elsewhere"] += 1
            if ship.planet is not None:
                fleet[f"docked_{biomass_tag(ship.planet)}"] += 1
    fleet_rows.append(
        {
            "turn": sim.current_turn,
            "n_cheap_origins": len(cheap_names),
            **dict(fleet),
        }
    )

    buyers = buyer_rows(sim, targets, commodities, fallback.brain, entry_by_commodity)
    return pair_rows, origin_rows, buyers


def run_probe(
    turns: int,
    planets: int,
    actors: int,
    ships: int,
    sample_every: int,
    warmup: int,
    cheap_ask_max: int,
    max_origins: int,
    workers: int,
) -> Dict[str, Any]:
    if turns < 1 or planets < 1 or sample_every < 1 or warmup < 0:
        raise ValueError("invalid probe parameters")
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    for planet in sim.planets:
        planet_tag[planet.name] = bucket_of(planet.attributes.biomass)
    if workers > 1:
        sim.parallel_workers = workers
    commodities = []
    for commodity_id in FOOD_IDS:
        commodity = sim.commodity_registry.get_commodity(commodity_id)
        if commodity is None:
            raise ValueError(f"{commodity_id} missing from the commodity registry")
        commodities.append(commodity)

    pair_rows: List[Dict[str, Any]] = []
    origin_rows: List[Dict[str, Any]] = []
    buyer_snapshots: List[Dict[str, Any]] = []
    for _ in range(min(warmup, turns)):
        sim.run_turn()
    for _ in range(max(0, turns - warmup)):
        sim.run_turn()
        if sim.current_turn % sample_every == 0:
            pairs, origins, buyers = sample(
                sim, commodities, cheap_ask_max, max_origins
            )
            pair_rows.extend(pairs)
            origin_rows.extend(origins)
            buyer_snapshots.extend(buyers)
            outcomes = Counter(r["outcome"] for r in pairs)
            print(
                f"turn {sim.current_turn}: {len(origins)} origins, "
                f"{len(pairs)} pairs, {len(buyers)} hungry planets, "
                f"outcomes {dict(outcomes)}",
                flush=True,
            )

    if (
        patch_calls["execute"] == 0
        or patch_calls["update_journey"] == 0
        or patch_calls["decide_travel"] == 0
        or patch_calls["evaluate"] == 0
    ):
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    hungry = hungry_planets(sim)
    origin_names = {r["origin"] for r in origin_rows}
    dest_names = {r["destination"] for r in pair_rows}
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "sample_every": sample_every,
            "warmup": warmup,
            "cheap_ask_max": cheap_ask_max,
            "max_origins": max_origins,
            "workers": workers,
            "ships": len(sim.ships),
            "commodities": list(FOOD_IDS),
            "low_biomass_max": LOW_BIOMASS_MAX,
        },
        "hungry_planets": [
            {"planet": p.name, "tag": tag, "biomass": round(b, 4)}
            for p, tag, b in hungry
        ],
        "pair_rows": pair_rows,
        "origin_rows": origin_rows,
        "buyer_rows": buyer_snapshots,
        "ship_purchases": dict(ship_purchases.most_common()),
        "ship_sales": dict(ship_sales.most_common()),
        "arrivals_total": sum(arrivals.values()),
        "arrivals_at_origins": sum(arrivals[name] for name in origin_names),
        "arrivals_at_hungry": {p.name: arrivals[p.name] for p, _, _ in hungry},
        "origins_ever": sorted(origin_names),
        "destinations_ever": sorted(dest_names),
        "gate_attribution_mismatches": dict(gate_attribution_mismatches),
        "patch_calls": dict(patch_calls),
        "planet_tags": dict(planet_tag),
        "cap_binding": dict(cap_binding),
        "cap_sums": {k: dict(v) for k, v in cap_sums.items()},
        "fleet_rows": fleet_rows,
        "departures": departures,
        "arrival_rows": arrival_rows,
        "sales_by_tag": {
            f"{tag}|{cid}": {
                "units": sum(q for q, _ in rows),
                "fills": len(rows),
                "mean_price": round(
                    sum(q * p for q, p in rows) / max(1, sum(q for q, _ in rows)), 2
                ),
            }
            for (tag, cid), rows in sales_by_tag.items()
        },
    }


def _median(values: List[Any]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def print_table(result: Dict[str, Any]) -> None:
    pairs: List[Dict[str, Any]] = result["pair_rows"]
    origins: List[Dict[str, Any]] = result["origin_rows"]
    buyers: List[Dict[str, Any]] = result["buyer_rows"]
    hungry = result["hungry_planets"]
    print(
        f"\nhungry planets: {sum(1 for h in hungry if h['tag'] == 'zero')} zero-biomass, "
        f"{sum(1 for h in hungry if h['tag'] == 'low')} low"
    )
    print("  " + ", ".join(f"{h['planet']}({h['tag']},{h['biomass']})" for h in hungry))
    print(
        f"{len(origins)} origin-observations, {len(pairs)} (origin, destination) pairs"
    )
    if not pairs:
        print("no pairs sampled")
        return

    for commodity_id in result["params"]["commodities"]:
        subset = [r for r in pairs if r["commodity"] == commodity_id]
        if not subset:
            print(f"\n{commodity_id}: no pairs")
            continue
        for tag in ("zero", "low"):
            tagged = [r for r in subset if r["dest_tag"] == tag]
            if not tagged:
                continue
            outcomes = Counter(r["outcome"] for r in tagged)
            total = len(tagged)
            print(f"\n{commodity_id} -> {tag}-biomass destinations ({total} pairs)")
            print(f"{'outcome':>34} {'count':>6} {'share':>6}")
            for outcome, count in outcomes.most_common():
                print(f"{outcome:>34} {count:>6} {100.0 * count / total:>5.0f}%")
            print(
                "  in shortlist {c}/{t}; median sellable={s} depth>entry={d} "
                "flow_qty={f} entry={e} acquirable={a} dest_avg={av} best_bid={b}".format(
                    c=sum(1 for r in tagged if r["in_candidate_destinations"]),
                    t=total,
                    s=_median([r["sellable"] for r in tagged]),
                    d=_median([r["dest_bid_depth_above_entry"] for r in tagged]),
                    f=_median([r["dest_flow_qty"] for r in tagged]),
                    e=_median([r["entry_price"] for r in tagged]),
                    a=_median([r["acquirable"] for r in tagged]),
                    av=_median([r["dest_avg_price"] for r in tagged]),
                    b=_median([r["dest_best_bid"] for r in tagged]),
                )
            )
            planned = [r for r in tagged if r.get("plan")]
            if planned:
                print(
                    "  plans built={n} accepted={a} median qty={q} buy={bu} sell={s} "
                    "profit={pr} margin={mg}".format(
                        n=len(planned),
                        a=sum(1 for r in planned if r["plan"]["accepted"]),
                        q=_median([r["plan"]["quantity"] for r in planned]),
                        bu=_median([r["plan"]["purchase_price"] for r in planned]),
                        s=_median([r["plan"]["sell_price"] for r in planned]),
                        pr=_median([r["plan"]["expected_profit"] for r in planned]),
                        mg=_median([r["plan"]["margin"] for r in planned]),
                    )
                )
            lost = [r for r in tagged if r.get("winner")]
            if lost:
                print(
                    "  lost ranking {n}: median own profit={o} vs winner profit={w}; "
                    "winners {c}".format(
                        n=len(lost),
                        o=_median([r["plan"]["expected_profit"] for r in lost]),
                        w=_median([r["winner"]["expected_profit"] for r in lost]),
                        c=dict(
                            Counter(r["winner"]["commodity"] for r in lost).most_common(
                                4
                            )
                        ),
                    )
                )

    print("\nbuyer side at hungry planets (one row per planet per sample)")
    header = (
        f"{'planet':>12} {'tag':>5} {'turn':>5} {'pop':>4} {'money':>7} "
        f"{'f_bids':>6} {'f_depth':>7} {'f_best':>6} {'f_avg':>6} {'f_sell':>6} "
        f"{'pf_bids':>7} {'pf_depth':>8} {'pf_best':>7} {'pf_avg':>6} {'pf_sell':>7} "
        f"{'arr':>4}"
    )
    print(header)
    arrivals_by_planet = result["arrivals_at_hungry"]
    for row in sorted(
        buyers, key=lambda r: (r["tag"] != "zero", r["planet"], r["turn"])
    ):
        f = row["food"]
        pf = row["processed_food"]
        print(
            f"{row['planet'][:12]:>12} {row['tag']:>5} {row['turn']:>5} "
            f"{row['population']:>4} {str(row['median_actor_money']):>7} "
            f"{f['bid_levels']:>6} {f['bid_depth']:>7} {str(f['best_bid']):>6} "
            f"{str(f['avg_price']):>6} {f['sellable']:>6} "
            f"{pf['bid_levels']:>7} {pf['bid_depth']:>8} {str(pf['best_bid']):>7} "
            f"{str(pf['avg_price']):>6} {pf['sellable']:>7} "
            f"{arrivals_by_planet.get(row['planet'], 0):>4}"
        )

    zero_rows = [r for r in buyers if r["tag"] == "zero"]
    if zero_rows:
        for cid in result["params"]["commodities"]:
            sub = [r[cid] for r in zero_rows]
            print(
                "zero planets, {c}: median bid depth={d} best bid={b} "
                "depth>entry={e} flow_qty={f} haircut>entry {h}/{n} "
                "speculative_cap {s}/{n} stock_held={k}".format(
                    c=cid,
                    d=_median([x["bid_depth"] for x in sub]),
                    b=_median([x["best_bid"] for x in sub]),
                    e=_median([x["bid_depth_above_entry"] for x in sub]),
                    f=_median([x["flow_qty"] for x in sub]),
                    h=sum(1 for x in sub if x["haircut_beats_entry"]),
                    n=len(sub),
                    s=sum(1 for x in sub if x["sellable_source"] == "speculative_cap"),
                    k=_median([x["stock_held"] for x in sub]),
                )
            )

    print("\nQ2 bidder detail at hungry planets (median over samples)")
    print(
        f"{'tag':>5} {'cid':>15} {'bidders':>8} {'bid_qty':>8} {'bid_val':>8} "
        f"{'b_money_med':>11} {'b_money_tot':>11} {'reserved':>9} "
        f"{'age_med':>8} {'age_max':>8} {'vol30':>7} {'res_qty':>8}"
    )
    for tag in ("zero", "low"):
        sub_rows = [r for r in buyers if r["tag"] == tag]
        for cid in result["params"]["commodities"]:
            sub = [r[cid] for r in sub_rows]
            if not sub:
                continue
            print(
                f"{tag:>5} {cid:>15} {_median([x['n_bidders'] for x in sub]):>8} "
                f"{_median([x['bid_depth'] for x in sub]):>8} "
                f"{_median([x['bid_value'] for x in sub]):>8} "
                f"{_median([x['bidder_money_median'] for x in sub]):>11} "
                f"{_median([x['bidder_money_total'] for x in sub]):>11} "
                f"{_median([x['bidder_reserved_total'] for x in sub]):>9} "
                f"{_median([x['bid_age_median'] for x in sub]):>8} "
                f"{_median([x['bid_age_max'] for x in sub]):>8} "
                f"{_median([x['volume_30d'] for x in sub]):>7} "
                f"{_median([x['resident_bid_qty'] for x in sub]):>8}"
            )

    print("\nQ3 executed food trade plans by destination bucket")
    deps = result["departures"]
    food_deps = [
        d
        for d in deps
        if d["plan_kind"] == "TradePlan"
        and d.get("plan")
        and d["plan"].get("commodity") in FOOD_IDS
    ]
    print(
        f"{'bucket':>7} {'trips':>6} {'qty_med':>8} {'qty_tot':>8} {'buy':>6} "
        f"{'sell':>6} {'profit':>8} {'dist_med':>9}"
    )
    for bucket in ("zero", "low", "mid", "rich"):
        sub = [d for d in food_deps if d["dest_tag"] == bucket]
        if not sub:
            print(f"{bucket:>7} {0:>6}")
            continue
        print(
            f"{bucket:>7} {len(sub):>6} "
            f"{_median([d['plan']['quantity'] for d in sub]):>8} "
            f"{sum(d['plan']['quantity'] for d in sub):>8} "
            f"{_median([d['plan']['buy'] for d in sub]):>6} "
            f"{_median([d['plan']['sell'] for d in sub]):>6} "
            f"{_median([d['plan']['profit'] for d in sub]):>8} "
            f"{_median([d['distance'] for d in sub]):>9}"
        )
    print("food units actually sold by ships, by planet bucket:")
    for key, val in sorted(result["sales_by_tag"].items()):
        print(f"  {key:>22}: {val}")

    print("\nQ3b planned vs loaded vs sold, executed food trips")
    print(
        f"{'bucket':>7} {'trips':>6} {'planned':>8} {'loaded':>7} {'sold':>7} "
        f"{'load_frac':>10} {'sell_frac':>10}"
    )
    for bucket in ("zero", "low", "mid", "rich"):
        sub = [d for d in food_deps if d["dest_tag"] == bucket]
        if not sub:
            continue
        planned = sum(d["plan"]["quantity"] for d in sub)
        loaded = sum(d.get("loaded", 0) for d in sub)
        sold = sum(sum(d.get("sold", {}).values()) for d in sub)
        print(
            f"{bucket:>7} {len(sub):>6} {planned:>8} {loaded:>7} {sold:>7} "
            f"{loaded / max(1, planned):>10.2f} {sold / max(1, loaded):>10.2f}"
        )

    print("\nQ3c which cap bound a food plan's quantity (all evaluations)")
    binding = result["cap_binding"]
    for key in sorted(k for k in binding if k.split("|")[0] in ("zero", "low")):
        print(f"  {key:>32}: {binding[key]}")
    print("mean caps per food plan:")
    for key, sums in sorted(result["cap_sums"].items()):
        if key.split("|")[0] not in ("zero", "low"):
            continue
        n = max(1, sums.get("n", 0))
        print(
            f"  {key:>22}: n={sums.get('n', 0)} cash={sums.get('cash', 0) / n:.0f} "
            f"hold={sums.get('hold', 0) / n:.0f} sellable={sums.get('sellable', 0) / n:.0f} "
            f"acquirable={sums.get('acquirable', 0) / n:.0f} "
            f"quantity={sums.get('quantity', 0) / n:.0f}"
        )

    print("\nQ3d share of all fleet departures carrying food")
    all_deps = result["departures"]
    with_food = [
        d for d in all_deps if any(cid in FOOD_IDS for cid in d.get("cargo", {}))
    ]
    print(
        f"departures={len(all_deps)} carrying food={len(with_food)} "
        f"({100.0 * len(with_food) / max(1, len(all_deps)):.0f}%)"
    )
    print(f"{'bucket':>7} {'departures':>11} {'with_food':>10} {'food_units':>11}")
    for bucket in ("zero", "low", "mid", "rich"):
        sub = [d for d in all_deps if d["dest_tag"] == bucket]
        subf = [d for d in sub if any(c in FOOD_IDS for c in d.get("cargo", {}))]
        print(
            f"{bucket:>7} {len(sub):>11} {len(subf):>10} "
            f"{sum(q for d in subf for c, q in d['cargo'].items() if c in FOOD_IDS):>11}"
        )

    print("\nQ5b fleet position at each sample")
    print(
        f"{'turn':>5} {'fleet':>6} {'in_flight':>10} {'at_cheap_food':>14} "
        f"{'elsewhere':>10} {'n_cheap':>8}"
    )
    for row in result["fleet_rows"]:
        print(
            f"{row['turn']:>5} {row.get('fleet', 0):>6} {row.get('in_flight', 0):>10} "
            f"{row.get('docked_cheap_food_origin', 0):>14} "
            f"{row.get('docked_elsewhere', 0):>10} {row['n_cheap_origins']:>8}"
        )

    print("\nQ4 shortlist membership for hungry destinations")
    print(
        f"{'cid':>15} {'tag':>5} {'pairs':>6} {'in_short':>9} {'is_demand':>10} "
        f"{'demand_rank':>12} {'near_rank':>10} {'n_demand':>9}"
    )
    for cid in result["params"]["commodities"]:
        for tag in ("zero", "low"):
            sub = [r for r in pairs if r["commodity"] == cid and r["dest_tag"] == tag]
            if not sub:
                continue
            print(
                f"{cid:>15} {tag:>5} {len(sub):>6} "
                f"{sum(1 for r in sub if r['in_candidate_destinations']):>9} "
                f"{sum(1 for r in sub if r.get('is_demand_planet')):>10} "
                f"{str(_median([r.get('demand_rank') for r in sub])):>12} "
                f"{str(_median([r.get('near_rank') for r in sub])):>10} "
                f"{str(_median([r.get('n_demand_planets') for r in sub])):>9}"
            )

    print("\nQ5 arrivals by destination bucket and trip kind")
    arr = result["arrival_rows"]
    kinds = sorted({a["trip_kind"] for a in arr})
    print(f"{'bucket':>7} " + " ".join(f"{k:>12}" for k in kinds) + f" {'total':>7}")
    for bucket in ("zero", "low", "mid", "rich"):
        sub = [a for a in arr if a["tag"] == bucket]
        counts = Counter(a["trip_kind"] for a in sub)
        print(
            f"{bucket:>7} "
            + " ".join(f"{counts.get(k, 0):>12}" for k in kinds)
            + f" {len(sub):>7}"
        )
    print(
        f"{'bucket':>7} {'arrivals':>9} {'w_food':>7} {'food_u':>7} "
        f"{'w_contract':>11} {'contract_u':>11} {'cargo_med':>10}"
    )
    for bucket in ("zero", "low", "mid", "rich"):
        sub = [a for a in arr if a["tag"] == bucket]
        if not sub:
            continue
        print(
            f"{bucket:>7} {len(sub):>9} "
            f"{sum(1 for a in sub if a['food_aboard'] > 0):>7} "
            f"{sum(a['food_aboard'] for a in sub):>7} "
            f"{sum(1 for a in sub if a['contract_hold'] > 0):>11} "
            f"{sum(a['contract_hold'] for a in sub):>11} "
            f"{_median([a['cargo_units'] for a in sub]):>10}"
        )
    contract_arr = [a for a in arr if a["contract_hold"] > 0]
    print(
        "contract-hold arrivals carrying food: "
        f"{sum(1 for a in contract_arr if a['food_aboard'] > 0)}/{len(contract_arr)}"
    )

    winners = Counter(
        r["best_plan_any_commodity"]["commodity"]
        for r in origins
        if r["best_plan_any_commodity"]
    )
    print(
        f"\n_best_plan_from winners at sampled origins: {dict(winners.most_common())}"
    )
    print("ship purchases:", result["ship_purchases"])
    print("ship sales:", result["ship_sales"])
    print("arrivals at hungry planets:", result["arrivals_at_hungry"])
    if result["gate_attribution_mismatches"]:
        print(
            "WARNING gate attribution mismatches:",
            result["gate_attribution_mismatches"],
        )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--cheap-ask-max", type=int, default=DEFAULT_CHEAP_ASK_MAX)
    parser.add_argument("--max-origins", type=int, default=6)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        sample_every=args.sample_every,
        warmup=args.warmup,
        cheap_ask_max=args.cheap_ask_max,
        max_origins=args.max_origins,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print_table(result)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
