"""Why a delivered lot does not sell at the planet it was flown to.

Tier-1b in-process probe, the successor to ``notebooks/plan_claim_probe.py``,
which found that 367 of 454 executed trade plans sold nothing at the
destination. This one follows the destination order book instead of the money.

Per plan departure it records the book the plan was priced against - the
inputs ``TraderBrain._evaluate_trade_opportunity`` (ship.py:2219-2243) reads:
resting bid levels above the entry price, flow price, flow units per turn,
the ``flow_qty`` demand credit, the resulting ``sellable``, and the plan's
``expected_sell_price_per_unit``. On arrival it records the same book again,
plus the resting asks of competing sellers and the recent clearing volume.
For every turn the ship then stays docked it records the book, the ship's own
resting asks, its cost-basis floor (``_sell_floor_price``), ``unsold_turns``,
and the units of the lot that cleared that turn.

Each arrival is then classified:

* ``no_bids`` - no resting bid at any price on any docked turn
* ``bids_below_floor`` - bids exist, but the best bid never reaches the
  ship's floor, so ``_place_flow_sell_orders`` cannot price into them
* ``asked_at_bid_unfilled`` - the ship rested an ask at or below the best bid
  and still did not fill
* ``never_listed`` - the ship left without ever resting an ask here
* ``sold`` - some of the lot cleared at the destination

Run:

    PYTHONPATH=/home/timg/code/spacesim2 \
    /home/timg/code/spacesim2/.venv-ft/bin/python \
        /home/timg/code/spacesim2/notebooks/arrival_book_probe.py \
        --turns 400 --planets 50 --warmup 100 \
        --workers 12 --out /home/timg/code/spacesim2/tmp/arrival_book.json
"""

import argparse
import json
import math
import statistics
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from spacesim2.core.market import Market
from spacesim2.core.ship import (
    FUEL_MILLI,
    DEMAND_HORIZON_TURNS,
    FLOW_SHARE_EXTERNAL,
    DISTANCE_PER_TURN,
    SELL_PRICE_HAIRCUT,
    SPECULATIVE_PLAN_CAP,
    Ship,
    ShipStatus,
    TradePlan,
    TraderBrain,
    units_to_milli,
)

from spacesim2.cli.common import create_and_setup_simulation  # isort: skip

FOOD_IDS = ("food", "processed_food")
MAX_DOCKED_ROWS = 15

_LOCK = threading.Lock()
patch_calls: Counter = Counter()

# ship name -> commodity id -> FIFO list of {qty, price, dep}
hold: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
    lambda: defaultdict(list)
)
departures: Dict[int, Dict[str, Any]] = {}
open_deps: Dict[str, List[int]] = defaultdict(list)
_next_dep_id = [0]
market_planets: Dict[int, str] = {}
planets_by_name: Dict[str, Any] = {}
# (due_turn, planet name, commodity id, depth at hold, bid ages) follow-ups
clearing_followups: List[Dict[str, Any]] = []
clearing_rows: List[Dict[str, Any]] = []


def _travel_turns(distance: float) -> int:
    return max(1, math.ceil(distance / DISTANCE_PER_TURN))


def _book(brain: TraderBrain, market: Market, commodity, entry_price: int) -> Dict:
    """The demand estimate's own inputs, as ship.py:2219-2243 computes them."""
    levels = market.get_bid_levels(commodity)
    above = [(p, q) for p, q in levels if p > entry_price]
    depth_above = sum(q for _, q in above)
    flow_value = brain._flow_value(market, commodity)
    flow_px = int(flow_value * SELL_PRICE_HAIRCUT) if flow_value else 0
    per_turn = brain._recent_flow_per_turn(market, commodity)
    flow_qty = int(per_turn * DEMAND_HORIZON_TURNS) if flow_px > entry_price else 0
    if depth_above + flow_qty > 0:
        sellable = depth_above + flow_qty
    elif flow_px > entry_price:
        sellable = SPECULATIVE_PLAN_CAP
    else:
        sellable = 0
    asks = market.get_ask_levels(commodity)
    recent = market.volume_history.get(commodity, [])[-10:]
    return {
        "best_bid": levels[0][0] if levels else 0,
        "bid_depth": sum(q for _, q in levels),
        "bid_levels": [[p, q] for p, q in levels[:3]],
        "depth_above_entry": depth_above,
        "flow_px": flow_px,
        "flow_per_turn": round(per_turn, 2),
        "flow_qty": flow_qty,
        "sellable": sellable,
        "speculative": depth_above + flow_qty == 0 and sellable > 0,
        "best_ask": asks[0][0] if asks else 0,
        "ask_depth": sum(q for _, q in asks),
        "clearing_10t": sum(recent),
        "avg_price": market.get_avg_price(commodity),
    }


