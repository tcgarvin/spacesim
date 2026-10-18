"""Tests for the live galaxy view's read-only adapters."""

from spacesim2.core.actor import ActorType
from spacesim2.core.ship import ShipStatus
from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.view_model import GalaxyViewModel, planet_wellbeing


def _small_sim() -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=2,
        num_regular_actors=8,
        num_market_makers=1,
        num_ships=2,
    )
    return sim


def test_planet_wellbeing_in_unit_range_and_excludes_market_makers() -> None:
    sim = _small_sim()
    sim.run_turn()

    for planet in sim.planets:
        wellbeing = planet_wellbeing(planet)
        assert 0.0 <= wellbeing <= 1.0

        # Recomputing over regular actors only must match: market makers
        # must not influence the value.
        scores = []
        for actor in planet.actors:
            needs = [d for d in actor.drives if d.WELLBEING]
            if actor.actor_type == ActorType.SERVICE or not needs:
                continue
            scores.append(sum(d.metrics.get_score() for d in needs) / len(needs))
        expected = sum(scores) / len(scores) if scores else 0.0
        assert abs(wellbeing - expected) < 1e-9


def test_current_turn_tracks_run_turn() -> None:
    sim = _small_sim()
    vm = GalaxyViewModel(sim)
    start = vm.current_turn
    sim.run_turn()
    sim.run_turn()
    assert vm.current_turn == start + 2


def test_docked_ship_snapshot_has_zero_progress_and_coincident_endpoints() -> None:
    sim = _small_sim()
    vm = GalaxyViewModel(sim)
    # Fresh ships start docked.
    for snap in vm.ships():
        assert not snap.traveling
        assert snap.progress == 0.0
        assert snap.origin == snap.dest


def test_traveling_ship_snapshot_interpolation_fields() -> None:
    sim = _small_sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    origin, dest = sim.planets[0], sim.planets[1]

    # Force a traveling state directly to avoid fuel and decision dependencies.
    ship.planet = origin
    ship.destination = dest
    ship.status = ShipStatus.TRAVELING
    ship.travel_progress = 0.5

    snap = next(s for s in vm.ships() if s.name == ship.name)
    assert snap.traveling
    assert snap.origin == origin.get_position()
    assert snap.dest == dest.get_position()
    assert snap.origin != snap.dest
    assert abs(snap.progress - 0.5) < 1e-9


def test_planet_detail_snapshot_fields() -> None:
    sim = _small_sim()
    for _ in range(5):
        sim.run_turn()
    vm = GalaxyViewModel(sim)
    planet = sim.planets[0]

    detail = vm.planet_detail(planet.name)
    assert detail is not None
    assert detail.name == planet.name
    assert detail.population > 0
    assert 0.0 <= detail.wellbeing <= 1.0
    assert detail.total_wealth >= 0

    # One row per drive name, scores in range, worst never above the mean.
    assert detail.drives
    for drive in detail.drives:
        assert 0.0 <= drive.worst_score <= drive.mean_score <= 1.0

    # One market row per transportable commodity.
    transportable = [
        c for c in sim.commodity_registry.all_commodities() if c.transportable
    ]
    assert len(detail.market) == len(transportable)
    for row in detail.market:
        assert row.price >= 0
        assert row.scarcity >= 0.0

    # Recent trades are capped and newest-first.
    assert len(detail.recent_trades) <= 8
    turns = [t.turn for t in detail.recent_trades]
    assert turns == sorted(turns, reverse=True)


def test_planet_detail_unknown_name_returns_none() -> None:
    vm = GalaxyViewModel(_small_sim())
    assert vm.planet_detail("Nowhere") is None
    assert vm.ship_detail("Ghost Ship") is None


def test_ship_detail_docked_and_traveling() -> None:
    sim = _small_sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    origin, dest = sim.planets[0], sim.planets[1]
    ship.planet = origin

    docked = vm.ship_detail(ship.name)
    assert docked is not None
    assert origin.name in docked.status
    assert docked.route == ""
    assert docked.cargo_capacity == ship.cargo_capacity

    ship.destination = dest
    ship.status = ShipStatus.TRAVELING
    ship.travel_progress = 0.25

    traveling = vm.ship_detail(ship.name)
    assert traveling is not None
    assert traveling.status == "traveling"
    assert origin.name in traveling.route and dest.name in traveling.route
    assert "25%" in traveling.route


def test_ship_detail_cargo_rows_reflect_hold() -> None:
    sim = _small_sim()
    vm = GalaxyViewModel(sim)
    ship = sim.ships[0]
    food = sim.commodity_registry["food"]
    ship.cargo.add_commodity(food, 7)

    detail = vm.ship_detail(ship.name)
    assert detail is not None
    food_rows = [r for r in detail.cargo if r.commodity_name == food.name]
    assert len(food_rows) == 1
    assert food_rows[0].quantity >= 7
    assert detail.cargo_used >= 7
