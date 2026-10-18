"""Right-side drill-down panel for a selected planet or ship.

Pure draw functions over the immutable detail snapshots from
:mod:`~spacesim2.ui.live.view_model`. The detail is rebuilt every turn, so
prices, drives, and cargo update while the sim runs. Each ``draw_*`` returns
the panel rect so the caller can keep clicks inside the panel from falling
through to the map.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import Color, Fonts, GoodIcons
from spacesim2.ui.live.view_model import PlanetDetail, ShipDetail

PANEL_W = 380
MARGIN = 12
PAD = 14
# Commodity icon size in market/hold rows, and the gap before the label.
ICON_PX = 16
ICON_GAP = 5
PANEL_FILL: Tuple[int, int, int, int] = (10, 12, 24, 222)
PANEL_BORDER: Color = (60, 75, 110)
SECTION: Color = (120, 200, 255)
TEXT: Color = assets.HUD_TEXT
DIM: Color = (120, 130, 160)
BAR_BACK: Color = (30, 38, 60)
SCARCITY_HOT: Color = (220, 90, 70)

# Scarcity pressure saturates at this value in the market; normalises the
# warning colour. Must match SCARCITY_PRESSURE_MAX in core/market.py.
SCARCITY_MAX = 3.0


def _wrap(text: str, font: pygame.font.Font, width: int) -> List[str]:
    """Greedy word-wrap for `last_action` strings."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.size(candidate)[0] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class _PanelWriter:
    """Tracks a y-cursor down a panel surface so sections stack cleanly."""

    def __init__(self, panel: pygame.Surface, fonts: Fonts) -> None:
        self.panel = panel
        self.fonts = fonts
        self.y = PAD
        self.width = panel.get_width() - 2 * PAD

    def text(self, line: str, size: str = "normal", color: Color = TEXT) -> None:
        label = self.fonts.render(line, size, color)
        self.panel.blit(label, (PAD, self.y))
        self.y += label.get_height() + 2

    def section(self, title: str) -> None:
        self.y += 8
        label = self.fonts.render(title.upper(), "small", SECTION)
        self.panel.blit(label, (PAD, self.y))
        rule_y = self.y + label.get_height() + 2
        pygame.draw.line(
            self.panel, PANEL_BORDER, (PAD, rule_y), (PAD + self.width, rule_y)
        )
        self.y = rule_y + 6

    def bar_row(
        self,
        label: str,
        fraction: float,
        color: Color,
        right_text: str,
        marker: float | None = None,
    ) -> None:
        """A labelled horizontal bar with a right-aligned value.

        ``marker``, if given, draws a notch at that fraction. It shows the
        worst-off actor against the planet mean.
        """
        name = self.fonts.render(label, "small", TEXT)
        value = self.fonts.render(right_text, "small", color)
        self.panel.blit(name, (PAD, self.y))
        self.panel.blit(value, (PAD + self.width - value.get_width(), self.y))
        bar_y = self.y + name.get_height() + 2
        bar_w = self.width
        bar_h = 5
        pygame.draw.rect(self.panel, BAR_BACK, (PAD, bar_y, bar_w, bar_h))
        fill_w = int(max(0.0, min(1.0, fraction)) * bar_w)
        if fill_w > 0:
            pygame.draw.rect(self.panel, color, (PAD, bar_y, fill_w, bar_h))
        if marker is not None:
            mx = PAD + int(max(0.0, min(1.0, marker)) * bar_w)
            pygame.draw.line(self.panel, TEXT, (mx, bar_y - 2), (mx, bar_y + bar_h + 1))
        self.y = bar_y + bar_h + 6

    def kv_row(
        self,
        left: str,
        right: str,
        right_color: Color = TEXT,
        icon: Optional[pygame.Surface] = None,
    ) -> None:
        """Left label, right-aligned value on one line.

        When ``icon`` is given it is drawn crisp-scaled to ``ICON_PX`` before
        the label and the label shifts right. Text-only rows are unchanged.
        """
        name = self.fonts.render(left, "small", TEXT)
        value = self.fonts.render(right, "small", right_color)
        label_x = PAD
        row_height = max(name.get_height(), value.get_height())
        if icon is not None:
            scaled = pygame.transform.scale(icon, (ICON_PX, ICON_PX))
            icon_y = self.y + (name.get_height() - ICON_PX) // 2
            self.panel.blit(scaled, (PAD, icon_y))
            label_x = PAD + ICON_PX + ICON_GAP
            row_height = max(row_height, ICON_PX)
        self.panel.blit(name, (label_x, self.y))
        self.panel.blit(value, (PAD + self.width - value.get_width(), self.y))
        self.y += row_height + 3

    def wrapped(self, text: str, color: Color = DIM) -> None:
        font = self.fonts.font("small")
        for line in _wrap(text, font, self.width):
            self.text(line, "small", color)

    def fits(self, rows: int, row_height: int = 18) -> int:
        """How many of ``rows`` fit before the panel bottom, leaving footer room."""
        remaining = self.panel.get_height() - PAD - self.y
        return max(0, min(rows, remaining // row_height))


def _panel_rect(surface: pygame.Surface) -> pygame.Rect:
    height = surface.get_height() - 2 * MARGIN
    return pygame.Rect(surface.get_width() - PANEL_W - MARGIN, MARGIN, PANEL_W, height)


def _blit_panel(
    surface: pygame.Surface, panel: pygame.Surface, rect: pygame.Rect
) -> pygame.Rect:
    surface.blit(panel, rect.topleft)
    pygame.draw.rect(surface, PANEL_BORDER, rect, 1)
    return rect


def draw_planet_panel(
    surface: pygame.Surface, fonts: Fonts, detail: PlanetDetail, icons: GoodIcons
) -> pygame.Rect:
    rect = _panel_rect(surface)
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    panel.fill(PANEL_FILL)
    w = _PanelWriter(panel, fonts)

    w.text(detail.name, "large", TEXT)
    w.text(f"pop {detail.population} · wealth {detail.total_wealth:,}cr", "small", DIM)
    well_color = assets.wellbeing_color(detail.wellbeing)
    w.bar_row(
        "wellbeing", detail.wellbeing, well_color, f"{detail.wellbeing * 100:.0f}%"
    )

    if detail.drives:
        w.section("needs")
        for drive in detail.drives:
            color = assets.wellbeing_color(drive.mean_score)
            w.bar_row(
                drive.name.capitalize(),
                drive.mean_score,
                color,
                f"{drive.mean_score * 100:.0f}%",
                marker=drive.worst_score,
            )

    w.section("market")
    # Stressed goods first so trouble surfaces even when rows get clipped.
    rows = sorted(detail.market, key=lambda r: (-r.scarcity, -r.volume_30d))
    visible = w.fits(len(rows))
    for row in rows[:visible]:
        if row.scarcity > 0.05:
            heat = min(1.0, row.scarcity / SCARCITY_MAX)
            color = assets.lerp_color((235, 200, 110), SCARCITY_HOT, heat)
            right = f"{row.price}cr  scarce"
        else:
            color = TEXT
            right = f"{row.price}cr"
        w.kv_row(
            f"{row.commodity_name}  ·  vol {row.volume_30d:.1f}/d",
            right,
            color,
            icon=icons.get(row.commodity_id),
        )
    if visible < len(rows):
        w.text(f"… {len(rows) - visible} more", "small", DIM)

    if detail.docked_ships and w.fits(2):
        w.section("docked ships")
        w.wrapped(", ".join(detail.docked_ships), TEXT)

    if detail.recent_trades and w.fits(3):
        w.section("recent trades")
        for trade in detail.recent_trades[: w.fits(len(detail.recent_trades))]:
            w.kv_row(
                f"t{trade.turn}  {trade.commodity_name} ×{trade.quantity}",
                f"@ {trade.price}cr",
                DIM,
            )

    return _blit_panel(surface, panel, rect)


def draw_ship_panel(
    surface: pygame.Surface, fonts: Fonts, detail: ShipDetail, icons: GoodIcons
) -> pygame.Rect:
    rect = _panel_rect(surface)
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    panel.fill(PANEL_FILL)
    w = _PanelWriter(panel, fonts)

    w.text(detail.name, "large", TEXT)
    w.text(detail.status, "small", DIM)
    if detail.route:
        w.text(detail.route, "small", assets.SHIP_ENGINE)

    w.section("ship")
    w.kv_row("money", f"{detail.money:,}cr")
    w.kv_row("fuel", f"{detail.fuel_units:.3f} / {detail.fuel_capacity_units:.3f}")
    cargo_frac = (
        detail.cargo_used / detail.cargo_capacity if detail.cargo_capacity else 0.0
    )
    w.bar_row(
        "cargo",
        cargo_frac,
        SECTION,
        f"{detail.cargo_used}/{detail.cargo_capacity}",
    )

    if detail.cargo:
        w.section("hold")
        for row in detail.cargo[: w.fits(len(detail.cargo))]:
            w.kv_row(
                row.commodity_name, f"×{row.quantity}", icon=icons.get(row.commodity_id)
            )

    if detail.last_action and detail.last_action != "None" and w.fits(3):
        w.section("last action")
        w.wrapped(detail.last_action)

    return _blit_panel(surface, panel, rect)
