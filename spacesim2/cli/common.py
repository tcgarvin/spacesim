"""Common utilities for CLI commands."""

import random

from spacesim2.core.actor import ActorType
from spacesim2.core.galaxy import DEFAULT_ARMS, DEFAULT_LANE_DENSITY
from spacesim2.core.simulation import DEFAULT_LANDS_PER_PLANET, Simulation


def create_and_setup_simulation(
    planets: int,
    actors: int,
    makers: int,
    operators: int = 2,
    ships: int = 1,
    arms: int = DEFAULT_ARMS,
    lane_density: float = DEFAULT_LANE_DENSITY,
    lands_per_planet: int = DEFAULT_LANDS_PER_PLANET,
) -> Simulation:
    """Create a simulation with the standard setup.

    Args:
        actors: Regular actors per planet.
        makers: Market makers per planet.
        operators: Spaceport operators per planet.
        arms: Spiral arms in the galaxy layout.
        lane_density: Fraction of optional local star lanes kept, 0..1.
        lands_per_planet: Size of each planet's land pool; must exceed
            ``actors`` so migrants have somewhere to settle.
    """
    sim = Simulation()
    sim.setup_simple(
        num_planets=planets,
        num_regular_actors=actors,
        num_market_makers=makers,
        num_spaceport_operators=operators,
        num_ships=ships,
        arms=arms,
        lane_density=lane_density,
        lands_per_planet=lands_per_planet,
    )
    return sim


def configure_actor_logging(sim: Simulation, spec: str = "1") -> int:
    """Select actors for the detailed logging pipeline.

    Args:
        spec: "all" for every actor and ship, an integer N for a random
            sample of N non-market-maker actors, or an actor name.

    Returns:
        Number of actors configured for logging.

    Raises:
        ValueError: If spec names an actor that does not exist.
    """
    if spec == "all":
        for actor in sim.actors:
            sim.data_logger.add_actor_to_log(actor)
        for ship in sim.ships:
            sim.data_logger.add_actor_to_log(ship)
        return len(sim.actors) + len(sim.ships)

    if spec.isdigit():
        eligible = [a for a in sim.actors if a.actor_type != ActorType.SERVICE]
        sample = random.sample(eligible, min(int(spec), len(eligible)))
        for actor in sample:
            sim.data_logger.add_actor_to_log(actor)
        return len(sample)

    for actor in sim.actors:
        if actor.name == spec:
            sim.data_logger.add_actor_to_log(actor)
            return 1
    raise ValueError(f"Actor '{spec}' not found")
