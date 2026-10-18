"""Tests for the UI/simulation frame protocol: worker, TurnFrame, subscriptions.

The render thread must only read immutable frames. These tests cover the
synchronous headless worker and the invariant that rendering never touches
simulation objects.
"""

import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
import pytest  # noqa: E402

from spacesim2.core.simulation import Simulation  # noqa: E402
from spacesim2.ui.live import view_model as view_model_module  # noqa: E402
from spacesim2.ui.live.app import LiveGalaxyApp  # noqa: E402
from spacesim2.ui.live.director import Director  # noqa: E402
from spacesim2.ui.live.history import HistoryRecorder  # noqa: E402
from spacesim2.ui.live.view_model import GalaxyViewModel  # noqa: E402
from spacesim2.ui.live.worker import SimulationWorker  # noqa: E402


def _sim(planets: int = 3) -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=planets,
        num_regular_actors=6,
        num_market_makers=1,
        num_ships=2,
    )
    return sim


def _worker(sim: Simulation) -> SimulationWorker:
    return SimulationWorker(sim, GalaxyViewModel(sim), HistoryRecorder(sim))


def test_startup_frame_matches_galaxy_without_extra_history_point() -> None:
    sim = _sim()
    worker = _worker(sim)
    frame = worker.latest_frame
    assert frame.turn == sim.current_turn
    assert {p.name for p in frame.planets} == {p.name for p in sim.planets}
    assert {s.name for s in frame.ships} == {s.name for s in sim.ships}
    assert frame.vitals.population == sum(
        1 for p in sim.planets for a in p.actors if a.actor_type.name != "SERVICE"
    )
    assert frame.planet_details == {} and frame.ship_details == {}
    # The recorder's own baseline is the only turn-0 sample.
    assert worker.history.turn_axis() == [sim.current_turn]


def test_synchronous_turns_publish_one_frame_per_turn() -> None:
    sim = _sim()
    worker = _worker(sim)
    start = sim.current_turn
    frames = []
    for _ in range(4):
        worker.request_turn()  # synchronous: the thread was never started
        frames.append(worker.latest_frame)
    assert sim.current_turn == start + 4
    assert [f.turn for f in frames] == [start + 1, start + 2, start + 3, start + 4]
    assert len({id(f) for f in frames}) == 4, "each turn publishes a new frame"
    assert worker.history.turn_axis() == list(range(start, start + 5))


def test_subscription_details_ride_along_and_are_immediate_when_idle() -> None:
    sim = _sim()
    worker = _worker(sim)
    planet = sim.planets[0].name
    ship = sim.ships[0].name

    # Idle worker: the detail lands in the current frame without a turn.
    worker.subscribe(("planet", planet))
    frame = worker.latest_frame
    assert frame.turn == sim.current_turn
    assert planet in frame.planet_details
    assert frame.planet_details[planet].name == planet

    worker.subscribe(("ship", ship))
    assert ship in worker.latest_frame.ship_details

    # Subscriptions persist across turns.
    worker.run_one_turn_now()
    frame = worker.latest_frame
    assert planet in frame.planet_details and ship in frame.ship_details
    assert frame.planet_details[planet].population > 0

    # Unsubscribing removes the detail; the cheap snapshot layer is untouched.
    worker.unsubscribe(("planet", planet))
    frame = worker.latest_frame
    assert planet not in frame.planet_details
    assert ship in frame.ship_details
    assert frame.has_planet(planet)


def test_unknown_subscription_kind_is_rejected() -> None:
    worker = _worker(_sim())
    with pytest.raises(ValueError):
        worker.subscribe(("moon", "x"))


def test_director_interpolates_between_frames_not_live_positions() -> None:
    sim = _sim()
    worker = _worker(sim)
    director = Director(worker, turns_per_second=10.0)
    before = {r.snapshot.name: r.pos for r in director.rendered_ships()}
    # A whole interval elapses, so one turn is requested and run synchronously.
    director.update(0.1)
    assert worker.latest_frame.turn == sim.current_turn
    director.refresh_frame()  # what the scene does at the top of each draw
    # Right after adopting the new frame alpha is 0: ships sit at their
    # previous positions and glide toward the new ones over the interval.
    assert director.alpha == 0.0
    for rendered in director.rendered_ships():
        assert rendered.pos == pytest.approx(before[rendered.snapshot.name])
    director.update(0.05)
    assert 0.0 < director.alpha <= 1.0


def test_paused_director_requests_no_turns() -> None:
    sim = _sim()
    worker = _worker(sim)
    director = Director(worker, turns_per_second=10.0, paused=True)
    start = sim.current_turn
    for _ in range(20):
        director.update(0.1)
    assert sim.current_turn == start


def test_render_never_touches_simulation_objects(monkeypatch) -> None:
    """Rendering is served entirely from the frozen frame.

    After the frame is built, every snapshot builder is replaced with one that
    raises. A render pass over galaxy, panel, HUD, and charts must still pass.
    """
    sim = _sim()
    app = LiveGalaxyApp(sim, speed=4.0, size=(900, 600))
    try:
        app.initialize()
        scene = app._scene
        assert scene is not None and app._worker is not None
        app.update(0.3)  # one synchronous turn
        scene.selection = ("planet", sim.planets[0].name)  # serviced while idle
        assert sim.planets[0].name in app._worker.latest_frame.planet_details

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("render thread touched simulation objects")

        for name in (
            "planet_wellbeing",
            "planet_wellbeing_by_name",
            "planet_detail",
            "ship_detail",
            "_ship_snapshot",
        ):
            monkeypatch.setattr(view_model_module, name, boom)
        monkeypatch.setattr(sim, "planets", None)
        monkeypatch.setattr(sim, "ships", None)

        for _ in range(3):
            app.render()
        assert scene._panel_rect is not None, "planet panel drew from the frame"
        # Hover, pick, and the HUD are frame-served too.
        assert app._camera is not None
        planet_pos = app._camera.world_to_screen(scene._director.frame.planets[0].pos)
        picked = scene.pick(planet_pos)
        assert picked is not None and picked[0] == "planet"
        app.render()
    finally:
        pygame.quit()


def test_threaded_worker_advances_turns_and_stops_cleanly() -> None:
    """On the real thread, requests are serviced and stop() joins the thread."""
    sim = _sim()
    worker = _worker(sim)
    start_turn = sim.current_turn
    worker.start()
    try:
        deadline = time.monotonic() + 10.0
        for _ in range(3):
            target = worker.latest_frame.turn + 1
            worker.request_turn()
            while worker.latest_frame.turn < target:
                assert time.monotonic() < deadline, "worker never published a frame"
                time.sleep(0.005)
    finally:
        worker.stop()
    assert not worker._thread.is_alive()
    assert worker.latest_frame.turn == start_turn + 3
    assert sim.current_turn == start_turn + 3
