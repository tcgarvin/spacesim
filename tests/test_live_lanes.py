"""Tests for the live view's star-lane support.

Covers polyline interpolation, galaxy-sized camera fit, and lane and waypoint
snapshots.
"""

import math

import pytest

from spacesim2.core.planet import Planet
from spacesim2.core.ship import Ship, ShipStatus, units_to_milli
from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.camera import FIT_MARGIN, Camera
from spacesim2.ui.live.director import Director, polyline_point
from spacesim2.ui.live.history import HistoryRecorder
from spacesim2.ui.live.view_model import GalaxyViewModel
from spacesim2.ui.live.worker import SimulationWorker


def _sim(num_planets: int = 12) -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=num_planets,
        num_regular_actors=2,
        num_market_makers=0,
        num_ships=1,
    )
    return sim


# --- polyline interpolation ---------------------------------------------------


def _depart(ship: Ship, dest: Planet) -> None:
    """Start a journey, retrying past the random pre-departure maintenance roll."""
    for _ in range(50):
        ship.status = ShipStatus.DOCKED
        if ship.start_journey(dest):
            return
    raise AssertionError(f"ship never departed: {ship.last_action}")


def test_polyline_midpoint_is_arc_length_midpoint_not_vertex() -> None:
    # Total length is 70, so the midpoint at 35 lies 5 units into the second
    # segment, not at the corner vertex.
    waypoints = ((0.0, 0.0), (30.0, 0.0), (30.0, 40.0))
    pos, heading = polyline_point(waypoints, 0.5)
    assert pos == pytest.approx((30.0, 5.0))
    assert heading == pytest.approx(math.pi / 2)


def test_polyline_heading_follows_current_segment() -> None:
    waypoints = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    _, h0 = polyline_point(waypoints, 0.1)
    _, h1 = polyline_point(waypoints, 0.5)
    _, h2 = polyline_point(waypoints, 0.9)
    assert h0 == pytest.approx(0.0)
    assert h1 == pytest.approx(math.pi / 2)
    assert h2 == pytest.approx(math.pi)


def test_polyline_endpoints_and_clamping() -> None:
    waypoints = ((1.0, 2.0), (4.0, 6.0))
    assert polyline_point(waypoints, 0.0)[0] == (1.0, 2.0)
    assert polyline_point(waypoints, 1.0)[0] == (4.0, 6.0)
    assert polyline_point(waypoints, 1.7)[0] == (4.0, 6.0)
    assert polyline_point(waypoints, -3.0)[0] == (1.0, 2.0)


def test_polyline_single_point_and_empty() -> None:
    assert polyline_point(((5.0, 5.0),), 0.4) == ((5.0, 5.0), 0.0)
    with pytest.raises(ValueError):
        polyline_point((), 0.0)


# --- camera -------------------------------------------------------------------


def test_camera_fits_non_square_galaxy_on_screen() -> None:
    galaxy = (400.0, 100.0)
    cam = Camera((800, 600), galaxy)
    # Width is the binding constraint: 800 px / (400 * margin).
    assert cam.zoom == pytest.approx(800 / (400.0 * FIT_MARGIN))
    assert cam.galaxy_size == galaxy
    # Every corner of the box lands inside the screen.
    for corner in ((0.0, 0.0), (400.0, 0.0), (0.0, 100.0), (400.0, 100.0)):
        sx, sy = cam.world_to_screen(corner)
        assert 0 <= sx <= 800
        assert 0 <= sy <= 600
    # Centered on the box, not on a fixed 50,50.
    assert cam.world_to_screen((200.0, 50.0)) == (400, 300)


def test_camera_tall_galaxy_uses_height_as_constraint() -> None:
    cam = Camera((800, 600), (100.0, 300.0))
    assert cam.zoom == pytest.approx(600 / (300.0 * FIT_MARGIN))


def test_camera_fit_keeps_galaxy_above_bottom_reserve() -> None:
    """A square galaxy fitted with a 200 px bottom reserve stays above it."""
    cam = Camera((800, 600), (100.0, 100.0), bottom_reserve_px=200)
    assert cam.zoom == pytest.approx(400 / (100.0 * FIT_MARGIN))
    for corner in ((0.0, 0.0), (100.0, 0.0), (0.0, 100.0), (100.0, 100.0)):
        sx, sy = cam.world_to_screen(corner)
        assert 0 <= sx <= 800
        assert 0 <= sy <= 400
    # Galaxy center sits in the middle of the usable 400 px band.
    assert cam.world_to_screen((50.0, 50.0)) == (400, 200)
    # Resizing with a new reserve refits the usable area.
    cam.resize((800, 600), bottom_reserve_px=0)
    assert cam.fit_zoom == pytest.approx(600 / (100.0 * FIT_MARGIN))


