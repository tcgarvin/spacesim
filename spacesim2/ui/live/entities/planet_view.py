"""Draws a planet: baked or placeholder sphere, wellbeing glow, and label.

Wellbeing is the one signal every world must show at a glance, so the cues
scale with distress, defined as 1 - wellbeing:

- The halo is faint on a thriving world and grows larger, hotter, and
  brighter as wellbeing drops.
- Below ``PULSE_WELLBEING`` the halo slowly pulses so a famine catches the
  eye in a busy frame.
- A wellbeing-coloured tint, masked to the sprite's silhouette, fades in over
  the baked art on sick worlds. Healthy worlds show the untouched painting.
"""

from __future__ import annotations

import math

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import Fonts, PlanetSprites
from spacesim2.ui.live.camera import Camera
from spacesim2.ui.live.procgen.placeholders import planet_sprite
from spacesim2.ui.live.view_model import PlanetSnapshot

# Planet visual radius in map units, independent of population. The scene
# uses it for click hit-testing and selection rings.
PLANET_MAP_RADIUS = 2.2
# Floor so a world stays a visible, clickable disc when a large galaxy is
# zoomed out to fit the screen.
MIN_PLANET_PX = 5
# Labels are drawn only once worlds are at least this big on screen;
# otherwise a dense galaxy becomes a wall of overlapping text. Hovered and
# selected planets are always labelled.
LABEL_MIN_RADIUS_PX = 8

# Wellbeing below this makes the halo pulse.
PULSE_WELLBEING = 0.35
_PULSE_HZ = 0.9

# Sprite tint stays fully off until wellbeing drops below this, so healthy
# worlds always show the untouched painterly art.
_TINT_FREE_WELLBEING = 0.75
_TINT_MAX_ALPHA = 130

_GLOW_MAX_ALPHA = 110


def _distress(wellbeing: float) -> float:
    """Clamp wellbeing to [0, 1] and invert: 0 = thriving, 1 = famine."""
    return 1.0 - max(0.0, min(1.0, wellbeing))


def glow_strength(wellbeing: float, time_s: float) -> float:
    """Halo intensity in [0, 1]; distressed worlds pulse over ``time_s``.

    The distress exponent keeps mid-range worlds modest so a failing world
    stands apart from a mediocre one.
    """
    strength = 0.15 + 0.85 * math.pow(_distress(wellbeing), 1.5)
    if wellbeing < PULSE_WELLBEING:
        pulse = 0.5 + 0.5 * math.sin(2.0 * math.pi * _PULSE_HZ * time_s)
        strength *= 0.7 + 0.3 * pulse
    return min(strength, 1.0)


def tint_alpha(wellbeing: float) -> int:
    """Surface alpha for the wellbeing tint over the baked sprite.

    Zero at or above ``_TINT_FREE_WELLBEING``, ramping linearly to
    ``_TINT_MAX_ALPHA`` at wellbeing 0.
    """
    span = _distress(wellbeing) - (1.0 - _TINT_FREE_WELLBEING)
    return int(_TINT_MAX_ALPHA * max(0.0, span) / _TINT_FREE_WELLBEING)


def _stable_seed(name: str) -> int:
    return abs(hash(name)) % 100_000


def planet_pixel_radius(camera: Camera) -> int:
    """On-screen radius of a world at the camera's current zoom."""
    return max(MIN_PLANET_PX, int(camera.scale(PLANET_MAP_RADIUS)))


def labels_visible(camera: Camera) -> bool:
    """Whether the zoom is close enough to label every world legibly."""
    return camera.scale(PLANET_MAP_RADIUS) >= LABEL_MIN_RADIUS_PX


def draw_planet(
    surface: pygame.Surface,
    planet: PlanetSnapshot,
    camera: Camera,
    fonts: Fonts,
    sprites: PlanetSprites,
    time_s: float,
    show_label: bool = True,
) -> None:
    screen_pos = camera.world_to_screen(planet.pos)
    radius = planet_pixel_radius(camera)

    glow_color = assets.wellbeing_color(planet.wellbeing)
    distress = _distress(planet.wellbeing)

    # Glow ring reach and brightness both scale with distress.
    glow_r = int(radius * (1.5 + 0.9 * distress))
    peak_alpha = int(_GLOW_MAX_ALPHA * glow_strength(planet.wellbeing, time_s))
    glow = pygame.Surface((glow_r * 2, glow_r * 2), pygame.SRCALPHA)
    for i in range(3):
        alpha = peak_alpha - i * (peak_alpha // 3)
        pygame.draw.circle(
            glow,
            (*glow_color, max(0, alpha)),
            (glow_r, glow_r),
            glow_r - i * (glow_r // 4),
        )
    surface.blit(glow, (screen_pos[0] - glow_r, screen_pos[1] - glow_r))

    if sprites:
        # Baked sprite scaled to the current zoom. Stable per world so a
        # planet keeps its look.
        baked = sprites.for_name(planet.name)
        body_sprite = pygame.transform.smoothscale(baked, (radius * 2, radius * 2))
        overlay_alpha = tint_alpha(planet.wellbeing)
        if overlay_alpha > 0:
            # Multiply-tint a copy, which keeps the sprite's alpha silhouette,
            # then fade it over the art by distress. smoothscale returned a
            # fresh surface, so mutating body_sprite never touches the cache.
            overlay = body_sprite.copy()
            overlay.fill((*glow_color, 255), special_flags=pygame.BLEND_RGBA_MULT)
            overlay.set_alpha(overlay_alpha)
            body_sprite.blit(overlay, (0, 0))
    else:
        # Procedural fallback: a shaded sphere tinted by wellbeing.
        body = (
            (glow_color[0] + 60) // 2,
            (glow_color[1] + 60) // 2,
            (glow_color[2] + 80) // 2,
        )
        body_sprite = planet_sprite(radius, body, _stable_seed(planet.name))
    surface.blit(body_sprite, (screen_pos[0] - radius, screen_pos[1] - radius))

    if not show_label:
        return
    label = fonts.render(planet.name, "small", assets.HUD_TEXT)
    surface.blit(
        label,
        (screen_pos[0] - label.get_width() // 2, screen_pos[1] + radius + 4),
    )
