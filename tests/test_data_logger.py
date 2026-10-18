"""Tests for DataLogger's bounded-memory, current-turn-only behavior."""

from unittest.mock import Mock

import pytest

from spacesim2.core.data_logger import DataLogger


def _fake_actor(name: str) -> Mock:
    actor = Mock()
    actor.name = name
    return actor


def test_only_current_turn_is_retained() -> None:
    """Advancing the turn discards the previous turn's logs."""
    logger = DataLogger()
    actor = _fake_actor("Alice")
    logger.add_actor_to_log(actor)

    logger.set_turn(1)
    logger.log_actor_note(actor, "turn one note")
    assert logger.get_actor_turn_log(actor).notes == ["turn one note"]

    logger.set_turn(2)
    assert logger.get_actor_turn_log(actor).notes == []


def test_memory_bounded_across_many_turns() -> None:
    """Internal storage stays O(logged actors), not O(turns x actors)."""
    logger = DataLogger()
    actors = [_fake_actor(f"Actor-{i}") for i in range(20)]
    for actor in actors:
        logger.add_actor_to_log(actor)

    for turn in range(1, 301):
        logger.set_turn(turn)
        for actor in actors:
            logger.log_actor_note(actor, f"note at turn {turn}")

    assert len(logger._actor_turn_logs) == len(actors)


def test_set_turn_same_turn_keeps_logs() -> None:
    """Re-setting the same turn keeps that turn's logs."""
    logger = DataLogger()
    actor = _fake_actor("Alice")
    logger.add_actor_to_log(actor)

    logger.set_turn(5)
    logger.log_actor_note(actor, "kept")
    logger.set_turn(5)
    assert logger.get_actor_turn_log(actor).notes == ["kept"]


def test_requesting_past_turn_raises() -> None:
    logger = DataLogger()
    actor = _fake_actor("Alice")
    logger.add_actor_to_log(actor)
    logger.set_turn(3)

    with pytest.raises(ValueError, match="turn 2"):
        logger.get_actor_turn_log(actor, turn=2)

    # Explicitly naming the current turn is fine.
    assert logger.get_actor_turn_log(actor, turn=3).notes == []


def test_market_status_skips_market_queries_for_unlogged_actor() -> None:
    """Unlogged actors must not trigger per-actor market queries."""
    logger = DataLogger()
    actor = _fake_actor("Unlogged")

    logger.log_actor_market_status(actor)

    actor.get_market_activity_this_turn.assert_not_called()