def test_camera_rejects_degenerate_galaxy() -> None:
    with pytest.raises(ValueError):
        Camera((800, 600), (0.0, 100.0))


def test_camera_round_trip_and_zoom_anchor() -> None:
    cam = Camera((1000, 700), (500.0, 500.0))
    world = cam.screen_to_world((123, 456))
    assert cam.world_to_screen(world) == (123, 456)
    cam.zoom_at((123, 456), 2.0)
    assert cam.screen_to_world((123, 456)) == pytest.approx(world)


# --- view model ---------------------------------------------------------------


def test_lanes_snapshot_matches_core_network_and_is_cached() -> None:
    sim = _sim()
    vm = GalaxyViewModel(sim)
    lanes = vm.lanes()
    assert len(lanes) == len(sim.star_lanes)
    core_pairs = {
        frozenset((lane.a.get_position(), lane.b.get_position()))
        for lane in sim.star_lanes.lanes
    }
    assert {frozenset((lane.a, lane.b)) for lane in lanes} == core_pairs
    assert vm.lanes() is lanes
    assert vm.galaxy_size == sim.galaxy_size


def test_docked_ship_waypoints_is_its_dock() -> None:
    vm = GalaxyViewModel(_sim())
    for snap in vm.ships():
        assert not snap.traveling
        assert snap.waypoints == (snap.origin,)


def test_traveling_ship_waypoints_follow_core_route() -> None:
    sim = _sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    # Drive a real departure so the core records a lane route.
    # Plenty of millifuel: these tests are about rendering, not the burn.
    ship.fuel = units_to_milli(500)
    origin = ship.planet
    assert origin is not None
    # Pick a destination that is at least two hops away, if one exists.
    dest = next(
        (
            p
            for p in sim.planets
            if p is not origin and not sim.star_lanes.has_lane(origin, p)
        ),
        None,
    )
    if dest is None:
        pytest.skip("galaxy too small for a multi-hop route")
    _depart(ship, dest)
    assert ship.status == ShipStatus.TRAVELING
    assert len(ship.route) >= 3

    snap = next(s for s in vm.ships() if s.name == ship.name)
    assert snap.traveling
    assert snap.waypoints == tuple(p.get_position() for p in ship.route)
    assert snap.waypoints[0] == origin.get_position()
    assert snap.waypoints[-1] == dest.get_position()

    detail = vm.ship_detail(ship.name)
    assert detail is not None
    expected_names = " -> ".join(p.name for p in ship.route)
    assert detail.route.startswith(expected_names)
    assert detail.route.endswith("(0%)")


def test_forced_travel_without_route_falls_back_to_endpoints() -> None:
    sim = _sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    origin, dest = sim.planets[0], sim.planets[1]
    ship.planet = origin
    ship.destination = dest
    ship.status = ShipStatus.TRAVELING
    ship.travel_progress = 0.5
    snap = next(s for s in vm.ships() if s.name == ship.name)
    assert snap.waypoints == (origin.get_position(), dest.get_position())


def test_director_positions_ship_along_route_with_segment_heading() -> None:
    sim = _sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    # Plenty of millifuel: these tests are about rendering, not the burn.
    ship.fuel = units_to_milli(500)
    origin = ship.planet
    assert origin is not None
    dest = next(
        (
            p
            for p in sim.planets
            if p is not origin and not sim.star_lanes.has_lane(origin, p)
        ),
        None,
    )
    if dest is None:
        pytest.skip("galaxy too small for a multi-hop route")
    _depart(ship, dest)
    ship.travel_progress = 0.5
    # The worker's startup frame snapshots the forced travel state.
    worker = SimulationWorker(sim, vm, HistoryRecorder(sim))
    director = Director(worker, paused=True)

    rendered = next(
        r for r in director.rendered_ships() if r.snapshot.name == ship.name
    )
    waypoints = tuple(p.get_position() for p in ship.route)
    expected_pos, expected_heading = polyline_point(waypoints, 0.5)
    assert rendered.pos == pytest.approx(expected_pos)
    assert rendered.heading == pytest.approx(expected_heading)
    # Heading follows the current lane segment, not the straight origin to
    # destination bearing. The two coincide only for a collinear route.
    first, last = waypoints[0], waypoints[-1]
    chord_heading = math.atan2(last[1] - first[1], last[0] - first[0])
    collinear = all(
        abs(
            (w[0] - first[0]) * (last[1] - first[1])
            - (w[1] - first[1]) * (last[0] - first[0])
        )
        < 1e-6
        for w in waypoints
    )
    assert collinear or abs(rendered.heading - chord_heading) > 1e-6
