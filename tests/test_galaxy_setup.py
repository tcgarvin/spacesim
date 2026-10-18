"""Tests for galaxy setup: spiral layout, star lanes, names, scaling."""

import math
import random

import pytest

from spacesim2.core.galaxy import (
    MIN_PLANET_DISTANCE,
    StarLaneNetwork,
    crossing_lane_pairs,
    generate_spiral_layout,
    is_connected,
)
from spacesim2.core.market import Market
from spacesim2.core.navigation import Navigator, get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.simulation import Simulation


def _min_pairwise_distance(positions: list[tuple[float, float]]) -> float:
    """Smallest pairwise distance among positions."""
    best = math.inf
    for i, (x1, y1) in enumerate(positions):
        for x2, y2 in positions[i + 1 :]:
            best = min(best, math.hypot(x1 - x2, y1 - y2))
    return best


@pytest.mark.parametrize(
    "num_planets,arms", [(1, 3), (2, 3), (5, 3), (100, 3), (100, 4)]
)
def test_layout_is_connected_planar_and_separated(num_planets: int, arms: int) -> None:
    layout = generate_spiral_layout(num_planets, arms=arms, rng=random.Random(7))
    assert len(layout.positions) == num_planets
    assert is_connected(num_planets, layout.lanes)
    assert crossing_lane_pairs(layout.positions, layout.lanes) == []
    if num_planets > 1:
        assert (
            _min_pairwise_distance(list(layout.positions)) >= MIN_PLANET_DISTANCE - 1e-9
        )
    for x, y in layout.positions:
        assert 0.0 <= x <= layout.width
        assert 0.0 <= y <= layout.height


def test_layout_has_no_duplicate_or_self_lanes() -> None:
    layout = generate_spiral_layout(60, rng=random.Random(3))
    assert len(set(layout.lanes)) == len(layout.lanes)
    assert all(i < j for i, j in layout.lanes)


def test_lane_density_controls_redundancy() -> None:
    tree = generate_spiral_layout(80, lane_density=0.0, rng=random.Random(1))
    dense = generate_spiral_layout(80, lane_density=1.0, rng=random.Random(1))
    assert len(tree.lanes) == 79, "density 0 must leave exactly a spanning tree"
    assert len(dense.lanes) > len(tree.lanes)
    assert is_connected(80, tree.lanes)


def test_layout_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        generate_spiral_layout(0)
    with pytest.raises(ValueError):
        generate_spiral_layout(5, arms=0)
    with pytest.raises(ValueError):
        generate_spiral_layout(5, lane_density=1.5)


def test_500_planet_setup_produces_exactly_500_planets() -> None:
    """A 500-planet galaxy has 500 planets, unique names, and valid spacing."""
    sim = Simulation()
    sim.setup_simple(
        num_planets=500,
        num_regular_actors=0,
        num_market_makers=0,
        num_ships=0,
    )

    assert len(sim.planets) == 500

    names = [p.name for p in sim.planets]
    assert len(set(names)) == 500, "planet names must be unique"
    assert all(name for name in names), "planet names must be non-empty"

    positions = [(p.x, p.y) for p in sim.planets]
    assert _min_pairwise_distance(positions) >= MIN_PLANET_DISTANCE - 1e-9
    assert len(sim.star_lanes) >= 499
    # Every planet is on a lane and the navigator can route between arbitrary
    # pairs. It raises if the network is disconnected.
    assert all(sim.star_lanes.lanes_from(p) for p in sim.planets)
    nav = get_navigator(sim)
    assert nav.distance(sim.planets[0], sim.planets[-1]) < math.inf


def test_setup_records_galaxy_size_and_lanes_match_planets() -> None:
    sim = Simulation()
    sim.setup_simple(
        num_planets=30, num_regular_actors=0, num_market_makers=0, num_ships=0
    )
    width, height = sim.galaxy_size
    for planet in sim.planets:
        assert 0.0 <= planet.x <= width
        assert 0.0 <= planet.y <= height
    planet_set = set(sim.planets)
    for lane in sim.star_lanes.lanes:
        assert lane.a in planet_set and lane.b in planet_set
        assert lane.length == pytest.approx(
            math.hypot(lane.b.x - lane.a.x, lane.b.y - lane.a.y)
        )