def _value_split(
    brain: TraderBrain, market: Market, commodity, quantity: int, basis: float
) -> Dict[str, Any]:
    """``_realizable_value`` (ship.py:1387) split into its two sources."""
    levels = market.get_bid_levels(commodity)
    best_bid = levels[0][0] if levels else 0
    flow_px, flow_qty = brain._creditable_flow(market, commodity, basis)
    remaining = quantity
    bid_value = 0
    bid_units = 0
    for price, level_qty in levels:
        if remaining <= 0:
            break
        take = min(level_qty, remaining)
        bid_value += take * price
        bid_units += take
        remaining -= take
    flow_value = 0
    flow_units = 0
    if remaining > 0 and flow_px > 0:
        rest_price = min(flow_px, best_bid) if best_bid else flow_px
        flow_units = min(remaining, flow_qty)
        flow_value = flow_units * rest_price
    return {
        "total": bid_value + flow_value,
        "bid_value": bid_value,
        "bid_units": bid_units,
        "flow_value": flow_value,
        "flow_units": flow_units,
        "flow_px": flow_px,
        "flow_credit_units": flow_qty,
        "best_bid": best_bid,
        "bid_depth": sum(q for _, q in levels),
    }


def _bid_ages(market: Market, commodity, turn: int) -> Dict[str, Any]:
    """Age in turns of the resting bids a forecast walks."""
    orders = [o for o in market.buy_orders.get(commodity, []) if not o.cancelled]
    if not orders:
        return {"n": 0, "median_age": None, "depth": 0, "max_age": None}
    ages = sorted((turn - o.created_turn, o.quantity) for o in orders)
    depth = sum(q for _, q in ages)
    return {
        "n": len(orders),
        "median_age": statistics.median([a for a, _ in ages]),
        "max_age": ages[-1][0],
        "depth": depth,
    }


def _winning_remote(ship: Ship, commodity, quantity: int) -> Optional[Dict[str, Any]]:
    """Replay the hold loop of ``_cargo_disposition`` (ship.py:3085) for one good.

    Returns the first remote planet that beats the local forecast by the same
    1.15 margin, with both forecasts split into bid walk and creditable flow.
    """
    brain = ship.brain
    planet = ship.planet
    if planet is None:
        return None
    market = planet.market
    fuel_commodity = brain._fuel_commodity()
    if fuel_commodity is None:
        return None
    basis = brain.basis_per_unit(commodity)
    basis_value = basis if basis is not None else 0.0
    local = _value_split(brain, market, commodity, quantity, basis_value)
    fuel_in_tank = ship.fuel
    _, local_fuel_ask = market.get_bid_ask_spread(fuel_commodity)
    fuel_available = fuel_in_tank + units_to_milli(brain._affordable_local_fuel(planet))
    for other in ship.simulation.planets:
        if other is planet:
            continue
        required_fuel = brain._departure_fuel_requirement(other)
        if fuel_available < required_fuel:
            continue
        remote = _value_split(brain, other.market, commodity, quantity, basis_value)
        if remote["total"] <= 0:
            continue
        fuel_needed = ship.fuel_required(brain._nav.distance(planet, other))
        origin_fuel_price = market.get_avg_price(fuel_commodity) or 10
        from_tank = min(fuel_needed, fuel_in_tank)
        to_buy = fuel_needed - from_tank
        fuel_cost = (
            from_tank * origin_fuel_price
            + to_buy * (local_fuel_ask or origin_fuel_price)
        ) / FUEL_MILLI
        if remote["total"] - fuel_cost > local["total"] * 1.15:
            turn = ship.simulation.current_turn
            return {
                "planet": other.name,
                "turn": turn,
                "quantity": quantity,
                "basis": round(basis_value, 2),
                "fuel_cost": round(fuel_cost, 1),
                "local": local,
                "remote": remote,
                "remote_bid_ages": _bid_ages(other.market, commodity, turn),
            }
    return None


