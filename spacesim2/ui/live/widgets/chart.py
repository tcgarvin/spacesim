"""Pure-pygame line charts drawn in the galaxy's own aesthetic.

Hand-drawn rather than matplotlib-blitted so the charts match the rest of the
surface: a soft underglow beneath a crisp stroke, volume as dim bars under the
price line, restrained labels. Each ``draw_*`` function is a pure function of
its inputs and the target rect, so they compose inside a panel and test
headlessly.

Time runs on a fixed window: the most recent turn is pinned to the right edge
and older samples move left, falling off once they age past ``WINDOW_TURNS``.
The x-axis scale never rubber-bands.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import Color, Fonts

# Turns visible across the chart width. Newest at the right edge.
WINDOW_TURNS = 100

PANEL_FILL: Tuple[int, int, int, int] = (10, 12, 24, 205)
PANEL_BORDER: Color = (60, 75, 110)
AXIS: Color = (45, 55, 80)
PRICE_LINE: Color = (120, 200, 255)
VOLUME_BAR: Color = (70, 95, 140)
LABEL: Color = assets.HUD_TEXT
LABEL_DIM: Color = (120, 130, 160)


def _visible(
    turns: Sequence[int],
    values: Sequence[float],
    now: int,
    window: int,
) -> Tuple[List[int], List[float]]:
    """Slice ``(turns, values)`` down to samples within the trailing window."""
    out_t: List[int] = []
    out_v: List[float] = []
    for t, v in zip(turns, values):
        if 0 <= now - t <= window:
            out_t.append(t)
            out_v.append(v)
    return out_t, out_v


def _time_x(turn: int, now: int, window: int, rect: pygame.Rect) -> int:
    """Pixel x for ``turn``: ``now`` sits at the right edge, older to the left."""
    age = now - turn
    return rect.right - int(age / window * (rect.width - 1))


def _value_y(value: float, lo: float, hi: float, rect: pygame.Rect) -> int:
    span = hi - lo if hi > lo else 1.0
    return rect.bottom - int((value - lo) / span * (rect.height - 1))


def _glow_line(
    surface: pygame.Surface, points: Sequence[Tuple[int, int]], color: Color
) -> None:
    """Draw a polyline with a soft underglow beneath a crisp 2px stroke.

    The translucent glow passes render onto a surface sized to the polyline's
    bounding box to keep per-frame allocation small.
    """
    if len(points) < 2:
        return
    pad = 4  # room for the widest glow stroke
    left = min(p[0] for p in points) - pad
    top = min(p[1] for p in points) - pad
    width = max(p[0] for p in points) - left + pad
    height = max(p[1] for p in points) - top + pad
    local = [(x - left, y - top) for x, y in points]
    glow = pygame.Surface((max(1, width), max(1, height)), pygame.SRCALPHA)
    pygame.draw.lines(glow, (*color, 60), False, local, 6)
    pygame.draw.lines(glow, (*color, 110), False, local, 3)
    surface.blit(glow, (left, top))
    pygame.draw.lines(surface, color, False, points, 2)


def draw_panel_background(surface: pygame.Surface, rect: pygame.Rect) -> None:
    """Translucent dark fill and thin border for a chart container."""
    fill = pygame.Surface(rect.size, pygame.SRCALPHA)
    fill.fill(PANEL_FILL)
    surface.blit(fill, rect.topleft)
    pygame.draw.rect(surface, PANEL_BORDER, rect, 1)


def draw_price_volume_plot(
    surface: pygame.Surface,
    rect: pygame.Rect,
    turns: Sequence[int],
    prices: Sequence[float],
    volumes: Sequence[float],
    now: int,
    fonts: Fonts,
    window: int = WINDOW_TURNS,
) -> None:
    """Strike price as a glowing line over dim per-turn volume bars.

    Price auto-scales to the visible window's min and max with headroom.
    Volume scales independently to its own visible max so both stay legible.
    """
    v_turns, v_prices = _visible(turns, prices, now, window)
    _, v_volumes = _visible(turns, volumes, now, window)
    if not v_prices:
        hint = fonts.render("awaiting trades…", "small", LABEL_DIM)
        surface.blit(hint, (rect.left, rect.centery))
        return

    # Volume bars across the bottom third, scaled to the window's peak volume.
    v_max = max(v_volumes) if v_volumes else 0.0
    if v_max > 0:
        bar_zone = pygame.Rect(
            rect.left, rect.bottom - rect.height // 3, rect.width, rect.height // 3
        )
        for t, vol in zip(v_turns, v_volumes):
            if vol <= 0:
                continue
            x = _time_x(t, now, window, rect)
            h = int(vol / v_max * bar_zone.height)
            pygame.draw.line(
                surface, VOLUME_BAR, (x, bar_zone.bottom), (x, bar_zone.bottom - h)
            )

    # Price line, auto-scaled with vertical headroom.
    p_lo, p_hi = min(v_prices), max(v_prices)
    if p_hi == p_lo:
        p_lo, p_hi = p_lo - 1.0, p_hi + 1.0
    pad = (p_hi - p_lo) * 0.1
    lo, hi = p_lo - pad, p_hi + pad
    points = [
        (_time_x(t, now, window, rect), _value_y(v, lo, hi, rect))
        for t, v in zip(v_turns, v_prices)
    ]
    _glow_line(surface, points, PRICE_LINE)

    # Min, max, and current readouts.
    hi_lbl = fonts.render(f"{p_hi:.0f}", "small", LABEL_DIM)
    lo_lbl = fonts.render(f"{p_lo:.0f}", "small", LABEL_DIM)
    surface.blit(hi_lbl, (rect.left, rect.top))
    surface.blit(lo_lbl, (rect.left, rect.bottom - lo_lbl.get_height()))
    cur = fonts.render(f"{v_prices[-1]:.0f}", "large", PRICE_LINE)
    surface.blit(cur, (rect.right - cur.get_width(), rect.top))


def draw_wellbeing_plot(
    surface: pygame.Surface,
    rect: pygame.Rect,
    turns: Sequence[int],
    values: Sequence[float],
    now: int,
    fonts: Fonts,
    window: int = WINDOW_TURNS,
) -> None:
    """Mean citizen wellbeing on a fixed 0..1 scale, coloured by current level."""
    # Reference midline at 0.5.
    mid_y = rect.bottom - rect.height // 2
    pygame.draw.line(surface, AXIS, (rect.left, mid_y), (rect.right, mid_y))

    v_turns, v_values = _visible(turns, values, now, window)
    if not v_values:
        return
    color = assets.wellbeing_color(v_values[-1])
    points = [
        (_time_x(t, now, window, rect), _value_y(v, 0.0, 1.0, rect))
        for t, v in zip(v_turns, v_values)
    ]
    _glow_line(surface, points, color)

    cur = fonts.render(f"{v_values[-1] * 100:.0f}%", "large", color)
    surface.blit(cur, (rect.right - cur.get_width(), rect.top))
