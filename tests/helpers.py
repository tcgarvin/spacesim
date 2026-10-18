from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.commands import GovernmentWorkCommand
from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.migration import NO_MIGRATION


def _get_mock_sim():
    return type(
        "MockSimulation", (object,), {"commodity_registry": CommodityRegistry()}
    )()


def _get_mock_brain():
    return type(
        "MockBrain",
        (object,),
        {
            "decide_economic_action": lambda _: GovernmentWorkCommand(),
            "decide_market_actions": lambda _: [],
            "decide_migration": lambda _: NO_MIGRATION,
        },
    )


def get_actor(
    name="DefaultTestActor",
    sim=None,
    actor_type=ActorType.REGULAR,
    brain=None,
    planet=None,
    initial_money=50,
    initial_skills={},
) -> Actor:
    if sim is None:
        sim = _get_mock_sim()

    if brain is None:
        brain = _get_mock_brain()

    return Actor(
        name,
        sim,
        actor_type,
        [],  # drives
        brain,
        planet,
        initial_money,
        initial_skills,
    )