def _record_buy(ship: Ship, commodity_id: str, quantity: int, price: int) -> None:
    with _LOCK:
        hold[ship.name][commodity_id].append(
            {"qty": quantity, "price": price, "dep": None}
        )


def _record_sale(ship: Ship, commodity, quantity: int, price: int, here: str) -> None:
    turn = ship.simulation.current_turn
    with _LOCK:
        queue = hold[ship.name][commodity.id]
        remaining = quantity
        while remaining > 0 and queue:
            lot = queue[0]
            used = min(remaining, lot["qty"])
            lot["qty"] -= used
            remaining -= used
            dep_id = lot["dep"]
            if dep_id is not None:
                dep = departures[dep_id]
                if here == dep["destination"]:
                    dep["dest_units"] += used
                    dep["dest_revenue"] += used * price
                    dep["sold_by_turn"].setdefault(str(turn), 0)
                    dep["sold_by_turn"][str(turn)] += used
                else:
                    dep["other_units"] += used
                    dep["other_revenue"] += used * price
                    if len(dep["other_sales"]) < 20:
                        dep["other_sales"].append(
                            {"planet": here, "turn": turn, "qty": used, "price": price}
                        )
            if lot["qty"] <= 0:
                queue.pop(0)


def _open_arrival(ship: Ship, planet_name: str) -> Optional[Dict[str, Any]]:
    """The departure whose lot this ship is currently sitting on, if any."""
    for dep_id in reversed(open_deps[ship.name]):
        dep = departures[dep_id]
        if dep["arrived"] and not dep["closed"] and dep["destination"] == planet_name:
            return dep
    return None


def _record_departure(ship: Ship, plan: TradePlan) -> None:
    commodity = plan.commodity
    brain = ship.brain
    entry = plan.purchase_price_per_unit
    book = _book(brain, plan.destination.market, commodity, entry)
    with _LOCK:
        dep_id = _next_dep_id[0]
        _next_dep_id[0] += 1
        tagged = 0
        for lot in hold[ship.name][commodity.id]:
            if lot["dep"] is not None or lot["qty"] <= 0:
                continue
            lot["dep"] = dep_id
            tagged += lot["qty"]
        departures[dep_id] = {
            "id": dep_id,
            "turn": ship.simulation.current_turn,
            "ship": ship.name,
            "origin": plan.origin.name,
            "destination": plan.destination.name,
            "commodity": commodity.id,
            "planned_qty": plan.quantity,
            "tagged_qty": tagged,
            "entry_price": entry,
            "expected_sell_px": plan.expected_sell_price_per_unit,
            "expected_profit": plan.expected_profit,
            "plan_travel_turns": _travel_turns(plan.distance),
            "departure_book": book,
            "arrival_book": None,
            "arrival_turn": None,
            "arrived": False,
            "closed": False,
            "docked_rows": [],
            "docked_turns": 0,
            "dest_units": 0,
            "dest_revenue": 0,
            "other_units": 0,
            "other_revenue": 0,
            "sold_by_turn": {},
            "other_sales": [],
            "hold_target": None,
        }
        open_deps[ship.name].append(dep_id)


def _note_arrival(ship: Ship) -> None:
    here = ship.planet.name if ship.planet is not None else "?"
    brain = ship.brain
    with _LOCK:
        pending = [
            departures[i]
            for i in open_deps[ship.name]
            if not departures[i]["arrived"] and departures[i]["destination"] == here
        ]
    for dep in pending:
        commodity = _commodities.get(dep["commodity"])
        if commodity is None:
            continue
        book = _book(brain, ship.planet.market, commodity, dep["entry_price"])
        with _LOCK:
            dep["arrived"] = True
            dep["arrival_turn"] = ship.simulation.current_turn
            dep["arrival_book"] = book


