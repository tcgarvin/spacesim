"""Unit tests for the planet renderer's wellbeing cues.

The glow and tint mappings are pure functions and are asserted directly. A
pygame-backed case using the SDL dummy driver checks that the tint overlay
changes a distressed world's sprite pixels and leaves a thriving one alone.
"""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
import pytest  # noqa: E402

from spacesim2.ui.live import assets  # noqa: E402
from spacesim2.ui.live.entities.planet_view import (  # noqa: E402
    PULSE_WELLBEING,
    glow_strength,
    tint_alpha,
)


def test_glow_strength_increases_as_wellbeing_drops() -> None:
    # Sampled above the pulse threshold so time plays no part.
    healthy = glow_strength(1.0, time_s=0.0)
    middling = glow_strength(0.6, time_s=0.0)
    poor = glow_strength(PULSE_WELLBEING + 0.02, time_s=0.0)
    assert healthy < middling < poor


def test_glow_strength_stays_in_unit_range() -> None:
    for wellbeing in (0.0, 0.1, PULSE_WELLBEING, 0.5, 0.9, 1.0, -0.5, 1.5):
        for time_s in (0.0, 0.3, 0.7, 1.1):
            assert 0.0 <= glow_strength(wellbeing, time_s) <= 1.0


def test_glow_pulses_only_below_threshold() -> None:
    # A famine world's halo varies over time; a healthy world's never does.
    samples = [glow_strength(0.1, t / 10.0) for t in range(12)]
    assert max(samples) - min(samples) > 0.05
    steady = [glow_strength(0.8, t / 10.0) for t in range(12)]
    assert max(steady) == min(steady)


def test_famine_glow_outshines_any_healthy_pulse_trough() -> None:
    # Even at its dimmest, a famine world must read stronger than a calm one.
    famine_trough = min(glow_strength(0.05, t / 20.0) for t in range(40))
    assert famine_trough > glow_strength(0.9, time_s=0.0)


def test_tint_alpha_is_zero_for_healthy_worlds() -> None:
    for wellbeing in (0.75, 0.9, 1.0, 1.5):
        assert tint_alpha(wellbeing) == 0


def test_tint_alpha_ramps_up_with_distress() -> None:
    mild = tint_alpha(0.6)
    severe = tint_alpha(0.1)
    floor = tint_alpha(0.0)
    assert 0 < mild < severe <= floor <= 255


@pytest.mark.parametrize("wellbeing,expect_tinted", [(0.1, True), (1.0, False)])
def test_tint_overlay_changes_baked_sprite_pixels(
    wellbeing: float, expect_tinted: bool
) -> None:
    """The multiply tint, as in draw_planet, recolours only a sick world's sprite."""
    pygame.display.init()
    pygame.display.set_mode((64, 64))
    try:
        sprite = pygame.Surface((32, 32), pygame.SRCALPHA)
        sprite.fill((200, 200, 200, 255))
        before = sprite.get_at((16, 16))

        alpha = tint_alpha(wellbeing)
        if alpha > 0:
            overlay = sprite.copy()
            glow_color = assets.wellbeing_color(wellbeing)
            overlay.fill((*glow_color, 255), special_flags=pygame.BLEND_RGBA_MULT)
            overlay.set_alpha(alpha)
            sprite.blit(overlay, (0, 0))

        after = sprite.get_at((16, 16))
        if expect_tinted:
            # Famine tint is red-dominant: green/blue must drop, silhouette kept.
            assert after[1] < before[1] and after[2] < before[2]
            assert after[3] == 255
        else:
            assert after == before
    finally:
        pygame.display.quit()
