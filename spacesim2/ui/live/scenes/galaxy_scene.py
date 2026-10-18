"""Composes a full galaxy frame and owns map interaction state.

Layering order: backdrop, star lanes, highlighted route, ships, then planets
on top so worlds read as the focal points, then selection chrome, charts, the
drill-down panel, and the HUD. The scene also owns picking: hovering
highlights a planet or ship, clicking selects it and opens a live detail
panel, and the charts strip scopes itself to the selected planet's market.
Selecting a ship highlights its lane route; selecting a planet highlights the
lanes touching it.

Everything drawn comes from the director's current
:class:`~spacesim2.ui.live.frame.TurnFrame`; the scene never reads simulation
objects. A selection is also a subscription on the simulation worker, which
makes the frame carry that entity's drill-down detail.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import pygame

from spacesim2.ui.live import assets
from spacesim2.ui.live.assets import Fonts, GoodIcons, PlanetSprites, ShipSprites
from spacesim2.ui.live.camera import Camera
from spacesim2.ui.live.director import Director, RenderedShip
from spacesim2.ui.live.entities.lane_view import draw_lane_set, draw_lanes, draw_route
from spacesim2.ui.live.entities.planet_view import (
    draw_planet,
    labels_visible,
    planet_pixel_radius,
)
from spacesim2.ui.live.entities.ship_view import SHIP_MAP_LENGTH, draw_ship
from spacesim2.ui.live.procgen.nebula import Nebula
from spacesim2.ui.live.view_model import GalaxyViewModel, LaneSnapshot
from spacesim2.ui.live.widgets import hud, info_panel
from spacesim2.ui.live.widgets.charts_panel import ChartsPanel, strip_reserve_px
from spacesim2.ui.live.worker import SimulationWorker

# (kind, name) where kind is "planet" or "ship".
Selection = Tuple[str, str]

_PICK_SLOP_PX = 8  # extra grab room around small targets
_HOVER_RING = (160, 175, 210)
_SELECT_RING = (235, 220, 160)


class GalaxyScene:
    def __init__(
        self,
        view_model: GalaxyViewModel,
        director: Director,
        worker: SimulationWorker,
        camera: Camera,
        size: tuple[int, int],
    ) -> None:
        # The view model serves only the static lane skeleton; all live state
        # comes through the director's frame.
        self._vm = view_model
        self._director = director
        self._worker = worker
        self._camera = camera
        self._nebula = Nebula(size)
        self._fonts = Fonts()
        # Sprite sets load once here, after the display exists so convert_alpha
        # works, and are passed down; never per frame or through globals.
        self._planet_sprites = PlanetSprites()
        self._ship_sprites = ShipSprites()
        self._good_icons = GoodIcons()
        self.charts = ChartsPanel(worker.history)
        self._selection: Optional[Selection] = None
        self.hover: Optional[Selection] = None
        self._panel_rect: Optional[pygame.Rect] = None

    @property
    def selection(self) -> Optional[Selection]:
        return self._selection

    @selection.setter
    def selection(self, target: Optional[Selection]) -> None:
        """Select ``target`` or nothing, keeping the worker subscription in step.

        Subscribing puts the entity's detail into later frames. Hover does not
        subscribe since it only needs positions.
        """
        if target == self._selection:
            return
        if self._selection is not None:
            self._worker.unsubscribe(self._selection)
        self._selection = target
        if target is not None:
            self._worker.subscribe(target)

    def resize(self, size: tuple[int, int]) -> None:
        self._nebula.resize(size)
        self._camera.resize(size, strip_reserve_px(size[1]))

    # -- Picking ---------------------------------------------------------

    def pick(self, pos: Tuple[int, int]) -> Optional[Selection]:
        """The planet or ship under ``pos``, planets taking priority."""
        planet_r = planet_pixel_radius(self._camera)
        for planet in self._director.frame.planets:
            px, py = self._camera.world_to_screen(planet.pos)
            if math.hypot(pos[0] - px, pos[1] - py) <= planet_r + _PICK_SLOP_PX:
                return ("planet", planet.name)
        ship_r = max(8, int(self._camera.scale(SHIP_MAP_LENGTH)))
        for rendered in self._director.rendered_ships():
            sx, sy = self._camera.world_to_screen(rendered.pos)
            if math.hypot(pos[0] - sx, pos[1] - sy) <= ship_r + _PICK_SLOP_PX:
                return ("ship", rendered.snapshot.name)
        return None

    def handle_click(self, pos: Tuple[int, int]) -> None:
        """Select what is under the cursor; a void click clears the selection.

        Clicks on the open detail panel are swallowed so touching the panel
        does not deselect through it.
        """
        if self._panel_rect is not None and self._panel_rect.collidepoint(pos):
            return
        self.selection = self.pick(pos)

    def update_hover(self, pos: Tuple[int, int]) -> None:
        if self._panel_rect is not None and self._panel_rect.collidepoint(pos):
            self.hover = None
            return
        self.hover = self.pick(pos)

    def clear_selection(self) -> bool:
        """Drop the current selection. Returns True if there was one."""
        if self.selection is None:
            return False
        self.selection = None
        return True

    @property
    def selected_planet(self) -> Optional[str]:
        if self.selection and self.selection[0] == "planet":
            return self.selection[1]
        return None

    # -- Drawing ---------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        # Adopt the newest published frame once so the whole pass draws one
        # consistent turn.
        self._director.refresh_frame()
        frame = self._director.frame
        # Parallax keys off the camera center so panning drifts the starfield.
        self._nebula.draw(surface, (self._camera.center_x, self._camera.center_y))

        # Lane skeleton first, then the focused route over it.
        draw_lanes(surface, self._vm.lanes(), self._camera)
        rendered_ships = self._director.rendered_ships()
        self._draw_focus_lanes(surface, rendered_ships)

        # Ships draw under the worlds.
        for rendered in rendered_ships:
            draw_ship(surface, rendered, self._camera, self._ship_sprites)

        # Wall-clock time drives the distress pulse on suffering worlds.
        time_s = pygame.time.get_ticks() / 1000.0
        all_labels = labels_visible(self._camera)
        focused = {
            target[1] for target in (self.hover, self.selection) if target is not None
        }
        for planet in frame.planets:
            draw_planet(
                surface,
                planet,
                self._camera,
                self._fonts,
                self._planet_sprites,
                time_s,
                show_label=all_labels or planet.name in focused,
            )

        self._draw_rings(surface)
        # Shrink the chart strip so the drill-down panel never covers it.
        reserve = info_panel.PANEL_W + info_panel.MARGIN if self.selection else 0
        self.charts.draw(surface, self._fonts, self.selected_planet, reserve)
        self._panel_rect = self._draw_detail_panel(surface)
        hud.draw_status_strip(surface, self._fonts, frame, self._director)
        hud.draw_help_line(surface, self._fonts, self.selection is not None)

    def _planet_lanes(self, name: str) -> List[LaneSnapshot]:
        """Lanes touching the named planet, matched by map position."""
        for planet in self._director.frame.planets:
            if planet.name == name:
                return [
                    lane
                    for lane in self._vm.lanes()
                    if lane.a == planet.pos or lane.b == planet.pos
                ]
        return []

    def _draw_focus_lanes(
        self, surface: pygame.Surface, rendered_ships: List[RenderedShip]
    ) -> None:
        """Highlight the hovered ship's route, then the selection's lanes on top."""
        routes = {r.snapshot.name: r.snapshot.waypoints for r in rendered_ships}
        if self.hover is not None and self.hover != self.selection:
            kind, name = self.hover
            if kind == "ship" and name in routes:
                draw_route(surface, routes[name], self._camera, assets.ROUTE_HOVER, 1)
        if self.selection is None:
            return
        kind, name = self.selection
        if kind == "ship":
            if name in routes:
                draw_route(
                    surface, routes[name], self._camera, assets.ROUTE_HIGHLIGHT, 2
                )
        else:
            draw_lane_set(
                surface,
                self._planet_lanes(name),
                self._camera,
                assets.LANE_HIGHLIGHT,
                2,
            )

    def _ring_center(self, target: Selection) -> Optional[Tuple[int, int, int]]:
        """Screen (x, y, radius) for a selection ring, or None if it vanished."""
        kind, name = target
        if kind == "planet":
            for planet in self._director.frame.planets:
                if planet.name == name:
                    x, y = self._camera.world_to_screen(planet.pos)
                    r = planet_pixel_radius(self._camera) + 6
                    return (x, y, r)
            return None
        for rendered in self._director.rendered_ships():
            if rendered.snapshot.name == name:
                x, y = self._camera.world_to_screen(rendered.pos)
                r = max(8, int(self._camera.scale(SHIP_MAP_LENGTH))) + 4
                return (x, y, r)
        return None

    def _draw_rings(self, surface: pygame.Surface) -> None:
        if self.hover is not None and self.hover != self.selection:
            ring = self._ring_center(self.hover)
            if ring is not None:
                pygame.draw.circle(surface, _HOVER_RING, ring[:2], ring[2], 1)
        if self.selection is not None:
            ring = self._ring_center(self.selection)
            if ring is not None:
                pygame.draw.circle(surface, _SELECT_RING, ring[:2], ring[2], 2)

    def _draw_detail_panel(self, surface: pygame.Surface) -> Optional[pygame.Rect]:
        """Draw the selection's panel from the frame's subscribed details.

        The detail can lag the selection by a turn when the worker was
        mid-turn at click time. Until it arrives nothing is drawn and the
        selection is kept. The selection is dropped only when the entity has
        vanished from the frame.
        """
        if self.selection is None:
            return None
        kind, name = self.selection
        frame = self._director.frame
        if kind == "planet":
            if not frame.has_planet(name):
                self.selection = None
                return None
            planet = frame.planet_details.get(name)
            if planet is None:
                return None
            return info_panel.draw_planet_panel(
                surface, self._fonts, planet, self._good_icons
            )
        if not frame.has_ship(name):
            self.selection = None
            return None
        ship = frame.ship_details.get(name)
        if ship is None:
            return None
        return info_panel.draw_ship_panel(surface, self._fonts, ship, self._good_icons)