def _record_docked_turn(ship: Ship, departing: bool = False) -> None:
    """Book, own asks and floor for a ship sitting on a delivered lot."""
    planet = ship.planet
    if planet is None:
        return
    with _LOCK:
        dep = _open_arrival(ship, planet.name)
    if dep is None:
        return
    commodity = _commodities.get(dep["commodity"])
    if commodity is None:
        return
    brain = ship.brain
    market = planet.market
    levels = market.get_bid_levels(commodity)
    own = [
        order
        for order in market.get_actor_orders(ship)["sell"]
        if order.commodity_type is commodity
    ]
    own_prices = [order.price for order in own]
    other_asks = [
        (price, qty)
        for price, qty in market.get_ask_levels(commodity)
        if not (own_prices and price in own_prices)
    ]
    row = {
        "turn": ship.simulation.current_turn,
        "departing": departing,
        "best_bid": levels[0][0] if levels else 0,
        "bid_depth": sum(q for _, q in levels),
        "best_ask_other": other_asks[0][0] if other_asks else 0,
        "ask_depth_other": sum(q for _, q in other_asks),
        "own_ask_min": min(own_prices) if own_prices else None,
        "own_ask_qty": sum(order.quantity for order in own),
        "floor": brain._sell_floor_price(commodity),
        "unsold_turns": brain._unsold_turns.get(commodity.id, 0),
        "held": ship.cargo.get_quantity(commodity),
        "list_called": bool(
            getattr(brain, "_probe_list", (None, set()))[0]
            == ship.simulation.current_turn
        ),
        "listed_this": commodity.id in getattr(brain, "_probe_list", (None, set()))[1],
        "sell_here": (
            getattr(brain, "_probe_disposition", (None, None))[1]
            if getattr(brain, "_probe_disposition", (None, None))[0]
            == ship.simulation.current_turn
            else None
        ),
        "plan_alive": isinstance(getattr(brain, "_current_plan", None), TradePlan),
        "selling_locally": bool(getattr(brain, "_selling_locally", False)),
        "money": ship.money,
    }
    with _LOCK:
        dep["docked_turns"] += 1
        if len(dep["docked_rows"]) < MAX_DOCKED_ROWS:
            dep["docked_rows"].append(row)
        elif departing:
            dep["docked_rows"][-1] = row


def _close_arrival(ship: Ship) -> None:
    """The ship is leaving; close any delivered lot it was sitting on.

    The row is taken here because ``Ship.start_journey`` cancels every
    resting order (ship.py:4448-4457): an ask placed on the turn the ship
    departs never reaches the end-of-turn match.
    """
    planet = ship.planet
    if planet is None:
        return
    _record_docked_turn(ship, departing=True)
    with _LOCK:
        dep = _open_arrival(ship, planet.name)
        if dep is not None:
            dep["closed"] = True
            commodity_id = dep["commodity"]
            dep["left_with"] = sum(
                lot["qty"]
                for lot in hold[ship.name][commodity_id]
                if lot["dep"] == dep["id"]
            )


_commodities: Dict[str, Any] = {}


