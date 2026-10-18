"""Tests for the threaded per-planet actor phase in core/parallel.py.

The threaded path also runs correctly, without speedup, on GIL-enabled
interpreters, so these tests run everywhere.
"""

import threading

import pytest

from spacesim2.core.navigation import Navigator, get_navigator
from spacesim2.core.parallel import run_actor_phase_threaded
from spacesim2.core.simulation import Simulation


def _setup(workers: int) -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=4, num_regular_actors=10, num_market_makers=1, num_ships=1
    )
    sim.parallel_workers = workers
    return sim


def _assert_books_consistent(sim: Simulation) -> None:
    for planet in sim.planets:
        market = planet.market
        book_ids = {
            order.order_id
            for book in (market.buy_orders, market.sell_orders)
            for orders in book.values()
            for order in orders
        }
        assert book_ids == set(market.orders_by_id)


def test_threaded_smoke_books_stay_consistent() -> None:
    sim = _setup(workers=2)
    for _ in range(10):
        sim.run_turn()
    _assert_books_consistent(sim)


def test_order_ids_unique_across_markets() -> None:
    sim = _setup(workers=4)
    for _ in range(5):
        sim.run_turn()
    seen: set[str] = set()
    for planet in sim.planets:
        ids = set(planet.market.orders_by_id)
        assert not (ids & seen)
        seen |= ids


def test_threaded_matches_serial_macro_shape() -> None:
    """A threaded run reaches the same coarse macro state as a serial one.

    The sim is stochastic, so only coarse properties are asserted: everyone
    acted, total money is positive, and markets saw orders.
    """
    sim = _setup(workers=2)
    for _ in range(10):
        sim.run_turn()
    acted = sum(1 for a in sim.actors if a.last_action)
    assert acted == len(sim.actors)
    assert sum(a.money for a in sim.actors) > 0
    assert any(p.market.orders_by_id for p in sim.planets)


def test_worker_count_capped_at_planet_count() -> None:
    sim = _setup(workers=16)
    sim.run_turn()  # 4 planets; must not fail with 16 requested workers
    _assert_books_consistent(sim)


def test_uncovered_actor_raises() -> None:
    sim = _setup(workers=2)
    sim.planets[0].actors.pop()
    with pytest.raises(RuntimeError, match="does not cover"):
        run_actor_phase_threaded(sim, 2)


def test_single_worker_rejected() -> None:
    sim = _setup(workers=1)
    with pytest.raises(ValueError):
        run_actor_phase_threaded(sim, 1)


def test_fuel_scan_is_never_built_on_a_worker_thread(monkeypatch) -> None:
    """The galaxy-wide fuel scan reads every planet's books, so a worker
    building it would race the owner threads of those markets."""
    builders: list[str] = []
    original = Navigator._fuel_market_scan

    def recording_scan(self: Navigator):
        if self._fuel_scan is None:
            builders.append(threading.current_thread().name)
        return original(self)

    monkeypatch.setattr(Navigator, "_fuel_market_scan", recording_scan)
    sim = _setup(workers=2)
    navigator = get_navigator(sim)
    for _ in range(5):
        # Forced refresh: the scan is missing when the actor phase starts.
        navigator.refresh_market_facts()
        sim.run_turn()
    assert builders, "wrapper never saw a scan build"
    assert all(name == threading.main_thread().name for name in builders)
