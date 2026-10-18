"""Queue of ships to build, filled during the actor phase and drained serially.

The actor phase is sharded across threads and an actor may touch only its own
state and its own planet's market (docs/performance.md). Appending to
``Simulation.ships`` from a brain would violate that, so a trading company that
has bought a hull records a :class:`ShipSpawn` here instead.
``Simulation.run_turn`` drains the queue after the actor phase and before the
ship phase, so a ship built this turn takes its first turn this turn.

The endowment has already left the company's money when the request is made.
It lands on the new ship at drain time, so no money is created or destroyed by
the round trip.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from spacesim.core.actor import Actor
    from spacesim.core.planet import Planet


@dataclass(frozen=True)
class ShipSpawn:
    """One ship to build: who owns it, where it appears, what cash it carries."""

    owner: "Actor"
    planet: "Planet"
    endowment: int


class ShipSpawnQueue:
    """Thread-safe list of pending :class:`ShipSpawn` requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: List[ShipSpawn] = []

    def request(self, spawn: ShipSpawn) -> None:
        """Add a spawn request. Safe to call from any actor-phase thread."""
        with self._lock:
            self._requests.append(spawn)

    def drain(self) -> List[ShipSpawn]:
        """Take every pending request, leaving the queue empty."""
        with self._lock:
            pending = self._requests
            self._requests = []
        return pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._requests)