def install_patches() -> None:
    original_execute = Market._execute_transaction
    original_update = Ship.update_journey
    original_start = Ship.start_journey
    original_take = Ship.take_turn
    original_list = TraderBrain._list_cargo_locally
    original_disposition = TraderBrain._cargo_disposition

    def wrapped_execute(
        self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
    ):  # type: ignore[no-untyped-def]
        result = original_execute(
            self, buyer, seller, commodity_type, quantity, price, buy_order, sell_order
        )
        if isinstance(buyer, Ship):
            patch_calls["ship_buy"] += 1
            _record_buy(buyer, commodity_type.id, quantity, price)
        if isinstance(seller, Ship):
            patch_calls["ship_sale"] += 1
            _record_sale(
                seller,
                commodity_type,
                quantity,
                price,
                market_planets.get(id(self), "?"),
            )
        return result

    def wrapped_update(self):  # type: ignore[no-untyped-def]
        arrived = original_update(self)
        if arrived and self.planet is not None:
            patch_calls["arrival"] += 1
            _note_arrival(self)
        return arrived

    def wrapped_start(self, destination, resuming: bool = False):  # type: ignore[no-untyped-def]
        brain = self.brain
        plan = getattr(brain, "_current_plan", None)
        is_plan_departure = (
            isinstance(plan, TradePlan)
            and bool(getattr(brain, "_plan_loaded", False))
            and self.planet is plan.origin
            and destination is plan.destination
            and not resuming
        )
        snapshot_plan = plan if is_plan_departure else None
        _close_arrival(self)
        started = original_start(self, destination, resuming=resuming)
        if started and snapshot_plan is not None:
            patch_calls["plan_departure"] += 1
            _record_departure(self, snapshot_plan)
        return started

    def wrapped_take(self):  # type: ignore[no-untyped-def]
        original_take(self)
        if self.status is ShipStatus.DOCKED and self.planet is not None:
            patch_calls["docked_turn"] += 1
            _record_docked_turn(self)

    def wrapped_list(self, market, skip=frozenset()):  # type: ignore[no-untyped-def]
        patch_calls["list_cargo"] += 1
        actions, listed, quantity = original_list(self, market, skip)
        self._probe_list = (
            self.ship.simulation.current_turn,
            {commodity.id for commodity in listed},
        )
        return actions, listed, quantity

    def wrapped_disposition(self, market):  # type: ignore[no-untyped-def]
        patch_calls["cargo_disposition"] += 1
        should_sell_here, committed = original_disposition(self, market)
        ship = self.ship
        self._probe_disposition = (ship.simulation.current_turn, should_sell_here)
        if not should_sell_here and ship.planet is not None:
            with _LOCK:
                dep = _open_arrival(ship, ship.planet.name)
                needed = dep is not None and dep["hold_target"] is None
            if needed and dep is not None:
                commodity = _commodities.get(dep["commodity"])
                quantity = self._sellable_quantity(commodity) if commodity else 0
                if commodity is not None and quantity > 0:
                    target = _winning_remote(ship, commodity, quantity)
                    if target is not None:
                        patch_calls["hold_target"] += 1
                        with _LOCK:
                            dep["hold_target"] = target
                            clearing_followups.append(
                                {
                                    "due": target["turn"] + 10,
                                    "planet": target["planet"],
                                    "commodity": dep["commodity"],
                                    "depth": target["remote"]["bid_depth"],
                                    "median_age": target["remote_bid_ages"][
                                        "median_age"
                                    ],
                                }
                            )
        return should_sell_here, committed

    TraderBrain._list_cargo_locally = wrapped_list  # type: ignore[assignment]
    TraderBrain._cargo_disposition = wrapped_disposition  # type: ignore[assignment]
    Market._execute_transaction = wrapped_execute  # type: ignore[assignment]
    Ship.update_journey = wrapped_update  # type: ignore[assignment]
    Ship.start_journey = wrapped_start  # type: ignore[assignment]
    Ship.take_turn = wrapped_take  # type: ignore[assignment]


def classify(dep: Dict[str, Any]) -> str:
    """Which gate the lot died at. Ordered from earliest to latest."""
    if dep["dest_units"] > 0:
        return "sold"
    rows = dep["docked_rows"]
    if not rows:
        return "no_docked_turn"
    listed = [row for row in rows if row["own_ask_min"] is not None]
    if not listed:
        if any(row["sell_here"] is False for row in rows):
            # ship.py:3503 `list_here = should_sell_here`: the delivered lot
            # is held for a market the ship rates higher, so no ask goes in.
            return "held_for_elsewhere"
        if any(row["list_called"] for row in rows):
            return "held_not_offered"
        return "never_listed"
    resting = [row for row in listed if not row["departing"]]
    if not resting:
        # Every ask this ship placed here was cancelled by its own departure
        # in the same turn, before the end-of-turn match.
        return "listed_then_left"
    if all(row["best_bid"] <= 0 for row in resting):
        return "no_bids"
    if all(row["best_bid"] < row["floor"] for row in resting if row["best_bid"] > 0):
        return "bids_below_floor"
    if any(
        0 < row["best_bid"] and row["own_ask_min"] <= row["best_bid"] for row in resting
    ):
        return "asked_at_bid_unfilled"
    return "asked_above_bid"


