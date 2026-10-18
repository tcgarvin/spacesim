"""Unit tests for the live view's baked-asset helpers.

Covers heading-to-frame mapping in the renderer's screen-space convention,
sprite-strip slicing, and ``GoodIcons`` missing-id behaviour. Uses SDL's dummy
video driver so the pygame-backed cases run headless.
"""

import math
import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from spacesim2.ui.live.assets import (  # noqa: E402
    GoodIcons,
    ShipSprites,
    _slice_strip,
    heading_to_frame,
)

# Strip order baked by the pipeline: frame index -> compass facing. The renderer
# heading is screen-space, y-down: 0 = east, +pi/2 = south, -pi/2 = north.
# Each entry is (label, heading_radians, expected_frame_index).
_SECTORS = [
    ("E", 0.0, 0),
    ("NE", -math.pi / 4, 1),
    ("N", -math.pi / 2, 2),
    ("NW", -3 * math.pi / 4, 3),
    ("W", math.pi, 4),
    ("SW", 3 * math.pi / 4, 5),
    ("S", math.pi / 2, 6),
    ("SE", math.pi / 4, 7),
]


def test_heading_to_frame_hits_each_of_eight_sectors() -> None:
    for label, heading, expected in _SECTORS:
        assert heading_to_frame(heading, 8) == expected, label


def test_heading_to_frame_is_periodic_over_2pi() -> None:
    # Adding/subtracting a full turn must not change the chosen facing.
    for _label, heading, expected in _SECTORS:
        assert heading_to_frame(heading + 2 * math.pi, 8) == expected
        assert heading_to_frame(heading - 2 * math.pi, 8) == expected


def test_heading_to_frame_wraps_west_from_both_sides() -> None:
    # +pi and -pi are both west, frame 4.
    assert heading_to_frame(math.pi, 8) == 4
    assert heading_to_frame(-math.pi, 8) == 4


def test_heading_to_frame_near_boundaries_picks_adjacent_facing() -> None:
    # Either side of a sector boundary lands on one of the two flanking frames.
    step = 2 * math.pi / 8
    for _label, heading, expected in _SECTORS:
        boundary = heading - step / 2.0  # halfway toward the previous facing
        neighbour = (expected + 1) % 8  # previous facing in strip order
        assert heading_to_frame(boundary + 1e-4, 8) == expected
        assert heading_to_frame(boundary - 1e-4, 8) == neighbour


def test_heading_to_frame_always_in_range_for_full_sweep() -> None:
    for i in range(720):
        heading = -math.pi + i * (2 * math.pi / 720)
        assert 0 <= heading_to_frame(heading, 8) < 8


def test_slice_strip_yields_expected_count_and_size() -> None:
    frames, size = 8, 10
    strip = pygame.Surface((frames * size, size), pygame.SRCALPHA)
    # A distinct colour per cell shows the cut is per-frame.
    for i in range(frames):
        strip.fill((i * 20 + 10, 0, 0, 255), pygame.Rect(i * size, 0, size, size))

    cut = _slice_strip(strip, frames, size)

    assert len(cut) == frames
    for i, frame in enumerate(cut):
        assert frame.get_size() == (size, size)
        assert frame.get_at((size // 2, size // 2))[0] == i * 20 + 10


def test_good_icons_missing_id_returns_none_and_known_id_returns_surface() -> None:
    # Needs a display for convert_alpha on the committed PNGs.
    pygame.display.init()
    pygame.display.set_mode((64, 64))
    try:
        icons = GoodIcons()
        # Committed assets: the set is populated and truthy.
        assert icons
        assert icons.get("food") is not None
        # Icons are optional decoration, so unknown ids resolve to None.
        assert icons.get("definitely_not_a_commodity") is None
    finally:
        pygame.display.quit()


def test_ship_sprites_frame_for_heading_returns_baked_surface() -> None:
    pygame.display.init()
    pygame.display.set_mode((64, 64))
    try:
        sprites = ShipSprites()
        # Committed freighter strip: truthy and every facing resolves.
        assert sprites
        east = sprites.frame_for_heading(0.0)
        north = sprites.frame_for_heading(-math.pi / 2)
        assert east.get_size() == (64, 64)
        assert north.get_size() == (64, 64)
    finally:
        pygame.display.quit()