def test_setup_accepts_arm_count() -> None:
    sim = Simulation()
    sim.setup_simple(
        num_planets=40, num_regular_actors=0, num_market_makers=0, num_ships=0, arms=5
    )
    assert len(sim.planets) == 40
    assert is_connected(
        40,
        [
            (sim.planets.index(lane.a), sim.planets.index(lane.b))
            for lane in sim.star_lanes.lanes
        ],
    )


def test_procedural_names_are_unique_and_readable() -> None:
    """The procedural name generator avoids collisions with used names."""
    used: set[str] = set()
    for _ in range(1000):
        name = Simulation._generate_procedural_name(used)
        assert name not in used
        assert name[0].isupper()
        used.add(name)


def test_map_scales_with_planet_count() -> None:
    """Bigger galaxies get bigger maps at constant arm density."""
    small = generate_spiral_layout(5, rng=random.Random(2))
    large = generate_spiral_layout(200, rng=random.Random(2))
    assert large.width * large.height > 4 * small.width * small.height


# --- Navigator over lanes ---------------------------------------------------


def _lane_world(specs, lanes):
    planets = [Planet(name, Market(), x, y) for name, x, y in specs]
    by_name = {p.name: p for p in planets}
    network = StarLaneNetwork()
    for a, b in lanes:
        network.add_lane(by_name[a], by_name[b])
    sim = type(
        "MockSim",
        (object,),
        {"planets": planets, "star_lanes": network, "current_turn": 0},
    )()
    return sim, by_name


def test_navigator_distance_follows_lanes_not_straight_line() -> None:
    # A - B - C in a line; A and C are not directly linked.
    sim, p = _lane_world(
        [("A", 0, 0), ("B", 30, 0), ("C", 60, 0), ("D", 30, 40)],
        [("A", "B"), ("B", "C"), ("B", "D")],
    )
    nav = Navigator(sim)
    assert nav.distance(p["A"], p["C"]) == 60.0
    assert nav.distance(p["A"], p["D"]) == 30.0 + 40.0  # via B, not hypot(30,40)=50
    assert nav.route(p["A"], p["D"]) == [p["A"], p["B"], p["D"]]
    assert nav.route(p["A"], p["A"]) == [p["A"]]
    assert nav.planets_by_proximity(p["A"]) == [p["B"], p["C"], p["D"]]


def test_navigator_picks_shortest_of_several_routes() -> None:
    sim, p = _lane_world(
        [("A", 0, 0), ("B", 10, 0), ("C", 20, 0), ("X", 10, 100)],
        [("A", "B"), ("B", "C"), ("A", "X"), ("X", "C")],
    )
    nav = Navigator(sim)
    assert nav.distance(p["A"], p["C"]) == 20.0
    assert nav.route(p["A"], p["C"]) == [p["A"], p["B"], p["C"]]


def test_navigator_raises_on_disconnected_network() -> None:
    sim, p = _lane_world([("A", 0, 0), ("B", 10, 0), ("C", 20, 0)], [("A", "B")])
    nav = Navigator(sim)
    with pytest.raises(ValueError, match="disconnected"):
        nav.distance(p["A"], p["C"])


def test_star_lane_network_rejects_duplicates_and_self_lanes() -> None:
    a = Planet("A", Market(), 0, 0)
    b = Planet("B", Market(), 10, 0)
    network = StarLaneNetwork()
    network.add_lane(a, b)
    assert network.has_lane(b, a)
    assert network.neighbors(a) == [b]
    with pytest.raises(ValueError):
        network.add_lane(b, a)
    with pytest.raises(ValueError):
        network.add_lane(a, a)
