"""Procedural stand-in sprites used until baked AI assets exist.

A shaded-sphere planet and an arrow ship glyph, both rendered to transparent
Surfaces. Planet sprites are cached by their visual parameters so they are not
rebuilt every frame.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
import pygame

from spacesim2.ui.live.assets import Color


@lru_cache(maxsize=64)
def planet_sprite(radius: int, base: Color, seed: int) -> pygame.Surface:
    """A shaded sphere lit from upper-left with procedural mottling."""
    radius = max(2, radius)
    d = radius * 2
    cx = cy = radius

    ys, xs = np.mgrid[0:d, 0:d]
    dx = (xs - cx) / radius
    dy = (ys - cy) / radius
    r2 = dx * dx + dy * dy
    inside = r2 <= 1.0

    # Sphere surface normal z; light from upper-left.
    nz = np.sqrt(np.clip(1.0 - r2, 0.0, 1.0))
    lx, ly, lz = -0.5, -0.5, 0.7
    lnorm = math.sqrt(lx * lx + ly * ly + lz * lz)
    shade = (dx * lx + dy * ly + nz * lz) / lnorm
    shade = np.clip(shade, 0.05, 1.0)

    # Mottling for surface texture.
    rng = np.random.default_rng(seed)
    mottle = 0.85 + 0.15 * rng.random((d, d))
    lit = shade * mottle

    base_arr = np.array(base, dtype=np.float64)
    rgb = np.clip(base_arr[None, None, :] * lit[:, :, None], 0, 255)

    surf = pygame.Surface((d, d), pygame.SRCALPHA)
    pixels = np.zeros((d, d, 4), dtype=np.uint8)
    pixels[:, :, :3] = rgb.astype(np.uint8)
    pixels[:, :, 3] = np.where(inside, 255, 0).astype(np.uint8)
    # pygame array3d/alpha want (col, row).
    rgb_view = pygame.surfarray.pixels3d(surf)
    alpha_view = pygame.surfarray.pixels_alpha(surf)
    rgb_view[:, :, :] = np.transpose(pixels[:, :, :3], (1, 0, 2))
    alpha_view[:, :] = np.transpose(pixels[:, :, 3], (1, 0))
    del rgb_view, alpha_view
    return surf


def ship_glyph(
    length: int, heading: float, body: Color, engine: Color
) -> pygame.Surface:
    """An arrowhead pointing along ``heading`` in radians, with an engine dot."""
    length = max(6, length)
    size = length * 2
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = cy = size // 2
    half = length / 2.0

    # Nose and two tail corners in local space, +x forward.
    pts_local = [
        (half, 0.0),
        (-half, half * 0.7),
        (-half * 0.4, 0.0),
        (-half, -half * 0.7),
    ]
    cos_h, sin_h = math.cos(heading), math.sin(heading)
    pts = [
        (cx + lx * cos_h - ly * sin_h, cy + lx * sin_h + ly * cos_h)
        for lx, ly in pts_local
    ]
    pygame.draw.polygon(surf, body, pts)

    # Engine glow just behind the tail.
    ex = cx - half * cos_h
    ey = cy - half * sin_h
    pygame.draw.circle(surf, engine, (int(ex), int(ey)), max(2, length // 5))
    return surf
