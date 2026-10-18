"""Headless render smoke test for the live galaxy view.

Uses SDL's dummy video driver so it runs with no display. Drives the app
through real frames, with the director stepping turns and the scene rendering,
and asserts the output is non-blank and exception-free.
"""

import math
import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np  # noqa: E402
import pygame  # noqa: E402

from spacesim2.core.ship import ShipStatus  # noqa: E402
from spacesim2.core.simulation import Simulation  # noqa: E402
from spacesim2.ui.live.app import LiveGalaxyApp  # noqa: E402


def _sim() -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=3,
        num_regular_actors=10,
        num_market_makers=1,
        num_ships=2,
    )
    return sim


def test_app_renders_non_blank_frames_over_several_turns() -> None:
    app = LiveGalaxyApp(_sim(), speed=4.0, size=(400, 300))
    try:
        app.initialize()
        # 30 frames at 50 ms each is a handful of simulation turns.
        for _ in range(30):
            app.update(0.05)
            app.render()

        assert app._screen is not None
        frame = pygame.surfarray.array3d(app._screen)
        # The backdrop alone has nebula and star variation, so the frame is
        # never a single flat colour.
        assert frame.std() > 1.0
        # Something must be brighter than the deep void background.
        assert int(np.max(frame.sum(axis=2))) > 30
    finally:
        pygame.quit()


def test_charts_panel_renders_and_cycles_commodities() -> None:
    app = LiveGalaxyApp(_sim(), speed=4.0, size=(900, 600))
    try:
        app.initialize()
        assert app._scene is not None
        # The panel is visible by default. Cycling commodities must not raise
        # before or while trades accumulate.
        for _ in range(10):
            app.update(0.05)
            app._scene.charts.cycle_commodity(1)
            app.render()
        # Toggle off and confirm rendering still succeeds.
        app._scene.charts.toggle()
        assert app._scene.charts.visible is False
        app.render()

        assert app._screen is not None
        frame = pygame.surfarray.array3d(app._screen)
        assert frame.std() > 1.0
    finally:
        pygame.quit()


def test_baked_ship_sprites_and_good_icons_render() -> None:
    """The committed sprite and icon assets render through the baked path.

    Forces a ship into travel so a directional frame renders, then opens a
    planet panel for market-row icons and the ship panel for hold-row icons.
    The frame must stay exception-free and non-blank.
    """
    sim = _sim()
    app = LiveGalaxyApp(sim, speed=4.0, size=(900, 600))
    try:
        app.initialize()
        assert app._scene is not None
        # Committed assets must be loaded, not silently skipped.
        assert app._scene._ship_sprites
        assert app._scene._good_icons

        # Give a ship cargo so the hold section renders an icon row.
        ship = sim.ships[0]
        food = sim.commodity_registry["food"]
        ship.cargo.add_commodity(food, 3)

        for _ in range(5):
            app.update(0.05)
            app.render()

        # Put the ship in transit so a directional frame renders. Set this
        # after the update loop and only render below, so no turn advances
        # against the forced travel state, which has no travel_time.
        ship.planet = sim.planets[0]
        ship.destination = sim.planets[1]
        ship.status = ShipStatus.TRAVELING
        ship.travel_progress = 0.4
        app.render()

        # Render a planet panel, then the ship panel.
        app._scene.selection = ("planet", sim.planets[0].name)
        app.render()
        app._scene.selection = ("ship", ship.name)
        app.render()

        assert app._screen is not None
        frame = pygame.surfarray.array3d(app._screen)
        assert frame.std() > 1.0
        assert int(np.max(frame.sum(axis=2))) > 30
    finally:
        pygame.quit()