def _resolve_followups(sim) -> None:
    """Ten turns after a hold, how much of that book actually cleared."""
    turn = sim.current_turn
    with _LOCK:
        due = [f for f in clearing_followups if f["due"] <= turn]
        if due:
            clearing_followups[:] = [f for f in clearing_followups if f["due"] > turn]
    for entry in due:
        planet = planets_by_name.get(entry["planet"])
        commodity = _commodities.get(entry["commodity"])
        if planet is None or commodity is None:
            continue
        cleared = sum(planet.market.volume_history.get(commodity, [])[-10:])
        with _LOCK:
            clearing_rows.append(
                {
                    "commodity": entry["commodity"],
                    "depth": entry["depth"],
                    "cleared_10t": cleared,
                    "median_bid_age": entry["median_age"],
                }
            )


def run_probe(
    turns: int, planets: int, actors: int, ships: int, warmup: int, workers: int
) -> Dict[str, Any]:
    install_patches()
    sim = create_and_setup_simulation(
        planets=planets, actors=actors, makers=2, ships=ships
    )
    if workers > 1:
        sim.parallel_workers = workers
    market_planets.update({id(planet.market): planet.name for planet in sim.planets})
    planets_by_name.update({planet.name: planet for planet in sim.planets})
    _commodities.update({c.id: c for c in sim.commodity_registry.all_commodities()})

    for _ in range(turns):
        sim.run_turn()
        _resolve_followups(sim)
        if sim.current_turn % 25 == 0:
            with _LOCK:
                arrived = sum(1 for d in departures.values() if d["arrived"])
            print(
                f"turn {sim.current_turn}: {len(departures)} departures, "
                f"{arrived} arrivals",
                flush=True,
            )

    if patch_calls["plan_departure"] == 0 or patch_calls["docked_turn"] == 0:
        raise RuntimeError(f"monkeypatches never fired: {dict(patch_calls)}")

    rows = []
    for dep in departures.values():
        if dep["turn"] <= warmup or not dep["arrived"]:
            continue
        dep["klass"] = classify(dep)
        dep["transit_turns"] = dep["arrival_turn"] - dep["turn"]
        rows.append(dep)
    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "ships": len(sim.ships),
            "warmup": warmup,
            "workers": workers,
            "demand_horizon": DEMAND_HORIZON_TURNS,
            "haircut": SELL_PRICE_HAIRCUT,
        },
        "arrivals": rows,
        "clearing_rows": clearing_rows,
        "unarrived": sum(
            1 for d in departures.values() if d["turn"] > warmup and not d["arrived"]
        ),
        "patch_calls": dict(patch_calls),
    }


def _median(values: List[Any]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 1) if clean else None


def _fmt(values: List[Any], width: int = 8) -> str:
    value = _median(values)
    return f"{'-' if value is None else value:>{width}}"


CLASSES = (
    "sold",
    "no_bids",
    "bids_below_floor",
    "asked_at_bid_unfilled",
    "asked_above_bid",
    "listed_then_left",
    "held_for_elsewhere",
    "held_not_offered",
    "never_listed",
    "no_docked_turn",
)


def class_table(rows: List[Dict[str, Any]], label: str) -> None:
    counts = Counter(row["klass"] for row in rows)
    total = max(1, len(rows))
    cells = " ".join(f"{100.0 * counts[name] / total:>5.0f}%" for name in CLASSES)
    print(f"{label:>26} {len(rows):>5} {cells}")


