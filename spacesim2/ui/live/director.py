"""Turn pacing and anti-slideshow ship interpolation.

The simulation advances in discrete turns on the worker thread; rendering runs
at about 60 fps. The director converts elapsed real time into turn requests at
``turns_per_second`` and, between published frames, interpolates each ship's
map position between the previous and latest
:class:`~spacesim2.ui.live.frame.TurnFrame` so ships glide instead of
teleporting. The director never touches the simulation.

Position is interpolated rather than ``travel_progress`` because the core
resets progress to 0 on arrival, which would slide the ship backwards. Positions
advance origin, route, destination monotonically, so a position lerp always
reads as forward motion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from spacesim2.ui.live.frame import TurnFrame
from spacesim2.ui.live.view_model import ShipSnapshot
from spacesim2.ui.live.worker import SimulationWorker

MIN_SPEED = 0.1
MAX_SPEED = 30.0


def polyline_point(
    waypoints: Sequence[Tuple[float, float]], fraction: float
) -> Tuple[Tuple[float, float], float]:
    """Point at ``fraction`` (0..1) of a polyline's arc length, and its heading.

    Heading is the direction in radians of the segment the point lies on, so a
    ship turns at each waypoint. A single-point polyline, meaning a docked
    ship, yields that point with heading 0. A zero-length polyline also
    reports heading 0.
    """
    if not waypoints:
        raise ValueError("polyline needs at least one waypoint")
    if len(waypoints) == 1:
        return waypoints[0], 0.0
    fraction = max(0.0, min(1.0, fraction))
    seg_lengths = [
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(waypoints[:-1], waypoints[1:])
    ]
    total = sum(seg_lengths)
    if total == 0.0:
        return waypoints[0], 0.0
    target = fraction * total
    walked = 0.0
    for (ax, ay), (bx, by), length in zip(waypoints[:-1], waypoints[1:], seg_lengths):
        heading = math.atan2(by - ay, bx - ax)
        if walked + length >= target and length > 0.0:
            t = (target - walked) / length
            return (ax + (bx - ax) * t, ay + (by - ay) * t), heading
        walked += length
    # Floating-point slack past the last segment: sit on the destination.
    (ax, ay), (bx, by) = waypoints[-2], waypoints[-1]
    return waypoints[-1], math.atan2(by - ay, bx - ax)


def _route_position(ship: ShipSnapshot) -> Tuple[float, float]:
    return polyline_point(ship.waypoints, ship.progress)[0]


@dataclass(frozen=True)
class RenderedShip:
    snapshot: ShipSnapshot
    pos: Tuple[float, float]  # interpolated map position this frame
    heading: float  # radians, 0 when stationary


class Director:
    def __init__(
        self,
        worker: SimulationWorker,
        turns_per_second: float = 1.0,
        paused: bool = False,
    ) -> None:
        self._worker = worker
        self.turns_per_second = max(MIN_SPEED, min(MAX_SPEED, turns_per_second))
        self.paused = paused
        self._accumulator = 0.0
        # Ships lerp from their positions in the previous frame to the current
        # one, driven by seconds since the current frame arrived.
        self._frame = worker.latest_frame
        self._prev_pos: Dict[str, Tuple[float, float]] = self._positions(self._frame)
        self._since_frame = 0.0

    @property
    def frame(self) -> TurnFrame:
        """The frame everything on screen is drawn from this render pass."""
        return self._frame

    @property
    def alpha(self) -> float:
        """Fraction [0, 1] through the current inter-turn interval."""
        return min(1.0, self._since_frame * self.turns_per_second)

    def toggle_pause(self) -> None:
        self.paused = not self.paused

    def change_speed(self, factor: float) -> None:
        self.turns_per_second = max(
            MIN_SPEED, min(MAX_SPEED, self.turns_per_second * factor)
        )

    def update(self, dt: float) -> None:
        """Advance real time by ``dt`` seconds; request a turn when one is due."""
        self.refresh_frame()
        if self.paused:
            return
        self._since_frame += dt
        self._accumulator += dt
        seconds_per_turn = 1.0 / self.turns_per_second
        if self._accumulator >= seconds_per_turn:
            # No catch-up debt: a sim slower than the requested pace runs
            # continuously, one turn after another.
            self._accumulator = 0.0
            self._worker.request_turn()

    def refresh_frame(self) -> None:
        """Adopt the worker's newest frame, rotating the ship lerp on a new turn."""
        latest = self._worker.latest_frame
        if latest is self._frame:
            return
        if latest.turn != self._frame.turn:
            # New turn: the frame being drawn becomes the lerp origin.
            self._prev_pos = self._positions(self._frame)
            self._since_frame = 0.0
        # Same turn with refreshed details leaves positions unchanged.
        self._frame = latest

    @staticmethod
    def _positions(frame: TurnFrame) -> Dict[str, Tuple[float, float]]:
        return {s.name: _route_position(s) for s in frame.ships}

    def rendered_ships(self) -> List[RenderedShip]:
        """Ships at their interpolated positions for this frame."""
        a = self.alpha
        out: List[RenderedShip] = []
        for ship in self._frame.ships:
            curr, heading = polyline_point(ship.waypoints, ship.progress)
            prev = self._prev_pos.get(ship.name, curr)
            pos = (prev[0] + (curr[0] - prev[0]) * a, prev[1] + (curr[1] - prev[1]) * a)
            if not ship.traveling:
                heading = 0.0
            out.append(RenderedShip(snapshot=ship, pos=pos, heading=heading))
        return out
