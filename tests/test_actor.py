from spacesim2.core.actor import Actor, ActorType

from .helpers import get_actor


def test_actor_initialization(mock_sim, mock_brain) -> None:
    """Constructor sets name, money, inventory, and type."""
    actor = Actor(
        "Test Actor", mock_sim, ActorType.REGULAR, [], mock_brain, initial_money=50
    )
    assert actor.name == "Test Actor"
    assert actor.money == 50
    assert actor.inventory is not None
    assert actor.actor_type == ActorType.REGULAR


def test_actor_government_work() -> None:
    """GovernmentWorkCommand pays the actor the government wage."""
    actor = get_actor(initial_money=0)

    from spacesim2.core.commands import GovernmentWorkCommand

    command = GovernmentWorkCommand()
    command.execute(actor)

    assert actor.money == 10  # Government wage
