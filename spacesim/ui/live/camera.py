"""Camera mapping galaxy map-space to screen pixels, with zoom and pan.

The galaxy lives in the ``width x height`` box ``Simulation.galaxy_size``,
which grows with planet count and is rarely square. The camera centers on a
map point and scales by ``zoom`` pixels per map unit, so panning and zooming
never touch entity state.
"""

from __future__ import annotations

from typing import Tuple

# Fraction of the screen left empty around the fitted galaxy.
FIT_MARGIN = 1.1
MIN_ZOOM_FACTOR = 0.5
MAX_ZOOM_FACTOR = 12.0


class Camera:
    def __init__(
        self,
        screen_size: Tuple[int, int],
        galaxy_size: Tuple[float, float],
        bottom_reserve_px: int = 0,
    ) -> None:
        """Create a camera fitted to the galaxy.

        Args:
            screen_size: Window size in pixels.
            galaxy_size: Galaxy box in map units (``Simulation.galaxy_size``).
            bottom_reserve_px: Pixels along the bottom edge covered by the
                charts strip; the fit keeps the galaxy above it.
        """
        width, height = galaxy_size
        if width <= 0.0 or height <= 0.0:
            raise ValueError(f"galaxy_size must be positive, got {galaxy_size}")
        if bottom_reserve_px < 0:
            raise ValueError("bottom_reserve_px must be non-negative")
        self._galaxy_w = width
        self._galaxy_h = height
        self._screen_w, self._screen_h = screen_size
        self._bottom_reserve = bottom_reserve_px
        self.center_x = width / 2.0
        self.center_y = height / 2.0
        self.zoom = self._fit_zoom()
        self._min_zoom = self.zoom * MIN_ZOOM_FACTOR
        self._max_zoom = self.zoom * MAX_ZOOM_FACTOR
        self.fit()

    def fit(self) -> None:
        """Fit the whole galaxy, with margin, into the unreserved screen area."""
        self.zoom = self._fit_zoom()
        self.center_x = self._galaxy_w / 2.0
        # The usable area's middle sits half the reserve above the screen
        # center.
        self.center_y = self._galaxy_h / 2.0 + (self._bottom_reserve / 2.0) / self.zoom

    @property
    def galaxy_size(self) -> Tuple[float, float]:
        return (self._galaxy_w, self._galaxy_h)

    @property
    def fit_zoom(self) -> float:
        """Zoom at which the whole galaxy fits on screen."""
        return self._fit_zoom()

    def _fit_zoom(self) -> float:
        usable_h = max(1, self._screen_h - self._bottom_reserve)
        return min(
            self._screen_w / (self._galaxy_w * FIT_MARGIN),
            usable_h / (self._galaxy_h * FIT_MARGIN),
        )

    def resize(
        self, screen_size: Tuple[int, int], bottom_reserve_px: int | None = None
    ) -> None:
        self._screen_w, self._screen_h = screen_size
        if bottom_reserve_px is not None:
            self._bottom_reserve = bottom_reserve_px
        fit = self._fit_zoom()
        self._min_zoom = fit * MIN_ZOOM_FACTOR
        self._max_zoom = fit * MAX_ZOOM_FACTOR
        self.zoom = max(self._min_zoom, min(self._max_zoom, self.zoom))

    def world_to_screen(self, pos: Tuple[float, float]) -> Tuple[int, int]:
        x, y = pos
        sx = (x - self.center_x) * self.zoom + self._screen_w / 2.0
        sy = (y - self.center_y) * self.zoom + self._screen_h / 2.0
        return int(round(sx)), int(round(sy))

    def scale(self, map_length: float) -> float:
        """Convert a length in map units to pixels at the current zoom."""
        return map_length * self.zoom

    def zoom_at(self, screen_pos: Tuple[int, int], factor: float) -> None:
        """Zoom by ``factor`` while keeping the map point under the cursor fixed."""
        before = self.screen_to_world(screen_pos)
        self.zoom = max(self._min_zoom, min(self._max_zoom, self.zoom * factor))
        after = self.screen_to_world(screen_pos)
        self.center_x += before[0] - after[0]
        self.center_y += before[1] - after[1]

    def screen_to_world(self, screen_pos: Tuple[int, int]) -> Tuple[float, float]:
        sx, sy = screen_pos
        x = (sx - self._screen_w / 2.0) / self.zoom + self.center_x
        y = (sy - self._screen_h / 2.0) / self.zoom + self.center_y
        return x, y

    def pan_pixels(self, dx: int, dy: int) -> None:
        """Drag the view by a pixel delta."""
        self.center_x -= dx / self.zoom
        self.center_y -= dy / self.zoom
