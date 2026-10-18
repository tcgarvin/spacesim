"""Full-screen nebula backdrop plus a multi-layer parallax starfield.

The nebula is fBm noise mapped through the house nebula tints over the dark
void, baked once into a Surface and regenerated only on resize. The starfield
is a few depth layers of points that shift against camera pan for parallax.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.procgen.noise import fbm

# Parallax layers: (star_count, depth, brightness). Smaller depth is farther
# away and moves less when the camera pans.
_STAR_LAYERS: Tuple[Tuple[int, float, int], ...] = (
    (220, 0.15, 110),
    (140, 0.35, 170),
    (70, 0.6, 230),
)


def _build_nebula_surface(size: Tuple[int, int], seed: int) -> pygame.Surface:
    width, height = size
    # Work at reduced resolution for speed, then scale up smoothly.
    scale = 4
    w, h = max(1, width // scale), max(1, height // scale)

    base = fbm(w, h, octaves=5, seed=seed, base_cells=2)
    # A second, larger-scale field masks where clouds appear, leaving void.
    mask = fbm(w, h, octaves=3, seed=seed + 100, base_cells=1)
    density = np.clip((base * mask - 0.25) * 2.2, 0.0, 1.0)

    rgb = np.zeros((h, w, 3), dtype=np.float64)
    tints = assets.NEBULA_TINTS
    # Blend between tints across a third noise field for colour variation.
    hue = fbm(w, h, octaves=2, seed=seed + 200, base_cells=2)
    t = hue * (len(tints) - 1)
    idx = np.floor(t).astype(int)
    frac = t - idx
    idx = np.clip(idx, 0, len(tints) - 2)
    for c in range(3):
        lo = np.array([tints[i][c] for i in range(len(tints))])
        rgb[:, :, c] = lo[idx] * (1 - frac) + lo[idx + 1] * frac
    rgb *= density[:, :, None]

    bg = np.array(assets.BACKGROUND, dtype=np.float64)
    out = np.clip(bg[None, None, :] + rgb, 0, 255).astype(np.uint8)

    # numpy is (row, col, ch); pygame surfarray wants (col, row, ch).
    small = pygame.surfarray.make_surface(np.transpose(out, (1, 0, 2)))
    return pygame.transform.smoothscale(small, size)


class Nebula:
    def __init__(self, size: Tuple[int, int], seed: int = 1234) -> None:
        self._seed = seed
        self._size = size
        self._surface = _build_nebula_surface(size, seed)
        self._stars = self._build_stars(size, seed)

    def _build_stars(
        self, size: Tuple[int, int], seed: int
    ) -> List[Tuple[np.ndarray, np.ndarray, float, int]]:
        width, height = size
        rng = np.random.default_rng(seed + 7)
        layers: List[Tuple[np.ndarray, np.ndarray, float, int]] = []
        for count, depth, brightness in _STAR_LAYERS:
            xs = rng.random(count) * width
            ys = rng.random(count) * height
            layers.append((xs, ys, depth, brightness))
        return layers

    def resize(self, size: Tuple[int, int]) -> None:
        if size == self._size:
            return
        self._size = size
        self._surface = _build_nebula_surface(size, self._seed)
        self._stars = self._build_stars(size, self._seed)

    def draw(self, surface: pygame.Surface, pan: Tuple[float, float]) -> None:
        """Blit nebula, then parallax stars offset by camera ``pan`` in map units."""
        surface.blit(self._surface, (0, 0))
        width, height = self._size
        px, py = pan
        for xs, ys, depth, brightness in self._stars:
            ox = (xs - px * depth) % width
            oy = (ys - py * depth) % height
            color = (brightness, brightness, min(255, brightness + 20))
            for i in range(len(ox)):
                surface.set_at((int(ox[i]), int(oy[i])), color)
