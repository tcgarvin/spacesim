"""Value-noise and fractal Brownian motion on numpy grids.

Shared by the nebula backdrop and the placeholder sprites. Deterministic for a
given seed so a generated backdrop is stable across frames.
"""

from __future__ import annotations

import numpy as np


def _smoothstep(t: np.ndarray) -> np.ndarray:
    return np.asarray(t * t * (3.0 - 2.0 * t), dtype=np.float64)


def value_noise(width: int, height: int, cells: int, seed: int) -> np.ndarray:
    """Value noise in [0, 1] on a ``height x width`` grid.

    ``cells`` controls feature size: the lattice is ``cells x cells`` random
    values interpolated with smoothstep across the image.
    """
    rng = np.random.default_rng(seed)
    lattice = rng.random((cells + 1, cells + 1))

    ys = np.linspace(0, cells, height, endpoint=False)
    xs = np.linspace(0, cells, width, endpoint=False)
    gx, gy = np.meshgrid(xs, ys)

    x0 = np.floor(gx).astype(int)
    y0 = np.floor(gy).astype(int)
    x1 = x0 + 1
    y1 = y0 + 1

    fx = _smoothstep(gx - x0)
    fy = _smoothstep(gy - y0)

    v00 = lattice[y0, x0]
    v10 = lattice[y0, x1]
    v01 = lattice[y1, x0]
    v11 = lattice[y1, x1]

    top = v00 * (1 - fx) + v10 * fx
    bottom = v01 * (1 - fx) + v11 * fx
    return np.asarray(top * (1 - fy) + bottom * fy, dtype=np.float64)


def fbm(
    width: int, height: int, octaves: int = 5, seed: int = 0, base_cells: int = 3
) -> np.ndarray:
    """Fractal Brownian motion: summed value-noise octaves, normalized to [0, 1]."""
    total = np.zeros((height, width), dtype=np.float64)
    amplitude = 1.0
    cells = base_cells
    norm = 0.0
    for octave in range(octaves):
        total += amplitude * value_noise(width, height, cells, seed + octave)
        norm += amplitude
        amplitude *= 0.5
        cells *= 2
    total /= norm
    # Stretch to use the full range.
    lo, hi = float(total.min()), float(total.max())
    if hi - lo > 1e-9:
        total = (total - lo) / (hi - lo)
    return total
