"""Every actor's ActorType must agree with what its brain implies.

ActorType is a coarse "population vs infrastructure" split (REGULAR vs
SERVICE); this test pins the mapping from brain class to expected
ActorType so future brains are added deliberately rather than silently
falling on the wrong side of the split.
"""

from spacesim.core.actor import ActorType
from spacesim.core.brains.colonist import ColonistBrain
from spacesim.core.brains.industrialist import IndustrialistBrain
from spacesim.core.brains.market_maker_2 import MarketMakerBrain
from spacesim.core.brains.spaceport_operator import SpaceportOperatorBrain
from spacesim.core.brains.trading_company import TradingCompanyBrain
from spacesim.core.simulation import Simulation

# Brain class -> expected ActorType. Add new brains here as they're introduced.
_EXPECTED_ACTOR_TYPE: dict[type, ActorType] = {
    MarketMakerBrain: ActorType.SERVICE,
    SpaceportOperatorBrain: ActorType.SERVICE,
    TradingCompanyBrain: ActorType.SERVICE,
    ColonistBrain: ActorType.REGULAR,
    IndustrialistBrain: ActorType.REGULAR,
}


def test_actor_type_matches_brain() -> None:
    sim = Simulation()
    sim.setup_simple(num_planets=2, num_regular_actors=4, num_market_makers=1)

    assert sim.actors, "expected setup_simple to create actors"

    for actor in sim.actors:
        brain_class = type(actor.brain)
        expected = _EXPECTED_ACTOR_TYPE.get(brain_class)
        assert expected is not None, (
            f"no expected ActorType registered for brain {brain_class.__name__}; "
            "add it to _EXPECTED_ACTOR_TYPE"
        )
        assert actor.actor_type == expected, (
            f"actor {actor.name} has brain {brain_class.__name__} "
            f"but actor_type {actor.actor_type}, expected {expected}"
        )
