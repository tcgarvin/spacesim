"""Per-turn logging for selected actors, attached to the simulation."""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, AbstractSet, Union

from spacesim2.core.actor import Actor
from spacesim2.core.commands import Command
from spacesim2.core.drives.actor_drive import DriveMetrics

if TYPE_CHECKING:
    from spacesim2.core.market import OrderEvent, Transaction
    from spacesim2.core.ship import Ship

# Ships are logged through the same duck-typed interface as actors (name,
# inventory, money, drives), so logger methods accept either.
LoggableActor = Union[Actor, "Ship"]


@dataclass
class ActorTurnLog:
    notes: list[str] = field(default_factory=list)
    metrics: list[DriveMetrics] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    inventory: dict[str, int] = field(default_factory=dict)
    market_status: dict = field(default_factory=dict)


class DataLogger:
    """Collects per-turn logs for a selected subset of actors.

    Only the current turn is retained. Every consumer (headless UI, exporter)
    reads a turn before the next begins, so ``set_turn`` discards the previous
    turn. Memory is O(logged actors), not O(turns x logged actors).
    """

    def __init__(self) -> None:
        # Logs for the current turn only, keyed by actor sim-log key.
        self._actor_turn_logs: dict[str, ActorTurnLog] = defaultdict(ActorTurnLog)
        self.current_turn: int = 0
        self._actors_to_log: dict[str, LoggableActor] = {}

    def set_turn(self, turn: int) -> None:
        """Advance to a new turn, discarding the previous turn's logs."""
        if turn != self.current_turn:
            self._actor_turn_logs.clear()
        self.current_turn = turn

    def _get_actor_sim_log_key(self, actor: LoggableActor) -> str:
        return f"actor-{actor.name}"

    def is_actor_logged(self, actor: LoggableActor) -> bool:
        return actor.name in self._actors_to_log

    def get_all_logged_actors(self) -> list[LoggableActor]:
        return list(self._actors_to_log.values())

    def add_actor_to_log(self, actor: LoggableActor) -> None:
        self._actors_to_log[actor.name] = actor

    def logged_actor_names(self) -> AbstractSet[str]:
        """Live, read-only view of the logged actors' names.

        Markets hold it as their ``order_event_filter``. Being a view, it
        reflects actors added after setup by ``--log-actors``.
        """
        return self._actors_to_log.keys()

    def _turn_log(self, actor: LoggableActor) -> ActorTurnLog:
        """Current turn's log for an actor, created on first use."""
        return self._actor_turn_logs[self._get_actor_sim_log_key(actor)]

    def log_actor_metrics(self, actor: LoggableActor) -> None:
        if not self.is_actor_logged(actor):
            return

        turn_log = self._turn_log(actor)
        turn_log.metrics = [replace(d.metrics) for d in actor.drives]

    def log_actor_note(self, actor: LoggableActor, note: str) -> None:
        if not self.is_actor_logged(actor):
            return

        self._turn_log(actor).notes.append(note)

    def log_actor_command(self, actor: LoggableActor, action: Command) -> None:
        if not self.is_actor_logged(actor):
            return

        self._turn_log(actor).commands.append(action)

    def log_actor_inventory(self, actor: LoggableActor) -> None:
        if not self.is_actor_logged(actor):
            return

        turn_log = self._turn_log(actor)
        turn_log.inventory = {
            commodity.id: quantity
            for commodity, quantity in actor.inventory.commodities.items()
        }

    def log_actor_market_status(self, actor: Actor) -> None:
        if not self.is_actor_logged(actor):
            return

        market_data = actor.get_market_activity_this_turn()

        turn_log = self._turn_log(actor)
        turn_log.market_status = {
            "current_orders": market_data.get("current_orders", {}),
            "events_this_turn": [
                self._serialize_order_event(event)
                for event in market_data.get("events_this_turn", [])
            ],
            "transactions_this_turn": [
                self._serialize_transaction(tx, actor)
                for tx in market_data.get("transactions_this_turn", [])
            ],
        }

    def _serialize_order_event(self, event: "OrderEvent") -> dict:
        """Convert an OrderEvent to a serializable dict."""
        return {
            "order_id": event.order_id,
            "event_type": event.event_type,
            "turn": event.turn,
            "order_details": {
                "commodity": event.order.commodity_type.id,
                "quantity": event.order.quantity,
                "price": event.order.price,
                "type": "buy" if event.order.is_buy else "sell",
            },
        }

    def _serialize_transaction(
        self, transaction: "Transaction", actor: LoggableActor
    ) -> dict:
        """Convert a Transaction to a serializable dict."""
        role = "buyer" if transaction.buyer == actor else "seller"
        counterparty = (
            transaction.seller.name
            if transaction.buyer == actor
            else transaction.buyer.name
        )

        return {
            "commodity": transaction.commodity_type.id,
            "quantity": transaction.quantity,
            "price": transaction.price,
            "total_amount": transaction.total_amount,
            "counterparty": counterparty,
            "role": role,
            "turn": transaction.turn,
        }

    def get_actor_turn_log(
        self, actor: LoggableActor, turn: int | None = None
    ) -> ActorTurnLog:
        """Return the actor's log for the current turn.

        Raises ``ValueError`` if ``turn`` is given and is not the current
        turn; ``set_turn`` has already discarded older turns.
        """
        if turn is not None and turn != self.current_turn:
            raise ValueError(
                f"Only the current turn ({self.current_turn}) is retained; "
                f"requested turn {turn}."
            )
        return self._turn_log(actor)