def print_tables(result: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = result["arrivals"]
    if not rows:
        print("no arrivals recorded")
        return
    print(
        f"\n{len(rows)} plan arrivals after warmup "
        f"({result['unarrived']} departures never arrived)"
    )
    print("\n1. class shares")
    header = " ".join(f"{name[:6]:>6}" for name in CLASSES)
    print(f"{'group':>26} {'n':>5} {header}")
    class_table(rows, "all")
    by_commodity: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_commodity[row["commodity"]].append(row)
    for commodity, group in sorted(by_commodity.items(), key=lambda kv: -len(kv[1]))[
        :6
    ]:
        class_table(group, commodity)
    food = [row for row in rows if row["commodity"] in FOOD_IDS]
    if food:
        class_table(food, "food (all food ids)")

    print("\n2. prices (medians, credits per unit)")
    print(
        f"{'group':>26} {'plan_sell':>10} {'dep_bid':>8} {'arr_bid':>8} "
        f"{'first_ask':>10} {'floor':>7} {'local_ask':>10} {'flow_px':>8}"
    )

    def price_row(group: List[Dict[str, Any]], label: str) -> None:
        firsts = []
        for row in group:
            posted = [r for r in row["docked_rows"] if r["own_ask_min"] is not None]
            if posted:
                firsts.append(posted[0])
        print(
            f"{label:>26} {_fmt([r['expected_sell_px'] for r in group], 10)} "
            f"{_fmt([r['departure_book']['best_bid'] for r in group])} "
            f"{_fmt([r['arrival_book']['best_bid'] for r in group])} "
            f"{_fmt([f['own_ask_min'] for f in firsts], 10)} "
            f"{_fmt([f['floor'] for f in firsts], 7)} "
            f"{_fmt([f['best_ask_other'] for f in firsts], 10)} "
            f"{_fmt([r['departure_book']['flow_px'] for r in group])}"
        )

    price_row(rows, "all")
    for commodity, group in sorted(by_commodity.items(), key=lambda kv: -len(kv[1]))[
        :6
    ]:
        price_row(group, commodity)
    if food:
        price_row(food, "food (all food ids)")

    print("\n3. demand estimate vs what was there")
    print(
        f"{'group':>26} {'sellable':>9} {'flow_qty':>9} {'depth>entry':>12} "
        f"{'spec%':>6} {'arr_depth':>10} {'dep_depth':>10} {'transit':>8} "
        f"{'docked':>7} {'ask_depth':>10}"
    )

    def demand_row(group: List[Dict[str, Any]], label: str) -> None:
        print(
            f"{label:>26} "
            f"{_fmt([r['departure_book']['sellable'] for r in group], 9)} "
            f"{_fmt([r['departure_book']['flow_qty'] for r in group], 9)} "
            f"{_fmt([r['departure_book']['depth_above_entry'] for r in group], 12)} "
            f"{100.0 * sum(1 for r in group if r['departure_book']['speculative']) / len(group):>5.0f}% "
            f"{_fmt([r['arrival_book']['bid_depth'] for r in group], 10)} "
            f"{_fmt([r['departure_book']['bid_depth'] for r in group], 10)} "
            f"{_fmt([r['transit_turns'] for r in group], 8)} "
            f"{_fmt([r['docked_turns'] for r in group], 7)} "
            f"{_fmt([r['arrival_book']['ask_depth'] for r in group], 10)}"
        )

    demand_row(rows, "all")
    for commodity, group in sorted(by_commodity.items(), key=lambda kv: -len(kv[1]))[
        :6
    ]:
        demand_row(group, commodity)
    if food:
        demand_row(food, "food (all food ids)")

    held = [
        row
        for row in rows
        if row["klass"] == "held_for_elsewhere" and row["hold_target"] is not None
    ]
    if held:
        print(f"\n4. what the winning remote forecast is made of ({len(held)} holds)")
        print(
            f"{'group':>26} {'n':>4} {'fcast':>7} {'bid_val':>8} {'flow_val':>9} "
            f"{'flow%':>6} {'rem_bid':>8} {'loc_bid':>8} {'rem_depth':>10} "
            f"{'lot':>5} {'bid_age':>8} {'sold_there':>11} {'px_there':>9} "
            f"{'fcast_px':>9}"
        )

        def hold_row(group: List[Dict[str, Any]], label: str) -> None:
            remote = [r["hold_target"]["remote"] for r in group]
            local = [r["hold_target"]["local"] for r in group]
            flow_share = [
                100.0 * t["flow_value"] / t["total"] if t["total"] else 0.0
                for t in remote
            ]
            sold_there = []
            price_there = []
            for row in group:
                target = row["hold_target"]["planet"]
                sales = [s for s in row["other_sales"] if s["planet"] == target]
                units = sum(s["qty"] for s in sales)
                sold_there.append(units)
                if units:
                    price_there.append(
                        sum(s["qty"] * s["price"] for s in sales) / units
                    )
            forecast_px = [
                t["total"] / max(1, r["hold_target"]["quantity"])
                for t, r in zip(remote, group)
            ]
            reached = 100.0 * sum(1 for u in sold_there if u > 0) / len(group)
            print(
                f"{label:>26} {len(group):>4} {_fmt([t['total'] for t in remote], 7)} "
                f"{_fmt([t['bid_value'] for t in remote], 8)} "
                f"{_fmt([t['flow_value'] for t in remote], 9)} "
                f"{_fmt(flow_share, 6)} {_fmt([t['best_bid'] for t in remote], 8)} "
                f"{_fmt([t['best_bid'] for t in local], 8)} "
                f"{_fmt([t['bid_depth'] for t in remote], 10)} "
                f"{_fmt([r['hold_target']['quantity'] for r in group], 5)} "
                f"{_fmt([r['hold_target']['remote_bid_ages']['median_age'] for r in group], 8)} "
                f"{reached:>10.0f}% {_fmt(price_there, 9)} {_fmt(forecast_px, 9)}"
            )

        hold_row(held, "all holds")
        by_hold: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in held:
            by_hold[row["commodity"]].append(row)
        for commodity, group in sorted(by_hold.items(), key=lambda kv: -len(kv[1]))[:5]:
            hold_row(group, commodity)
        food_held = [r for r in held if r["commodity"] in FOOD_IDS]
        if food_held:
            hold_row(food_held, "food (all food ids)")

    clearing: List[Dict[str, Any]] = result.get("clearing_rows", [])
    if clearing:
        print(f"\n5. do the walked bids ever fill? ({len(clearing)} hold books)")
        print(
            f"{'commodity':>26} {'n':>5} {'bid_depth':>10} {'cleared_10t':>12} "
            f"{'cleared/depth':>14} {'median_bid_age':>15}"
        )
        by_clear: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in clearing:
            by_clear[row["commodity"]].append(row)

        def clear_row(group: List[Dict[str, Any]], label: str) -> None:
            depth = sum(row["depth"] for row in group)
            cleared = sum(row["cleared_10t"] for row in group)
            print(
                f"{label:>26} {len(group):>5} {_fmt([r['depth'] for r in group], 10)} "
                f"{_fmt([r['cleared_10t'] for r in group], 12)} "
                f"{(cleared / depth if depth else 0):>14.2f} "
                f"{_fmt([r['median_bid_age'] for r in group], 15)}"
            )

        clear_row(clearing, "all")
        for commodity, group in sorted(by_clear.items(), key=lambda kv: -len(kv[1]))[
            :5
        ]:
            clear_row(group, commodity)

    unsold = [row for row in rows if row["klass"] != "sold"]
    if unsold:
        print(
            f"\nunsold arrivals: {len(unsold)}; median docked turns "
            f"{_median([r['docked_turns'] for r in unsold])}, "
            f"median units left with "
            f"{_fmt([r.get('left_with') for r in unsold], 1)}, "
            f"share whose arrival book had zero bid depth "
            f"{100.0 * sum(1 for r in unsold if r['arrival_book']['bid_depth'] == 0) / len(unsold):.0f}%, "
            f"share whose departure book had zero bid depth "
            f"{100.0 * sum(1 for r in unsold if r['departure_book']['bid_depth'] == 0) / len(unsold):.0f}%"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--planets", type=int, default=50)
    parser.add_argument("--actors", type=int, default=100)
    parser.add_argument("--ships", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--out", type=str, default="tmp/arrival_book.json")
    args = parser.parse_args(argv)

    result = run_probe(
        turns=args.turns,
        planets=args.planets,
        actors=args.actors,
        ships=args.ships,
        warmup=args.warmup,
        workers=args.workers,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    print(f"\nwrote {out} ({len(result['arrivals'])} arrivals)")
    print_tables(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
