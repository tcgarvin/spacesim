"""Actor conservation: no actor is created or destroyed after setup.

An actor only moves between ``sim.actors`` and a ship's hold (a LOADED
passage contract), never out of existence, so ``sim.population()`` must be
the same value every turn. See ``Simulation.population()`` and
``Simulation.actors_aboard()`` in ``core/simulation.py``.
"""

import pytest

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core.contracts import ContractStatus, PassengerPayload


def _actors_aboard_by_scan(sim) -> int:
    """Independent reimplementation of ``actors_aboard`` for cross-checking.

    Scans every ship's loaded passenger contracts directly, rather than
    calling ``sim.actors_aboard()``, so the test does not just check the
    method against itself.
    """
    count = 0
    for ship in sim.ships:
        for contract in ship.contracts:
            if contract.status is ContractStatus.LOADED and isinstance(
                contract.payload, PassengerPayload
            ):
                assert contract.payload.actor.in_transit is True
                count += 1
    return count


def test_population_conserved_across_turns() -> None:
    """Population is invariant every turn, including while passengers fly."""
    sim = create_and_setup_simulation(
        planets=6, actors=6, makers=1, operators=1, ships=2
    )
    initial_population = sim.population()
    assert initial_population > 0

    for _ in range(150):
        sim.run_turn()
        assert sim.population() == initial_population
        assert sim.actors_aboard() == _actors_aboard_by_scan(sim)


def test_run_turn_raises_when_an_actor_vanishes_from_the_roster() -> None:
    """Removing an actor behind the sim's back trips the conservation check."""
    sim = create_and_setup_simulation(
        planets=2, actors=4, makers=1, operators=1, ships=1
    )
    sim.run_turn()  # records expected_population
    assert sim.expected_population is not None

    victim = sim.actors[0]
    planet = victim.planet
    assert planet is not None
    sim.actors.remove(victim)
    planet.remove_actor(victim)

    with pytest.raises(RuntimeError, match="actor conservation violated"):
        sim.run_turn()
