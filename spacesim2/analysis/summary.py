"""Compact, population-wide behavioral summary of a simulation.

The Tier-0 readout for the agent dev loop: a small machine-readable dict of
macro KPIs computed from the live ``Simulation`` object, not the sampled
Parquet logs, plus a coarse PASS/WARN/FAIL verdict against expected ranges.
An agent runs one command and reads about 20 numbers and a verdict. For
open-ended questions, write a Tier-1 analysis script instead; see the
``sim-evaluation`` skill.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Sequence

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.contracts import GOVERNMENT, PassengerPayload
from spacesim2.core.drives.prosperity_drive import (
    PROSPERITY_CATEGORIES,
    ProsperityDriveMetrics,
    needs_are_met,
    prosperity_index,
)
from spacesim2.core.drives.shelter_drive import PREFAB_HOUSING_NAME
from spacesim2.core.navigation import get_navigator
from spacesim2.core.ship import FUEL_MILLI, Ship, ShipStatus
from spacesim2.core.simulation import Simulation

# Verdict thresholds are catastrophe floors, not targets: a healthy run sits
# well above them. They flag runs that are broken regardless of design
# intent, which avoids alarm fatigue. Values are (min_turns, warn, fail):
# the run length from which the floor applies, then two mean-health floors.
#
# Calibrated against steady-state runs (2026-09-03):
#   12 planets / 500 turns: food 0.98, clothing 0.97, shelter 0.95, health 0.63
#   100 planets / 600 turns: food 0.955, clothing 0.93, shelter 0.94, health 0.535
# Each tier's drives ramp on their own timetable, so a threshold applies only
# once its supply chain has had time to stand up. Judging a drive before then
# would fail healthy short runs; that turn gate is why the entry exists.
#
# food, from turn 0: the survival need, running from the first turn. The floors
#   are set against the weaker 200-turn dev loop (0.78-0.82), not the 0.96-0.98
#   plateau: a 0.80 warn fired on about half of healthy dev-loop runs.
# clothing / shelter, from turn 200: comfort drives whose chains are up by the
#   dev-loop mark (clothing 0.87-0.97, shelter 0.94 at turn 200) but not at the
#   120-turn scale the smoke test runs, where shelter still sits near 0.44.
#   Their floors leave a wide margin because both swing run to run: shelter has
#   been seen at 0.82 on a 500-turn run that was otherwise healthy.
# health, from turn 400: depends on medicine and so on the chemistry tier,
#   which only bootstraps after roughly 300-400 turns. The floor is wide
#   because health is the most variable KPI in the model — an 800-turn run has
#   landed at 0.148 with medicine stockpiling — so the fail floor sits below
#   that known outlier and catches only a tier that never lit up at all.
_DRIVE_HEALTH_THRESHOLDS: Dict[str, tuple[int, float, float]] = {
    "food": (0, 0.70, 0.50),
    "clothing": (200, 0.70, 0.40),
    "shelter": (200, 0.70, 0.40),
    "health": (400, 0.30, 0.10),
}

# From this turn on the upper tiers are expected to be running, so their
# markets are held to the liveness check.
_LATE_RUN_TURNS = 400

# Survival commodity whose market must stay alive for the economy to function.
_LIVENESS_COMMODITY = "food"

# Materials the drives consume. Each must still be trading somewhere in the
# galaxy on a long run; a frozen market for one of them is a regression even
# while the drive's health coasts on existing stock.
_DRIVE_MATERIALS = ("food", "clothing", "simple_building_materials", "medicine")

# Commodities whose ship-carried share is worth watching: the drive materials
# plus the fuel that moves them.
_TRADE_WATCH_COMMODITIES = _DRIVE_MATERIALS + ("nova_fuel",)

# Turns of history behind the `markets` and `trade` sections. Market volume
# series keep 120 entries and transaction history is capped per market, so
# this window must stay well inside both.
_SOLVENT_MONEY = 1000
_ACTIVITY_WINDOW_TURNS = 50

# A drive instance counts as deprived when its debt exceeds this.
_DEPRIVED_DEBT = 0.80


def compute_summary(sim: Simulation) -> Dict[str, object]:
    """Compute a compact KPI summary from a finished simulation.

    Returns a JSON-serializable dict of macro KPIs plus a ``verdict`` block.
    Numeric values are rounded for compact, stable output.
    """
    regular_actors = [a for a in sim.actors if a.actor_type == ActorType.REGULAR]
    service_actors = [a for a in sim.actors if a.actor_type == ActorType.SERVICE]

    drives = _summarize_drives(regular_actors)
    prices = _summarize_prices(sim)
    markets = _summarize_markets(sim)
    prosperity = _summarize_prosperity(regular_actors, markets)
    trade = _summarize_trade(sim)
    trade.update(_summarize_fleet_fuel(sim))
    actors_aboard = sim.actors_aboard()
    summary: Dict[str, object] = {
        "turns": sim.current_turn,
        "planets": len(sim.planets),
        "regular_actors": len(regular_actors),
        "actors_aboard": actors_aboard,
        "population": len(regular_actors) + actors_aboard,
        "service_actors": _summarize_service_actors(service_actors),
        "ships": len(sim.ships),
        "money": _summarize_money(regular_actors),
        "drives": drives,
        "prosperity": prosperity,
        "durables": _summarize_durables(sim, regular_actors, markets),
        "inventory_totals": _summarize_inventory(sim),
        "prices": prices,
        "markets": markets,
        "trade": trade,
        "migration": _summarize_migration(sim, regular_actors),
        "contracts": _summarize_contracts(sim),
    }
    summary["verdict"] = _build_verdict(drives, prices, markets, sim.current_turn)
    return summary


def _summarize_migration(sim: Simulation, regular_actors: List) -> Dict[str, float]:
    """Actor movement between planets. Not part of the verdict.

    ``departures_per_100_turns_per_1000_actors`` normalizes the rate so runs
    of different length and population compare directly. Passengers aboard a
    ship are excluded from ``regular_actors``, so ``sim.actors_aboard()`` is
    added back for the denominator; this is the same count reported at top
    level as ``actors_aboard``.

    ``waiting`` and ``expired`` are the two ways passage fails: an actor
    whose contract nobody accepts, and one whose contract ran out. Both
    large on poor planets means the fleet is not reaching them, which is
    what migration exists to fix.
    """
    aboard = sim.actors_aboard()
    waiting = sum(
        1
        for planet in sim.planets
        for contract in planet.contracts.open_contracts()
        if isinstance(contract.payload, PassengerPayload)
    )
    population = len(regular_actors) + aboard
    turns = sim.current_turn
    if population > 0 and turns > 0:
        rate = sim.migration_departures * 100.0 * 1000.0 / (turns * population)
    else:
        rate = 0.0
    waits = sim.passage_wait_turns
    return {
        "departures": sim.migration_departures,
        "arrivals": sim.migration_arrivals,
        "aboard": aboard,
        "waiting": waiting,
        "expired": sim.passage_expired,
        "median_wait_turns": round(statistics.median(waits), 1) if waits else 0.0,
        "departures_per_100_turns_per_1000_actors": round(rate, 2),
    }


def _summarize_contracts(sim: Simulation) -> Dict[str, float]:
    """Transport jobs posted and how they ended. Not part of the verdict.

    ``government_payouts`` is money the government created paying carriers,
    the number the freight design says to check rather than assume; the per
    100 turns rate makes runs of different length comparable.

    ``pickup_stops_window`` counts the journeys broken in the last
    `_ACTIVITY_WINDOW_TURNS` turns so the ship could take contracts aboard at
    a planet it was flying past, the same window `fleet.refuel_stops_window`
    uses.
    """
    government_open = sum(
        1
        for planet in sim.planets
        for contract in planet.contracts.open_contracts()
        if contract.poster is GOVERNMENT
    )
    open_total = sum(len(planet.contracts.open_contracts()) for planet in sim.planets)
    turns = sim.current_turn
    payout_rate = sim.government_payouts * 100.0 / turns if turns > 0 else 0.0
    cutoff = sim.current_turn - _ACTIVITY_WINDOW_TURNS
    pickup_stops_window = sum(
        sum(1 for turn in ship.pickup_stop_turns if turn >= cutoff)
        for ship in sim.ships
    )
    return {
        "posted": sim.contracts_posted,
        "delivered": sim.contracts_delivered,
        "stranded": sim.contracts_stranded,
        "expired": sim.contracts_expired,
        "government_open": government_open,
        "government_payouts": sim.government_payouts,
        "government_payouts_per_100_turns": round(payout_rate, 1),
        "open_total": open_total,
        "pickup_stops_window": pickup_stops_window,
    }


def _summarize_service_actors(service_actors: List) -> Dict[str, int]:
    """Count service actors by brain class name (e.g. {"MarketMakerBrain": 100})."""
    counts: Dict[str, int] = {}
    for actor in service_actors:
        brain_name = type(actor.brain).__name__
        counts[brain_name] = counts.get(brain_name, 0) + 1
    return counts


def _summarize_money(regular_actors: List) -> Dict[str, float]:
    """Summarize the money distribution across regular actors."""
    if not regular_actors:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0}
    money = [float(a.money) for a in regular_actors]
    return {
        "mean": round(statistics.fmean(money), 1),
        "median": round(statistics.median(money), 1),
        "min": round(min(money), 1),
        "max": round(max(money), 1),
    }


def _summarize_drives(regular_actors: List) -> Dict[str, Dict[str, float]]:
    """Aggregate per-need metrics across the population.

    Returns drive name to {mean_health, mean_debt, pct_deprived}, where
    pct_deprived is the fraction of actors whose debt for that drive exceeds
    `_DEPRIVED_DEBT`. Prosperity drives are reported separately by
    ``_summarize_prosperity`` and never enter the verdict.
    """
    health: Dict[str, List[float]] = {}
    debt: Dict[str, List[float]] = {}
    deprived: Dict[str, int] = {}

    for actor in regular_actors:
        for drive in actor.drives:
            if not drive.WELLBEING:
                continue
            name = drive.metrics.get_name()
            health.setdefault(name, []).append(drive.metrics.health)
            debt.setdefault(name, []).append(drive.metrics.debt)
            if drive.metrics.debt > _DEPRIVED_DEBT:
                deprived[name] = deprived.get(name, 0) + 1

    result: Dict[str, Dict[str, float]] = {}
    n = len(regular_actors)
    for name in sorted(health):
        result[name] = {
            "mean_health": round(statistics.fmean(health[name]), 3),
            "mean_debt": round(statistics.fmean(debt[name]), 3),
            "pct_deprived": round(deprived.get(name, 0) / n, 3) if n else 0.0,
        }
    return result


def _summarize_prosperity(
    regular_actors: List, markets: Dict[str, object]
) -> Dict[str, object]:
    """Consumption above subsistence: index, coverage, gate, and trade volume.

    ``index_mean`` is the population mean of ``prosperity_index``.
    ``coverage`` is mean coverage per category. ``gate_pass_share`` is the
    fraction of actors whose needs are met well enough to bid for
    prosperity goods this turn. ``volume_per_planet_turn`` repeats the
    markets block for the prosperity goods only, 0.0 when never traded.
    """
    n = len(regular_actors)
    coverage: Dict[str, List[float]] = {}
    index_total = 0.0
    gate_passes = 0
    for actor in regular_actors:
        index_total += prosperity_index(actor)
        if needs_are_met(actor):
            gate_passes += 1
        for drive in actor.drives:
            # isinstance, not WELLBEING, because this needs the
            # ProsperityDriveMetrics-only `coverage` field.
            if isinstance(drive.metrics, ProsperityDriveMetrics):
                coverage.setdefault(drive.metrics.name, []).append(
                    drive.metrics.coverage
                )

    volume = markets["volume_per_planet_turn"]
    if not isinstance(volume, dict):
        raise ValueError(
            f"markets['volume_per_planet_turn'] must be a dict, got {type(volume).__name__}"
        )
    return {
        "index_mean": round(index_total / n, 3) if n else 0.0,
        "gate_pass_share": round(gate_passes / n, 3) if n else 0.0,
        "coverage": {
            name: round(statistics.fmean(values), 3)
            for name, values in sorted(coverage.items())
        },
        "volume_per_planet_turn": {
            c.commodity_id: volume.get(c.commodity_id, 0.0)
            for c in PROSPERITY_CATEGORIES
        },
    }


# Goods that are held and used over many turns rather than consumed on the
# turn they are bought. They left the prosperity index when they became
# durables; this block is how they are watched instead.
DURABLE_GOODS = ("computers", PREFAB_HOUSING_NAME)


def _summarize_durables(
    sim: Simulation, regular_actors: List, markets: Dict[str, object]
) -> Dict[str, object]:
    """Ownership and trade of the durable goods.

    ``computer_holder_share`` is the share of industrialists whose chosen
    recipe lists a capital good that hold one; industrialists whose recipe
    lists none, or who have no recipe, are out of the denominator.
    ``prefab_holder_share`` is the share of regular actors with a dwelling.
    ``volume_per_planet_turn`` repeats the markets block for the durables.
    Not part of the verdict.
    """
    capital_users = 0
    capital_holders = 0
    prefab_holders = 0
    prefab = sim.commodity_registry.get_commodity(PREFAB_HOUSING_NAME)

    for actor in regular_actors:
        if prefab is not None and actor.inventory.get_quantity(prefab) > 0:
            prefab_holders += 1
        brain = actor.brain
        if not isinstance(brain, IndustrialistBrain) or brain.chosen_recipe_id is None:
            continue
        process = sim.process_registry.get_process(brain.chosen_recipe_id)
        if process is None or not process.capital:
            continue
        capital_users += 1
        if all(
            actor.inventory.get_quantity(commodity) > 0 for commodity in process.capital
        ):
            capital_holders += 1

    volume = markets["volume_per_planet_turn"]
    if not isinstance(volume, dict):
        raise ValueError(
            f"markets['volume_per_planet_turn'] must be a dict, got {type(volume).__name__}"
        )
    n = len(regular_actors)
    return {
        "capital_recipe_actors": capital_users,
        "computer_holder_share": (
            round(capital_holders / capital_users, 3) if capital_users else 0.0
        ),
        "prefab_holder_share": round(prefab_holders / n, 3) if n else 0.0,
        "volume_per_planet_turn": {
            good: volume.get(good, 0.0) for good in DURABLE_GOODS
        },
    }


def _summarize_inventory(sim: Simulation) -> Dict[str, int]:
    """Population-wide stock: units of each commodity held across all actors."""
    totals: Dict[str, int] = {}
    for actor in sim.actors:
        for commodity, quantity in actor.inventory.commodities.items():
            totals[commodity.id] = totals.get(commodity.id, 0) + quantity
    return {cid: totals[cid] for cid in sorted(totals)}


def _summarize_prices(sim: Simulation) -> Dict[str, float]:
    """Mean 30-day average price per commodity, averaged across planet markets.

    Commodities with no trading history on any market are omitted rather
    than reported as zero.
    """
    per_commodity: Dict[str, List[float]] = {}
    for planet in sim.planets:
        market = planet.market
        for commodity in sim.commodity_registry.all_commodities():
            if market.has_history(commodity):
                price = market.get_30_day_average_price(commodity)
                per_commodity.setdefault(commodity.id, []).append(price)

    return {
        cid: round(statistics.fmean(prices), 1)
        for cid, prices in sorted(per_commodity.items())
    }


def _recent_volume(volumes: Sequence[int], window: int) -> float:
    """Mean per-turn volume over the last ``window`` recorded turns.

    A market appends one entry per turn once a commodity has traded there, so
    the tail of the series is the recent window. An empty series means the
    commodity never traded on that market.
    """
    if not volumes:
        return 0.0
    recent = volumes[-window:]
    return sum(recent) / len(recent)


def _summarize_markets(sim: Simulation) -> Dict[str, object]:
    """Market liveness: recent per-commodity trade volume and traded counts.

    ``volume_per_planet_turn`` is the mean units traded per planet per turn
    over the last `_ACTIVITY_WINDOW_TURNS`, so it is comparable across galaxy
    sizes. A commodity that has ever traded stays listed with volume 0.0 once
    its market freezes, which is exactly the regression this section exists to
    surface.
    """
    planet_count = len(sim.planets)
    per_commodity: Dict[str, float] = {}
    ever: set[str] = set()

    for planet in sim.planets:
        market = planet.market
        for commodity in sim.commodity_registry.all_commodities():
            if not market.has_history(commodity):
                continue
            ever.add(commodity.id)
            volumes = market.volume_history.get(commodity, [])
            per_commodity[commodity.id] = per_commodity.get(
                commodity.id, 0.0
            ) + _recent_volume(volumes, _ACTIVITY_WINDOW_TURNS)

    volume = {
        cid: round(total / planet_count, 2) if planet_count else 0.0
        for cid, total in sorted(per_commodity.items())
    }
    return {
        "window_turns": _ACTIVITY_WINDOW_TURNS,
        "traded_recent": sum(1 for v in volume.values() if v > 0.0),
        "traded_ever": len(ever),
        "volume_per_planet_turn": volume,
    }


def _summarize_trade(sim: Simulation) -> Dict[str, object]:
    """Interplanetary trade: units ships delivered and their share of volume.

    Counts fills in the last `_ACTIVITY_WINDOW_TURNS` where the seller is a
    ship. Units a ship both bought and sold on the same planet inside the
    window are netted out, so what remains is cargo that arrived from
    somewhere else. Ship-carried and total units come from the same
    transaction slice, so ``ship_share_of_volume`` stays internally consistent.
    A market's transaction history is capped, so on a very busy market that
    slice covers fewer than `_ACTIVITY_WINDOW_TURNS` turns and a thinly traded
    commodity can fall out of it; treat the shares as indicative and the
    `markets` volumes as the authority on what is trading.
    """
    cutoff = sim.current_turn - _ACTIVITY_WINDOW_TURNS
    total_units: Dict[str, int] = {}
    sold: Dict[tuple[int, str, str], int] = {}
    bought: Dict[tuple[int, str, str], int] = {}

    for index, planet in enumerate(sim.planets):
        for tx in planet.market.transaction_history:
            if tx.turn < cutoff:
                continue
            cid = tx.commodity_type.id
            total_units[cid] = total_units.get(cid, 0) + tx.quantity
            if isinstance(tx.seller, Ship):
                key = (index, tx.seller.name, cid)
                sold[key] = sold.get(key, 0) + tx.quantity
            if isinstance(tx.buyer, Ship):
                key = (index, tx.buyer.name, cid)
                bought[key] = bought.get(key, 0) + tx.quantity

    # Net out same-planet round trips per ship and commodity: a ship that
    # bought and sold the same good on one planet moved nothing.
    delivered: Dict[str, int] = {}
    for key, units in sold.items():
        cid = key[2]
        net = units - min(units, bought.get(key, 0))
        delivered[cid] = delivered.get(cid, 0) + net

    delivered = {cid: units for cid, units in sorted(delivered.items()) if units > 0}
    share = {
        cid: round(delivered.get(cid, 0) / total_units[cid], 3)
        for cid in _TRADE_WATCH_COMMODITIES
        if total_units.get(cid, 0) > 0
    }
    return {
        "window_turns": _ACTIVITY_WINDOW_TURNS,
        "ship_delivered_units": delivered,
        "ship_delivered_total": sum(delivered.values()),
        "ship_share_of_volume": share,
    }


def _summarize_fleet_fuel(sim: Simulation) -> Dict[str, object]:
    """Fleet fuel-access KPIs: where fuel can be bought, and who is stuck.

    ``fuel_ask_planets`` counts planets with a live resting nova_fuel ask
    right now, using the same test ship brains use to plan refueling.
    ``stranded_ships`` counts docked ships below their round-trip fuel
    reserve whose current planet has no such ask (see
    ``TraderBrain.is_stranded``). ``service_fuel_stock`` and
    ``industrialist_fuel_stock`` are nova_fuel held by SERVICE actors and by
    IndustrialistBrain actors respectively, a coarse look at whether fuel is
    piling up off the market instead of reaching ships.

    ``stranded_ships`` is definition-dependent: once spaceport operators keep
    a fuel ask on most planets, a docked ship with no local ask is rare even
    if it never actually departs. ``idle_ships`` is a definition-independent
    activity floor: docked ships that have not departed in the last
    `_ACTIVITY_WINDOW_TURNS` turns (or never have). ``departures_window``
    counts journeys started fleet-wide in that window,
    ``refuel_stops_window`` counts the journeys broken for an en-route refuel
    stop in it, and
    ``fuel_sold_by_service_window`` / ``fuel_sold_by_service_price`` track
    nova_fuel actually sold by SERVICE actors (spaceport operators) in the
    same window, volume-weighted by price. Like `ship_delivered_units`, the
    fuel-sold figures read from each market's capped transaction history, so
    on a very busy market they can cover fewer turns than the window.
    """
    navigator = get_navigator(sim)
    navigator.refresh_market_facts(turn=sim.current_turn)

    fuel_ask_planets = sum(
        1 for planet in sim.planets if navigator.fuel_purchasable_at(planet)
    )

    stranded_ships = sum(1 for ship in sim.ships if ship.brain.is_stranded())
    ship_count = len(sim.ships)
    stranded_ship_share = round(stranded_ships / ship_count, 3) if ship_count else 0.0

    cutoff = sim.current_turn - _ACTIVITY_WINDOW_TURNS
    idle_ships = sum(
        1
        for ship in sim.ships
        if ship.status == ShipStatus.DOCKED and ship.last_departure_turn < cutoff
    )
    idle_ship_share = round(idle_ships / ship_count, 3) if ship_count else 0.0
    departures_window = sum(
        sum(1 for turn in ship.departure_turns if turn >= cutoff) for ship in sim.ships
    )
    refuel_stops_window = sum(
        sum(1 for turn in ship.refuel_stop_turns if turn >= cutoff)
        for ship in sim.ships
    )

    fuel_commodity = sim.commodity_registry.get_commodity("nova_fuel")
    service_fuel_stock = 0
    industrialist_fuel_stock = 0
    if fuel_commodity is not None:
        for actor in sim.actors:
            quantity = actor.inventory.get_quantity(fuel_commodity)
            if actor.actor_type == ActorType.SERVICE:
                service_fuel_stock += quantity
            if type(actor.brain).__name__ == "IndustrialistBrain":
                industrialist_fuel_stock += quantity

    fuel_sold_units = 0
    fuel_sold_value = 0
    if fuel_commodity is not None:
        for planet in sim.planets:
            for tx in planet.market.transaction_history:
                if tx.turn < cutoff or tx.commodity_type is not fuel_commodity:
                    continue
                if (
                    isinstance(tx.seller, Actor)
                    and tx.seller.actor_type == ActorType.SERVICE
                ):
                    fuel_sold_units += tx.quantity
                    fuel_sold_value += tx.quantity * tx.price
    fuel_sold_by_service_price = (
        round(fuel_sold_value / fuel_sold_units, 1) if fuel_sold_units else 0.0
    )

    # Fleet fuel purchases over the same window, and what they cost. The
    # service price above is what operators charge; the gap between the two
    # is the fleet's spiked-fill premium.
    ship_fuel_units = 0
    ship_fuel_value = 0
    if fuel_commodity is not None:
        for planet in sim.planets:
            for tx in planet.market.transaction_history:
                if tx.turn < cutoff or tx.commodity_type is not fuel_commodity:
                    continue
                if isinstance(tx.buyer, Ship):
                    ship_fuel_units += tx.quantity
                    ship_fuel_value += tx.quantity * tx.price
    ship_fuel_price = (
        round(ship_fuel_value / ship_fuel_units, 1) if ship_fuel_units else 0.0
    )

    # Fleet solvency. A ship under _SOLVENT_MONEY cannot fund a short trip's
    # fuel and cargo, so the share above it is the share still trading.
    ship_money = sorted(ship.money for ship in sim.ships)
    # Fuel in tanks is capital too: a fleet that bunkers heavily shows low
    # cash and a large tank stock, which is a different condition from a
    # fleet that has lost its money.
    ship_fuel_in_tanks = round(sum(ship.fuel for ship in sim.ships) / FUEL_MILLI, 3)
    ship_money_median = float(statistics.median(ship_money)) if ship_money else 0.0
    ships_solvent_share = (
        round(sum(1 for m in ship_money if m >= _SOLVENT_MONEY) / ship_count, 3)
        if ship_count
        else 0.0
    )

    return {
        "fuel_ask_planets": fuel_ask_planets,
        "stranded_ships": stranded_ships,
        "stranded_ship_share": stranded_ship_share,
        "idle_ships": idle_ships,
        "idle_ship_share": idle_ship_share,
        "departures_window": departures_window,
        "refuel_stops_window": refuel_stops_window,
        "service_fuel_stock": service_fuel_stock,
        "industrialist_fuel_stock": industrialist_fuel_stock,
        "fuel_sold_by_service_window": fuel_sold_units,
        "fuel_sold_by_service_price": fuel_sold_by_service_price,
        "ship_fuel_bought_window": ship_fuel_units,
        "ship_fuel_price": ship_fuel_price,
        "ship_money_median": ship_money_median,
        "ship_money_total": int(sum(ship_money)),
        "ship_fuel_in_tanks": ship_fuel_in_tanks,
        "ships_solvent_share": ships_solvent_share,
    }


def _build_verdict(
    drives: Dict[str, Dict[str, float]],
    prices: Dict[str, float],
    markets: Dict[str, object],
    turns: int,
) -> Dict[str, object]:
    """Derive a coarse PASS/WARN/FAIL catastrophe verdict.

    Checks survival-drive health floors and that the survival commodity's
    market is still trading. The overall status is the worst signal. Flags
    are human-readable so an agent can act without re-deriving them.
    """
    flags: List[str] = []
    status = "PASS"

    def worsen(new_status: str) -> None:
        nonlocal status
        if new_status == "FAIL" or status == "PASS":
            status = new_status

    for name, (min_turns, warn, fail) in _DRIVE_HEALTH_THRESHOLDS.items():
        if name not in drives or turns < min_turns:
            continue
        mean_health = drives[name]["mean_health"]
        if mean_health < fail:
            worsen("FAIL")
            flags.append(f"{name} health {mean_health:.2f} < {fail:.2f}")
        elif mean_health < warn:
            worsen("WARN")
            flags.append(f"{name} health {mean_health:.2f} < {warn:.2f}")

    # A frozen survival market is a catastrophic regression.
    if _LIVENESS_COMMODITY not in prices:
        worsen("FAIL")
        flags.append(f"no {_LIVENESS_COMMODITY} market activity (dead market)")

    # Drive materials must still be moving somewhere. Only checked on long
    # runs: the upper tiers legitimately have not started by the 200-turn mark.
    if turns >= _LATE_RUN_TURNS:
        volume = markets["volume_per_planet_turn"]
        assert isinstance(volume, dict)
        for cid in _DRIVE_MATERIALS:
            if volume.get(cid, 0.0) <= 0.0:
                worsen("WARN")
                flags.append(f"no recent {cid} trade galaxy-wide (frozen market)")

    return {"status": status, "flags": flags}
