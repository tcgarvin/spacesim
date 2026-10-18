"""Draws ships.

A traveling ship gets an engine trail and a glyph facing its heading along
the current lane segment. Docked ships are drawn as a small mark at their
planet. Lanes and route highlights live in ``lane_view``.
"""

from __future__ import annotations

import math

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import ShipSprites
from spacesim2.ui.live.camera import Camera
from spacesim2.ui.live.director import RenderedShip
from spacesim2.ui.live.procgen.placeholders import ship_glyph

# Ship glyph length in map units. The scene uses it for hit-testing.
SHIP_MAP_LENGTH = 1.6
# Floor so a baked sprite stays legible when the whole galaxy is zoomed out.
MIN_SHIP_PX = 12


def draw_ship(
    surface: pygame.Surface,
    rendered: RenderedShip,
    camera: Camera,
    ship_sprites: ShipSprites,
) -> None:
    ship = rendered.snapshot
    screen_pos = camera.world_to_screen(rendered.pos)

    if ship.traveling:
        # The route is a star lane the scene already drew under the ships; only
        # the selected ship's path is highlighted (see lane_view). The engine
        # trail is a short fading segment behind the ship, drawn on a surface
        # sized to its bounding box so per-ship per-frame allocation stays
        # small.
        trail_len = int(camera.scale(SHIP_MAP_LENGTH * 2.5))
        if trail_len > 1:
            heading = rendered.heading
            tx = screen_pos[0] - int(math.cos(heading) * trail_len)
            ty = screen_pos[1] - int(math.sin(heading) * trail_len)
            left, top = min(tx, screen_pos[0]) - 2, min(ty, screen_pos[1]) - 2
            trail = pygame.Surface(
                (abs(tx - screen_pos[0]) + 4, abs(ty - screen_pos[1]) + 4),
                pygame.SRCALPHA,
            )
            pygame.draw.line(
                trail,
                (*assets.SHIP_ENGINE, 90),
                (tx - left, ty - top),
                (screen_pos[0] - left, screen_pos[1] - top),
                2,
            )
            surface.blit(trail, (left, top))

    if ship_sprites:
        # Pick the nearest of the 8 baked facings and scale it to the ship's
        # on-screen footprint. The frame is square, so match the glyph's
        # length*2 footprint; its centred arrow spans SHIP_MAP_LENGTH.
        # Nearest-neighbour ``scale``, not ``smoothscale``, keeps pixel art
        # crisp.
        target = max(MIN_SHIP_PX, int(round(camera.scale(SHIP_MAP_LENGTH * 2.0))))
        frame = ship_sprites.frame_for_heading(rendered.heading)
        sprite = pygame.transform.scale(frame, (target, target))
        surface.blit(
            sprite,
            (screen_pos[0] - target // 2, screen_pos[1] - target // 2),
        )
        return

    length = max(6, int(camera.scale(SHIP_MAP_LENGTH)))
    glyph = ship_glyph(length, rendered.heading, assets.SHIP_BODY, assets.SHIP_ENGINE)
    surface.blit(
        glyph,
        (
            screen_pos[0] - glyph.get_width() // 2,
            screen_pos[1] - glyph.get_height() // 2,
        ),
    )
