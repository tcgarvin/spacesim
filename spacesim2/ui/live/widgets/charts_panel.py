"""Toggleable bottom strip of live time-series charts.

Owns the view state the charts need, whether the strip is showing and which
commodity is selected, and lays out a price and volume chart beside a
citizen-wellbeing chart across the bottom third of the screen. Reads
everything from a :class:`~spacesim2.ui.live.history.HistoryRecorder`.

The selected commodity is shown with ``‹ name ›`` affordances so cycling is
discoverable. When the scene has a planet selected the strip scopes itself to
that planet's market and wellbeing instead of the galaxy aggregate, and
shrinks to clear the drill-down panel.
"""

from __future__ import annotations

from typing import Optional

import pygame

from spacesim2.core.commodity import CommodityDefinition
from spacesim2.ui.live.assets import Fonts
from spacesim2.ui.live.history import HistoryRecorder
from spacesim2.ui.live.widgets import chart

MARGIN = 16
HUD_RESERVE = 28  # leave the bottom status line clear
STRIP_FRACTION = 0.34  # share of the window height the strip occupies
# Tall enough for the large selector title plus the small meta line beneath
# it, so the plot's max-value label does not collide with the header text.
HEADER_H = 56
GUTTER = 24


def strip_reserve_px(screen_height: int) -> int:
    """Pixels the charts strip and HUD line cover along the bottom edge."""
    return HUD_RESERVE + int(screen_height * STRIP_FRACTION)


class ChartsPanel:
    def __init__(self, recorder: HistoryRecorder) -> None:
        self._recorder = recorder
        self.visible = True
        self._selected = 0

    def toggle(self) -> None:
        self.visible = not self.visible

    def cycle_commodity(self, step: int) -> None:
        count = len(self._recorder.commodities)
        if count:
            self._selected = (self._selected + step) % count

    def _selected_commodity(self) -> Optional[CommodityDefinition]:
        commodities = self._recorder.commodities
        if not commodities:
            return None
        return commodities[self._selected % len(commodities)]

    def _now(self) -> int:
        axis = self._recorder.turn_axis()
        return axis[-1] if axis else 0

    def draw(
        self,
        surface: pygame.Surface,
        fonts: Fonts,
        planet_name: Optional[str] = None,
        reserve_right: int = 0,
    ) -> None:
        if not self.visible:
            return
        commodity = self._selected_commodity()
        if commodity is None:
            return

        width, height = surface.get_size()
        strip_h = int(height * STRIP_FRACTION)
        strip = pygame.Rect(
            MARGIN,
            height - HUD_RESERVE - strip_h,
            width - 2 * MARGIN - reserve_right,
            strip_h,
        )
        if strip.width < 200:
            return  # window too narrow to chart anything legible
        chart.draw_panel_background(surface, strip)

        # Wide price and volume column beside a narrower wellbeing column.
        inner = strip.inflate(-2 * GUTTER, -GUTTER)
        price_w = int(inner.width * 0.64)
        price_col = pygame.Rect(inner.left, inner.top, price_w, inner.height)
        well_col = pygame.Rect(
            price_col.right + GUTTER,
            inner.top,
            inner.width - price_w - GUTTER,
            inner.height,
        )

        now = self._now()
        scope = planet_name if planet_name is not None else "galaxy"
        well_title = f"{planet_name} wellbeing" if planet_name else "Citizen wellbeing"
        self._draw_selector(surface, fonts, price_col, commodity, scope)
        self._draw_section_title(surface, fonts, well_col, well_title)

        price_plot = pygame.Rect(
            price_col.left,
            price_col.top + HEADER_H,
            price_col.width,
            price_col.height - HEADER_H,
        )
        well_plot = pygame.Rect(
            well_col.left,
            well_col.top + HEADER_H,
            well_col.width,
            well_col.height - HEADER_H,
        )

        if planet_name is not None:
            prices = self._recorder.planet_prices(planet_name, commodity.id)
            volumes = self._recorder.planet_volumes(planet_name, commodity.id)
            wellbeing = self._recorder.planet_wellbeing_series(planet_name)
        else:
            prices = self._recorder.prices(commodity.id)
            volumes = self._recorder.volumes(commodity.id)
            wellbeing = self._recorder.wellbeing()

        chart.draw_price_volume_plot(
            surface,
            price_plot,
            self._recorder.turn_axis(),
            prices,
            volumes,
            now,
            fonts,
        )
        chart.draw_wellbeing_plot(
            surface,
            well_plot,
            self._recorder.turn_axis(),
            wellbeing,
            now,
            fonts,
        )

    def _draw_selector(
        self,
        surface: pygame.Surface,
        fonts: Fonts,
        col: pygame.Rect,
        commodity: CommodityDefinition,
        scope: str,
    ) -> None:
        """Commodity name flanked by arrows, with the keybinding spelled out."""
        name = fonts.render(f"‹  {commodity.name}  ›", "large", chart.PRICE_LINE)
        surface.blit(name, (col.left, col.top))
        idx = self._selected % max(1, len(self._recorder.commodities))
        meta = fonts.render(
            f"{scope} price & volume · last {chart.WINDOW_TURNS} turns    "
            f"change commodity: [ or ] or arrow keys   "
            f"({idx + 1}/{len(self._recorder.commodities)})",
            "small",
            chart.LABEL_DIM,
        )
        surface.blit(meta, (col.left, col.top + name.get_height()))

    def _draw_section_title(
        self, surface: pygame.Surface, fonts: Fonts, col: pygame.Rect, title: str
    ) -> None:
        label = fonts.render(title, "large", chart.LABEL)
        surface.blit(label, (col.left, col.top))
