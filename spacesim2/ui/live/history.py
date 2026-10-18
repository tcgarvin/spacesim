"""Per-turn time-series recorder feeding the live charts.

Each planet market keeps unbounded per-turn ``price_history`` and
``volume_history``. The live charts need three things those lists lack: a
bounded window so a long session cannot grow without limit, a galaxy-wide
aggregate across every market, and a citizen-wellbeing series, which the core
does not record. :class:`HistoryRecorder` samples all three once per turn into
ring buffers the renderer reads each frame without touching core internals.

The :class:`~spacesim2.ui.live.worker.SimulationWorker` calls :meth:`record`
on the simulation thread after each ``run_turn``, and :meth:`sample` once at
startup for a turn-0 baseline. Charts read from the render thread, so a lock
serializes writes against the list copies readers take. Nothing here mutates
the simulation.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, List, Mapping

from spacesim2.core.actor import ActorType
from spacesim2.core.commodity import CommodityDefinition
from spacesim2.core.planet import Planet
from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.view_model import planet_wellbeing_by_name

# Turns of history to retain, one point per turn.
DEFAULT_WINDOW = 2048


def _global_wellbeing(sim: Simulation, wellbeing_by_name: Mapping[str, float]) -> float:
    """Mean welfare across every regular actor in the galaxy, in [0, 1].

    Weights each planet's mean by its regular population so the result equals
    the mean over all colonists. Market makers are excluded.
    """
    weighted = 0.0
    population = 0
    for planet in sim.planets:
        count = sum(1 for a in planet.actors if a.actor_type != ActorType.SERVICE)
        weighted += wellbeing_by_name.get(planet.name, 0.0) * count
        population += count
    if population == 0:
        return 0.0
    return max(0.0, min(1.0, weighted / population))


def _galaxy_price_and_volume(
    sim: Simulation, commodity: CommodityDefinition
) -> tuple[float, float]:
    """Galaxy-wide strike price and total volume for ``commodity`` this turn.

    The strike price is volume-weighted across planet markets. When no market
    traded this turn it falls back to the mean of each market's ``get_avg_price``,
    which carries the last price forward and never returns zero, so the line
    stays continuous. This matters for the turn-0 baseline, before any market
    has traded. Returns ``(price, total_volume)``.
    """
    weighted_price = 0.0
    total_volume = 0.0
    valuation_sum = 0.0
    market_count = 0
    for planet in sim.planets:
        market = planet.market
        volumes = market.volume_history.get(commodity, [])
        last_volume = float(volumes[-1]) if volumes else 0.0
        if last_volume > 0.0:
            last_price = float(market.price_history[commodity][-1])
            weighted_price += last_price * last_volume
            total_volume += last_volume
        valuation_sum += market.get_avg_price(commodity)
        market_count += 1

    if total_volume > 0.0:
        return weighted_price / total_volume, total_volume
    if market_count > 0:
        return valuation_sum / market_count, 0.0
    return 0.0, 0.0


def _planet_price_and_volume(
    planet: Planet, commodity: CommodityDefinition
) -> tuple[float, float]:
    """One planet market's strike price and volume for ``commodity`` this turn.

    Same continuity rule as the galaxy aggregate: on a turn with no trades the
    price falls back to the market's own valuation.
    """
    market = planet.market
    volumes = market.volume_history.get(commodity, [])
    last_volume = float(volumes[-1]) if volumes else 0.0
    if last_volume > 0.0:
        return float(market.price_history[commodity][-1]), last_volume
    return float(market.get_avg_price(commodity)), 0.0


class HistoryRecorder:
    """Bounded per-turn time series for the live charts.

    One sample per turn: galaxy-wide strike price and volume for every
    transportable commodity, plus mean citizen wellbeing. Read back as plain
    lists for charting.
    """

    def __init__(self, simulation: Simulation, window: int = DEFAULT_WINDOW) -> None:
        self._sim = simulation
        self._window = window
        # Serializes the simulation thread's append against the list copies
        # chart readers take on the render thread.
        self._lock = threading.Lock()
        self.commodities: List[CommodityDefinition] = [
            c
            for c in simulation.commodity_registry.all_commodities()
            if c.transportable
        ]
        self._turns: Deque[int] = deque(maxlen=window)
        self._price: Dict[str, Deque[float]] = {
            c.id: deque(maxlen=window) for c in self.commodities
        }
        self._volume: Dict[str, Deque[float]] = {
            c.id: deque(maxlen=window) for c in self.commodities
        }
        self._wellbeing: Deque[float] = deque(maxlen=window)
        # Per-planet series, keyed by planet name then commodity id. Sampled in
        # lockstep with the galaxy series so they share the same turn axis.
        self._planet_price: Dict[str, Dict[str, Deque[float]]] = {
            p.name: {c.id: deque(maxlen=window) for c in self.commodities}
            for p in simulation.planets
        }
        self._planet_volume: Dict[str, Dict[str, Deque[float]]] = {
            p.name: {c.id: deque(maxlen=window) for c in self.commodities}
            for p in simulation.planets
        }
        self._planet_wellbeing: Dict[str, Deque[float]] = {
            p.name: deque(maxlen=window) for p in simulation.planets
        }
        # Turn-0 baseline so the charts have a point before any turn runs.
        self.sample()

    def sample(self) -> None:
        """Record one data point, running the actor wellbeing sweep itself."""
        self.record(planet_wellbeing_by_name(self._sim))

    def record(self, wellbeing_by_name: Mapping[str, float]) -> None:
        """Record one data point using an already-computed wellbeing sweep.

        The worker passes the same per-planet map it builds the frame from,
        so actors are walked once per turn.
        """
        with self._lock:
            self._turns.append(self._sim.current_turn)
            self._wellbeing.append(_global_wellbeing(self._sim, wellbeing_by_name))
            for commodity in self.commodities:
                price, volume = _galaxy_price_and_volume(self._sim, commodity)
                self._price[commodity.id].append(price)
                self._volume[commodity.id].append(volume)
            for planet in self._sim.planets:
                if planet.name not in self._planet_wellbeing:
                    continue  # planet added after startup is not tracked
                self._planet_wellbeing[planet.name].append(
                    wellbeing_by_name.get(planet.name, 0.0)
                )
                for commodity in self.commodities:
                    price, volume = _planet_price_and_volume(planet, commodity)
                    self._planet_price[planet.name][commodity.id].append(price)
                    self._planet_volume[planet.name][commodity.id].append(volume)

    def turn_axis(self) -> List[int]:
        with self._lock:
            return list(self._turns)

    def prices(self, commodity_id: str) -> List[float]:
        with self._lock:
            return list(self._price.get(commodity_id, ()))

    def volumes(self, commodity_id: str) -> List[float]:
        with self._lock:
            return list(self._volume.get(commodity_id, ()))

    def wellbeing(self) -> List[float]:
        with self._lock:
            return list(self._wellbeing)

    def planet_prices(self, planet_name: str, commodity_id: str) -> List[float]:
        with self._lock:
            return list(self._planet_price.get(planet_name, {}).get(commodity_id, ()))

    def planet_volumes(self, planet_name: str, commodity_id: str) -> List[float]:
        with self._lock:
            return list(self._planet_volume.get(planet_name, {}).get(commodity_id, ()))

    def planet_wellbeing_series(self, planet_name: str) -> List[float]:
        with self._lock:
            return list(self._planet_wellbeing.get(planet_name, ()))
