"""Minimal chrome: a top status strip and a bottom key-hint line.

The top strip shows turn, pacing, and three galaxy vitals: population, mean
wellbeing, and ships in flight. The bottom line spells out the controls so
nothing is a hidden keybinding.
"""

from __future__ import annotations

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import Fonts
from spacesim2.ui.live.director import Director
from spacesim2.ui.live.frame import TurnFrame

PANEL_FILL = (10, 12, 24, 185)
PANEL_BORDER = (60, 75, 110)
PAUSED_COLOR = (235, 200, 110)
PLAYING_COLOR = (120, 200, 255)
PAD = 10
MARGIN = 12


def draw_status_strip(
    surface: pygame.Surface,
    fonts: Fonts,
    frame: TurnFrame,
    director: Director,
) -> None:
    """Top-left block: turn and pacing on one line, galaxy vitals on the next."""
    vitals = frame.vitals
    population, wellbeing, traveling = (
        vitals.population,
        vitals.wellbeing,
        vitals.traveling,
    )
    well_color = assets.wellbeing_color(wellbeing)

    if director.paused:
        state_text, state_color = "|| paused", PAUSED_COLOR
    else:
        state_text, state_color = (
            f"{director.turns_per_second:g} turns/s",
            (PLAYING_COLOR),
        )

    # Render the pieces first so the backing panel can size to fit.
    turn_lbl = fonts.render(f"turn {frame.turn}", "large", assets.HUD_TEXT)
    state_lbl = fonts.render(state_text, "normal", state_color)
    vitals_pre = fonts.render(
        f"pop {population} · wellbeing ", "normal", assets.HUD_TEXT
    )
    vitals_well = fonts.render(f"{wellbeing * 100:.0f}%", "normal", well_color)
    vitals_post = fonts.render(f" · {traveling} in flight", "normal", assets.HUD_TEXT)

    line1_w = turn_lbl.get_width() + 14 + state_lbl.get_width()
    line2_w = vitals_pre.get_width() + vitals_well.get_width() + vitals_post.get_width()
    width = max(line1_w, line2_w) + 2 * PAD
    line1_h = turn_lbl.get_height()
    line2_h = vitals_pre.get_height()
    height = line1_h + 4 + line2_h + 2 * PAD

    panel = pygame.Surface((width, height), pygame.SRCALPHA)
    panel.fill(PANEL_FILL)
    panel.blit(turn_lbl, (PAD, PAD))
    panel.blit(
        state_lbl,
        (PAD + turn_lbl.get_width() + 14, PAD + line1_h - state_lbl.get_height() - 2),
    )
    y2 = PAD + line1_h + 4
    panel.blit(vitals_pre, (PAD, y2))
    panel.blit(vitals_well, (PAD + vitals_pre.get_width(), y2))
    panel.blit(
        vitals_post, (PAD + vitals_pre.get_width() + vitals_well.get_width(), y2)
    )

    surface.blit(panel, (MARGIN, MARGIN))
    pygame.draw.rect(
        surface, PANEL_BORDER, pygame.Rect(MARGIN, MARGIN, width, height), 1
    )


def draw_help_line(surface: pygame.Surface, fonts: Fonts, has_selection: bool) -> None:
    """Bottom key hints; the escape hint flips meaning while a panel is open."""
    esc_hint = "[esc] close panel" if has_selection else "[esc] quit"
    line = (
        "[click] inspect planet/ship   [space] pause   [+/-] speed   "
        f"[tab] charts   [wheel] zoom   [drag] pan   {esc_hint}"
    )
    text = fonts.render(line, "small", assets.HUD_TEXT)
    surface.blit(text, (MARGIN, surface.get_height() - text.get_height() - 10))
