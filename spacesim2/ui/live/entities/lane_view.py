"""Draws the star-lane network and highlighted routes.

Lanes are the galaxy's fixed skeleton, so they sit under everything else as
thin dim lines. A highlighted route, meaning the selected ship's flight path
or the lanes touching the selected planet, is drawn brighter and wider on top.
"""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.camera import Camera
from spacesim2.ui.live.view_model import LaneSnapshot

Point = Tuple[float, float]


def _on_screen(surface: pygame.Surface, a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Cheap reject for segments whose bounding box misses the surface."""
    w, h = surface.get_size()
    return not (
        max(a[0], b[0]) < 0
        or min(a[0], b[0]) > w
        or max(a[1], b[1]) < 0
        or min(a[1], b[1]) > h
    )


def draw_lanes(
    surface: pygame.Surface, lanes: Iterable[LaneSnapshot], camera: Camera
) -> None:
    """Draw every lane as a 1px quiet line."""
    for lane in lanes:
        a = camera.world_to_screen(lane.a)
        b = camera.world_to_screen(lane.b)
        if _on_screen(surface, a, b):
            pygame.draw.line(surface, assets.TRADE_LANE, a, b, 1)


def draw_route(
    surface: pygame.Surface,
    waypoints: Sequence[Point],
    camera: Camera,
    color: assets.Color,
    width: int = 2,
) -> None:
    """Draw a polyline through ``waypoints``."""
    if len(waypoints) < 2:
        return
    points = [camera.world_to_screen(p) for p in waypoints]
    pygame.draw.lines(surface, color, False, points, width)


def draw_lane_set(
    surface: pygame.Surface,
    lanes: Iterable[LaneSnapshot],
    camera: Camera,
    color: assets.Color,
    width: int = 2,
) -> None:
    """Draw a subset of lanes highlighted."""
    for lane in lanes:
        a = camera.world_to_screen(lane.a)
        b = camera.world_to_screen(lane.b)
        pygame.draw.line(surface, color, a, b, width)
