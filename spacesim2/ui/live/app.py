"""LiveGalaxyApp: window, main loop, and input for the live galaxy view.

Holds the pygame window, clock, camera, director for turn pacing, and the
galaxy scene. Input: play/pause, speed, zoom, pan, click-to-inspect, quit. A
mouse press pans only once it moves past a small slop; a release inside the
slop is a click and selects the planet or ship under the cursor.
``initialize``, ``update``, and ``render`` are separate so tests can drive
frames headlessly with ``SDL_VIDEODRIVER=dummy`` and no blocking loop.

Turns run on a :class:`~spacesim2.ui.live.worker.SimulationWorker` thread that
``run`` starts and stops. ``initialize`` alone leaves the worker synchronous so
headless drivers step turns deterministically on their own thread.
"""

from __future__ import annotations

from typing import Optional, Tuple

from spacesim2.core.simulation import Simulation

try:
    import pygame

    PYGAME_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pygame installed
    PYGAME_AVAILABLE = False

from spacesim2.ui.live.camera import Camera
from spacesim2.ui.live.director import Director
from spacesim2.ui.live.history import HistoryRecorder
from spacesim2.ui.live.scenes.galaxy_scene import GalaxyScene
from spacesim2.ui.live.view_model import GalaxyViewModel
from spacesim2.ui.live.widgets.charts_panel import strip_reserve_px
from spacesim2.ui.live.worker import SimulationWorker

DEFAULT_SIZE = (1600, 900)
TARGET_FPS = 60
ZOOM_STEP = 1.1
# A press-release pair that moves at most this many pixels is a click;
# anything more is a drag.
CLICK_SLOP_PX = 5


class LiveGalaxyApp:
    def __init__(
        self,
        simulation: Simulation,
        speed: float = 1.0,
        paused: bool = False,
        size: Tuple[int, int] = DEFAULT_SIZE,
    ) -> None:
        self._sim = simulation
        self._speed = speed
        self._paused = paused
        self._size = size

        self._screen: Optional[pygame.Surface] = None
        self._clock: Optional[pygame.time.Clock] = None
        self._camera: Optional[Camera] = None
        self._worker: Optional[SimulationWorker] = None
        self._director: Optional[Director] = None
        self._scene: Optional[GalaxyScene] = None
        self._running = False
        self._dragging = False
        self._last_mouse: Tuple[int, int] = (0, 0)
        self._mouse_down_at: Optional[Tuple[int, int]] = None
        self._hand_cursor = False

    def initialize(self) -> None:
        pygame.init()
        pygame.font.init()
        self._screen = pygame.display.set_mode(self._size, pygame.RESIZABLE)
        pygame.display.set_caption("SpaceSim2 — Live Galaxy")
        self._clock = pygame.time.Clock()

        view_model = GalaxyViewModel(self._sim)
        # Fit the galaxy above the charts strip so the strip does not hide it.
        self._camera = Camera(
            self._size, view_model.galaxy_size, strip_reserve_px(self._size[1])
        )
        self._worker = SimulationWorker(
            self._sim, view_model, HistoryRecorder(self._sim)
        )
        self._director = Director(
            self._worker,
            turns_per_second=self._speed,
            paused=self._paused,
        )
        self._scene = GalaxyScene(
            view_model, self._director, self._worker, self._camera, self._size
        )

    def handle_event(self, event: "pygame.event.Event") -> bool:
        """Process one event. Returns False if the app should quit."""
        assert self._camera is not None and self._director is not None
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                # Esc closes the detail panel first and quits only when
                # nothing is open.
                assert self._scene is not None
                return self._scene.clear_selection()
            if event.key == pygame.K_q:
                return False
            if event.key == pygame.K_SPACE:
                self._director.toggle_pause()
            elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                self._director.change_speed(2.0)
            elif event.key == pygame.K_MINUS:
                self._director.change_speed(0.5)
            elif event.key == pygame.K_TAB:
                assert self._scene is not None
                self._scene.charts.toggle()
            elif event.key in (pygame.K_RIGHTBRACKET, pygame.K_RIGHT):
                assert self._scene is not None
                self._scene.charts.cycle_commodity(1)
            elif event.key in (pygame.K_LEFTBRACKET, pygame.K_LEFT):
                assert self._scene is not None
                self._scene.charts.cycle_commodity(-1)
        elif event.type == pygame.MOUSEWHEEL:
            factor = ZOOM_STEP if event.y > 0 else 1.0 / ZOOM_STEP
            self._camera.zoom_at(pygame.mouse.get_pos(), factor)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._mouse_down_at = event.pos
            self._last_mouse = event.pos
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if not self._dragging and self._mouse_down_at is not None:
                assert self._scene is not None
                self._scene.handle_click(event.pos)
            self._dragging = False
            self._mouse_down_at = None
        elif event.type == pygame.MOUSEMOTION:
            if self._mouse_down_at is not None and not self._dragging:
                # Promote to a drag once the cursor leaves the click slop.
                moved = (
                    abs(event.pos[0] - self._mouse_down_at[0]),
                    abs(event.pos[1] - self._mouse_down_at[1]),
                )
                if max(moved) > CLICK_SLOP_PX:
                    self._dragging = True
            if self._dragging:
                dx = event.pos[0] - self._last_mouse[0]
                dy = event.pos[1] - self._last_mouse[1]
                self._camera.pan_pixels(dx, dy)
            else:
                assert self._scene is not None
                self._scene.update_hover(event.pos)
                self._update_cursor()
            self._last_mouse = event.pos
        elif event.type == pygame.VIDEORESIZE:
            self._resize((event.w, event.h))
        return True

    def _update_cursor(self) -> None:
        """Show a hand over clickable planets/ships, the arrow elsewhere."""
        assert self._scene is not None
        want_hand = self._scene.hover is not None
        if want_hand != self._hand_cursor:
            cursor = (
                pygame.SYSTEM_CURSOR_HAND if want_hand else pygame.SYSTEM_CURSOR_ARROW
            )
            pygame.mouse.set_cursor(cursor)
            self._hand_cursor = want_hand

    def _resize(self, size: Tuple[int, int]) -> None:
        assert self._scene is not None
        self._size = size
        self._screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        self._scene.resize(size)

    def update(self, dt: float) -> None:
        assert self._director is not None
        self._director.update(dt)

    def render(self) -> None:
        assert self._screen is not None and self._scene is not None
        self._scene.draw(self._screen)
        pygame.display.flip()

    def run(self) -> None:
        self.initialize()
        assert self._clock is not None and self._worker is not None
        self._worker.start()
        self._running = True
        try:
            while self._running:
                dt = self._clock.tick(TARGET_FPS) / 1000.0
                for event in pygame.event.get():
                    if not self.handle_event(event):
                        self._running = False
                        break
                self.update(dt)
                self.render()
        finally:
            # Let an in-flight turn finish before pygame is torn down; the
            # worker may still be building a frame.
            self._worker.stop()
            pygame.quit()
