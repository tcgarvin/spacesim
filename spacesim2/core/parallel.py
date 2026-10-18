"""Threaded per-planet actor phase.

Runs each turn's actor phase across a shared-memory thread pool, sharding
planets round-robin. Real parallelism needs a free-threaded CPython build,
3.13t or 3.14t: the actor phase is pure-Python bytecode, so under a GIL the
threads serialize and the phase runs at about serial speed. The code is
correct either way, so tests run on stock interpreters.

Threads are safe here because an actor turn touches only

- the actor's own state: inventory, money, skills, drives, brain memory,
- its own planet's market, to place and cancel orders; matching runs later,
  serially,
- read-only registries: commodities, processes, skills,
- the data logger, written under per-actor keys with a single writer each;
  per-key dict ops are atomic on free-threaded CPython,
- the global order-id counter, wrapped in a lock by market._LockedCounter,
- the shared ``Navigator``'s galaxy-wide fuel facts, read only: the fuel
  scan and fallback ask depths read every planet's books, so
  ``_warm_shared_caches`` builds them before the shards start.

Planets are disjoint across shards, so no market is touched by two threads.
Ships, market matching, and export stay serial in ``Simulation.run_turn``.

Ordering differs from the serial path: each planet's actors are shuffled
independently with a per-shard RNG instead of one global shuffle of
``sim.actors``. The only view of other planets during the actor phase is the
navigator's fuel snapshot, which both paths usually carry over from the
previous ship phase, so this changes only the random stream. In-actor
code keeps using the global ``random`` module; free-threaded CPython guards
it with an internal lock, which is a known contention point.
"""

import random
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, List

from spacesim2.core.navigation import get_navigator

if TYPE_CHECKING:
    from spacesim2.core.planet import Planet
    from spacesim2.core.simulation import Simulation


def _warm_shared_caches(sim: "Simulation") -> None:
    """Build lazily filled shared caches before threads race to fill them.

    The registry indexes come out complete whichever thread builds them, so
    warming them only saves duplicate work. The navigator's fuel scan is
    different: it reads every planet's live order book and fills other
    planets' quote caches, so a worker building it would read books that
    their owner threads are mutating and could leave a stale best quote in a
    market it does not own. Built here, every actor sees the phase-start
    snapshot. When no producer sells fuel anywhere, spaceport operators also
    read per-planet ask depth, which is warmed for the same reason.
    """
    for commodity in sim.commodity_registry.all_commodities():
        sim.process_registry.get_processes_producing(commodity)
        sim.process_registry.get_processes_consuming(commodity)
    navigator = get_navigator(sim)
    navigator.fuel_value_reference()
    if not navigator.producer_fuel_ask_planets():
        for planet, _ in navigator.fuel_ask_planets():
            navigator.fuel_ask_depth_at(planet)


def _run_shard(planets: List["Planet"], rng: random.Random) -> None:
    """Run the actor phase for one shard's planets, in shuffled actor order."""
    for planet in planets:
        actors = list(planet.actors)
        rng.shuffle(actors)
        for actor in actors:
            actor.take_turn()


def _get_pool(sim: "Simulation", workers: int) -> ThreadPoolExecutor:
    """The sim's persistent actor-phase pool, rebuilt if the size changed."""
    pool = sim._actor_phase_pool
    if pool is None or sim._actor_phase_pool_size != workers:
        if pool is not None:
            pool.shutdown(wait=True)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="actor-phase")
        sim._actor_phase_pool = pool
        sim._actor_phase_pool_size = workers
    return pool


def run_actor_phase_threaded(sim: "Simulation", workers: int) -> None:
    """Run every planet's actor turns across a thread pool.

    Must be called where run_turn would otherwise run its serial actor
    loop: after set_current_turn, before the ship phase.
    """
    if workers <= 1:
        raise ValueError("run_actor_phase_threaded requires workers >= 2")
    # Shards iterate planet.actors; an actor registered on the sim but not
    # on its planet would never act again.
    if sum(len(p.actors) for p in sim.planets) != len(sim.actors):
        raise RuntimeError(
            "planet.actors does not cover sim.actors; cannot shard the "
            "actor phase by planet"
        )
    _warm_shared_caches(sim)
    workers = min(workers, len(sim.planets))
    shards = [sim.planets[k::workers] for k in range(workers)]
    rngs = [random.Random(random.random()) for _ in shards]
    pool = _get_pool(sim, workers)
    futures = [pool.submit(_run_shard, shard, rng) for shard, rng in zip(shards, rngs)]
    # result() re-raises the first shard failure.
    for future in futures:
        future.result()