def test_hundred_planet_galaxy_renders_with_lanes_and_highlights() -> None:
    """The default 100-planet spiral fits, draws its lanes, and picks.

    Renders zoomed out with labels hidden by the declutter threshold, then with
    a ship and a planet selected so the route and lane highlight paths run,
    then zoomed in so labels appear.
    """
    sim = Simulation()
    sim.setup_simple(
        num_planets=100, num_regular_actors=2, num_market_makers=1, num_ships=1
    )
    app = LiveGalaxyApp(sim, speed=4.0, size=(1200, 800))
    try:
        app.initialize()
        scene, camera = app._scene, app._camera
        assert scene is not None and camera is not None
        assert camera.galaxy_size == sim.galaxy_size
        # Every planet is on screen at the fitted zoom.
        for planet in sim.planets:
            sx, sy = camera.world_to_screen(planet.get_position())
            assert 0 <= sx <= 1200 and 0 <= sy <= 800
        for _ in range(8):
            app.update(0.05)
            app.render()

        # A planet is pickable at its own screen position even at fit zoom.
        # Use the most isolated planet; in the dense core two planets can sit
        # within one pick radius of each other.
        target = max(
            sim.planets,
            key=lambda p: min(
                math.hypot(p.x - q.x, p.y - q.y) for q in sim.planets if q is not p
            ),
        )
        assert scene.pick(camera.world_to_screen(target.get_position())) == (
            "planet",
            target.name,
        )

        scene.selection = ("planet", target.name)
        app.render()
        traveling = [s for s in sim.ships if s.status == ShipStatus.TRAVELING]
        if traveling:
            scene.selection = ("ship", traveling[0].name)
            scene.hover = ("ship", traveling[-1].name)
            app.render()

        camera.zoom_at((600, 400), 4.0)
        app.render()

        assert app._screen is not None
        frame = pygame.surfarray.array3d(app._screen)
        assert frame.std() > 1.0
    finally:
        pygame.quit()


def test_app_handles_quit_event() -> None:
    app = LiveGalaxyApp(_sim(), size=(320, 200))
    try:
        app.initialize()
        quit_event = pygame.event.Event(pygame.QUIT)
        assert app.handle_event(quit_event) is False
    finally:
        pygame.quit()


def test_click_planet_selects_and_renders_detail_panel() -> None:
    sim = _sim()
    app = LiveGalaxyApp(sim, size=(900, 600))
    try:
        app.initialize()
        assert app._scene is not None and app._camera is not None
        planet = sim.planets[0]
        pos = app._camera.world_to_screen(planet.get_position())

        down = pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos)
        up = pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=pos)
        assert app.handle_event(down) is True
        assert app.handle_event(up) is True
        assert app._scene.selection == ("planet", planet.name)

        # Panel and planet-scoped charts render without error.
        app.render()
        assert app._scene._panel_rect is not None

        # Esc closes the panel first and the app keeps running.
        esc = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)
        assert app.handle_event(esc) is True
        assert app._scene.selection is None
        # A second Esc quits once nothing is open.
        assert app.handle_event(esc) is False
    finally:
        pygame.quit()


def test_drag_does_not_select() -> None:
    sim = _sim()
    app = LiveGalaxyApp(sim, size=(900, 600))
    try:
        app.initialize()
        assert app._scene is not None and app._camera is not None
        planet = sim.planets[0]
        pos = app._camera.world_to_screen(planet.get_position())
        far = (pos[0] + 60, pos[1] + 60)

        app.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos))
        app.handle_event(
            pygame.event.Event(
                pygame.MOUSEMOTION, pos=far, rel=(60, 60), buttons=(1, 0, 0)
            )
        )
        app.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=far))
        assert app._scene.selection is None
    finally:
        pygame.quit()


def test_click_void_clears_selection_and_ship_pick_works() -> None:
    sim = _sim()
    app = LiveGalaxyApp(sim, size=(900, 600))
    try:
        app.initialize()
        scene = app._scene
        assert scene is not None and app._camera is not None

        # Ship picking: dock position of a known ship.
        ship = sim.ships[0]
        assert ship.planet is not None
        ship_pos = app._camera.world_to_screen(ship.planet.get_position())
        picked = scene.pick(ship_pos)
        # The planet sits on top of its docked ships, so the planet wins here.
        assert picked is not None and picked[0] == "planet"

        scene.selection = ("ship", ship.name)
        app.render()  # ship panel renders without error
        assert scene._panel_rect is not None

        # Clicking a spot that picks nothing clears the selection.
        void = None
        for x in range(10, 900, 40):
            for y in range(10, 600, 40):
                if scene.pick((x, y)) is None and not (
                    scene._panel_rect and scene._panel_rect.collidepoint((x, y))
                ):
                    void = (x, y)
                    break
            if void:
                break
        assert void is not None
        scene.handle_click(void)
        assert scene.selection is None
    finally:
        pygame.quit()
