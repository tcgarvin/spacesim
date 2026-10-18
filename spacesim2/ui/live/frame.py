"""The immutable per-turn frame the renderer reads from.

The simulation mutates freely inside ``run_turn``, so the render thread must
never read live ``Planet``, ``Actor``, ``Ship``, or ``Market`` objects. The
simulation worker builds one :class:`TurnFrame` at each turn boundary and
publishes it by reference swap. Readers hold a frozen object and need no lock.

A frame carries only what the screen shows: one small snapshot per planet and
ship, the galaxy vitals for the HUD, and drill-down details for the entities
the UI has subscribed to, meaning the current selection. It is never a copy of
the whole sim. The per-planet wellbeing sweep over every actor's drives is the
one expensive part of building a frame, so it is computed once and shared with
the history recorder.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, FrozenSet, Mapping, Tuple

from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.view_model import (
    GalaxyViewModel,
    PlanetDetail,
    PlanetSnapshot,
    ShipDetail,
    ShipSnapshot,
)

# (kind, name) where kind is "planet" or "ship"; the same shape the scene uses
# for its selection.
Subscription = Tuple[str, str]


@dataclass(frozen=True)
class GalaxyVitals:
    """The HUD's at-a-glance numbers, derived from the planet/ship snapshots."""

    population: int
    wellbeing: float  # population-weighted mean, in [0, 1]
    traveling: int  # ships in flight


@dataclass(frozen=True)
class TurnFrame:
    turn: int
    planets: Tuple[PlanetSnapshot, ...]
    ships: Tuple[ShipSnapshot, ...]
    vitals: GalaxyVitals
    planet_details: Mapping[str, PlanetDetail]
    ship_details: Mapping[str, ShipDetail]

    def has_planet(self, name: str) -> bool:
        return any(p.name == name for p in self.planets)

    def has_ship(self, name: str) -> bool:
        return any(s.name == name for s in self.ships)


def galaxy_vitals(
    planets: Tuple[PlanetSnapshot, ...],
    ships: Tuple[ShipSnapshot, ...],
    aboard: int = 0,
) -> GalaxyVitals:
    """The HUD total. ``aboard`` (passengers in flight) is added so the
    galaxy-wide population does not dip while a migrant is between planets;
    per-planet snapshots are unaffected and stay resident-only.
    """
    resident_population = sum(p.population for p in planets)
    population = resident_population + aboard
    if resident_population > 0:
        wellbeing = (
            sum(p.wellbeing * p.population for p in planets) / resident_population
        )
    else:
        wellbeing = 0.0
    traveling = sum(1 for s in ships if s.traveling)
    return GalaxyVitals(population=population, wellbeing=wellbeing, traveling=traveling)


def build_details(
    view_model: GalaxyViewModel, subscriptions: FrozenSet[Subscription]
) -> Tuple[Dict[str, PlanetDetail], Dict[str, ShipDetail]]:
    """Drill-down snapshots for the subscribed entities only.

    A subscription naming an entity that no longer exists is skipped; the
    scene notices the name is missing from the frame and drops it.
    """
    planet_details: Dict[str, PlanetDetail] = {}
    ship_details: Dict[str, ShipDetail] = {}
    for kind, name in subscriptions:
        if kind == "planet":
            planet = view_model.planet_detail(name)
            if planet is not None:
                planet_details[name] = planet
        elif kind == "ship":
            ship = view_model.ship_detail(name)
            if ship is not None:
                ship_details[name] = ship
        else:
            raise ValueError(f"unknown subscription kind {kind!r}")
    return planet_details, ship_details


def build_frame(
    sim: Simulation,
    view_model: GalaxyViewModel,
    subscriptions: FrozenSet[Subscription],
    wellbeing_by_planet: Mapping[str, float],
) -> TurnFrame:
    """Snapshot the simulation into a frame. Runs on the simulation thread.

    ``wellbeing_by_planet`` is the result of ``planet_wellbeing_by_name``,
    passed in so the caller can share one actor sweep with the history
    recorder.
    """
    planets = tuple(view_model.planets(wellbeing_by_planet))
    ships = tuple(view_model.ships())
    planet_details, ship_details = build_details(view_model, subscriptions)
    return TurnFrame(
        turn=sim.current_turn,
        planets=planets,
        ships=ships,
        vitals=galaxy_vitals(planets, ships, aboard=sim.actors_aboard()),
        planet_details=planet_details,
        ship_details=ship_details,
    )


def with_details(
    frame: TurnFrame,
    view_model: GalaxyViewModel,
    subscriptions: FrozenSet[Subscription],
) -> TurnFrame:
    """The same turn's frame with its detail set rebuilt for ``subscriptions``.

    Services a subscription change between turns without re-sweeping every
    planet; only the subscribed entities are inspected.
    """
    planet_details, ship_details = build_details(view_model, subscriptions)
    return replace(frame, planet_details=planet_details, ship_details=ship_details)
