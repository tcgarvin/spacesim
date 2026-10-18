import pytest

from spacesim2.core.actor import ActorType
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.simulation import Simulation

from .helpers import get_actor


@pytest.fixture
def food_commodity():
    """Food commodity."""
    return CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )


def test_actor_government_work() -> None:
    """GovernmentWorkCommand increases the actor's money."""
    mock_sim = type(
        "MockSimulation", (object,), {"commodity_registry": CommodityRegistry()}
    )()
    actor = get_actor("Test Actor", mock_sim, initial_money=0)
    initial_money = actor.money

    from spacesim2.core.commands import GovernmentWorkCommand

    command = GovernmentWorkCommand()
    command.execute(actor)
    assert actor.money > initial_money


def test_planet_add_actor() -> None:
    """add_actor links the actor and planet both ways."""
    market = Market()
    planet = Planet("Test Planet", market)
    mock_sim = type(
        "MockSimulation", (object,), {"commodity_registry": CommodityRegistry()}
    )()
    actor = get_actor("Test Actor", mock_sim)

    planet.add_actor(actor)

    assert actor in planet.actors
    assert actor.planet == planet


def test_simulation_setup() -> None:
    """Default setup_simple: 2 planets, 4 regulars, 1 maker, 2 operators, 1 company each."""
    sim = Simulation()
    sim.setup_simple()

    assert len(sim.planets) == 2

    # (4 regular + 1 market maker + 2 spaceport operators + 1 trading
    # company) * 2 planets
    assert len(sim.actors) == 16

    regular_count = 0
    service_count = 0
    for actor in sim.actors:
        if actor.actor_type == ActorType.REGULAR:
            regular_count += 1
        elif actor.actor_type == ActorType.SERVICE:
            service_count += 1

    assert regular_count == 8
    assert service_count == 8

    planet1_actors = 0
    planet2_actors = 0
    for actor in sim.actors:
        assert actor.planet is not None
        if actor.planet.name == sim.planets[0].name:
            planet1_actors += 1
        elif actor.planet.name == sim.planets[1].name:
            planet2_actors += 1

    assert planet1_actors == 8
    assert planet2_actors == 8

    assert sim.planets[0].market is not None
    assert sim.planets[1].market is not None


class SimulationTestHelper:
    """Builds a one-planet, one-actor simulation."""

    @staticmethod
    def setup_test_simulation():
        """Simulation whose single actor always does government work."""
        sim = Simulation()

        market = Market()
        planet = Planet("TestPlanet", market)
        sim.planets.append(planet)

        market = Market()
        planet.market = market

        sim.commodity_registry = CommodityRegistry()
        food_commodity = CommodityDefinition(
            id="food",
            name="Food",
            transportable=True,
            description="Basic nourishment required by actors.",
        )
        fuel_commodity = CommodityDefinition(
            id="nova_fuel",
            name="Nova Fuel",
            transportable=True,
            description="High-energy fuel for starships.",
        )
        sim.commodity_registry._commodities["food"] = food_commodity
        sim.commodity_registry._commodities["nova_fuel"] = fuel_commodity
        market.commodity_registry = sim.commodity_registry

        actor = get_actor(
            name="TestWorker",
            sim=sim,
            planet=planet,
            initial_money=0,
            actor_type=ActorType.REGULAR,
        )

        from spacesim2.core.commands import GovernmentWorkCommand

        actor.brain.decide_economic_action = lambda _: GovernmentWorkCommand()

        sim.actors.append(actor)
        planet.add_actor(actor)

        return sim


def test_simulation_run_turn() -> None:
    """run_turn advances the turn counter and executes the actor's action."""
    sim = SimulationTestHelper.setup_test_simulation()

    assert len(sim.actors) == 1
    actor = sim.actors[0]
    assert actor.money == 0

    initial_turn = sim.current_turn

    from spacesim2.core.commands import GovernmentWorkCommand

    command = GovernmentWorkCommand()
    command.execute(actor)
    assert actor.money == 10

    actor.money = 0

    sim.run_turn()

    assert sim.current_turn == initial_turn + 1

    # The overridden decide_economic_action earns one government wage per turn.
    assert actor.money == 10
