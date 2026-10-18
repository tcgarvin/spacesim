import enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, cast

from spacesim2.core.commands import (
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
    prune_unchanged_order_commands,
)
from spacesim2.core.commodity import Inventory
from spacesim2.core.land import Land
from spacesim2.core.migration import NO_MIGRATION, MigrationDecision
from spacesim2.core.planet import Planet

if TYPE_CHECKING:
    from spacesim2.core.actor_brain import ActorBrain
    from spacesim2.core.contracts import Contract
    from spacesim2.core.drives.actor_drive import ActorDrive
    from spacesim2.core.process import ProcessDefinition
    from spacesim2.core.ship import Ship
    from spacesim2.core.simulation import Simulation


class ActorType(enum.Enum):
    """Types of actors in the simulation."""

    REGULAR = "regular"
    SERVICE = "service"


class Actor:
    """Represents an economic actor in the simulation."""

    # Class-level defaults so Mock(spec=Actor) sees the attributes; instances
    # shadow them.
    skills_version: int = 0
    land: Land = Land.default()
    claims_land: bool = False

    def __init__(
        self,
        name: str,
        sim: "Simulation",
        actor_type: ActorType,
        drives: List["ActorDrive"],
        brain: "ActorBrain",
        planet: Optional[Planet] = None,
        initial_money: int = 50,
        initial_skills: Optional[Dict[str, float]] = None,
        tastes: Mapping[str, float] = MappingProxyType({}),
    ) -> None:
        self.name = name
        # Fixed for life. Prosperity drives read it at construction; kept
        # here for later export and UI use, not yet consumed by either.
        self.tastes: Dict[str, float] = dict(tastes)
        # No implicit capital injection by actor type: every actor starts with
        # exactly what the caller passed. Market makers are sized explicitly at
        # setup; spaceport operators bootstrap on the government wage.
        self.money = initial_money
        self.reserved_money = 0  # Money reserved for market orders
        self.planet = planet
        self.inventory = Inventory()
        self.actor_type = actor_type
        # Extraction coefficients. Regular actors claim a land from the
        # planet's pool in Planet.add_actor; service actors never extract and
        # keep the penalty-free default. See core/land.py.
        self.claims_land: bool = actor_type is ActorType.REGULAR
        self.land: Land = Land.default()
        self.active_orders: Dict[
            str, str
        ] = {}  # Track active order IDs and their types
        self.food_consumed_this_turn = (
            False  # Track if actor has consumed food this turn
        )
        self.last_action = "None"
        self.last_market_action = "None"
        self.sim = sim
        self.drives = drives

        self.skills: Dict[str, float] = {}
        # Bumped whenever a skill rating is set. With Inventory.version it
        # lets brain caches detect actor-state changes cheaply. See
        # actor_brain.BrainCache.
        self.skills_version: int = 0

        if initial_skills:
            for skill_id, rating in initial_skills.items():
                self.skills[skill_id] = rating

        self.brain = brain
        self.last_market_check_turn: int = (
            0  # Track when actor last checked market status
        )
        # This turn's migration decision, read and cleared by
        # core/migration.run_migration_phase. Only land-claiming actors are
        # ever asked; service actors stay put.
        self.migration_request: MigrationDecision = NO_MIGRATION
        # The last passage contract posted for this actor, if any. Never
        # cleared: its status is what says whether the passage is still
        # live, so the migration phase reads the one live contract off it
        # and the brain reads an expiry off the same field.
        self.passage_contract: Optional["Contract"] = None
        # True between departure and arrival. An in-transit actor is in
        # neither ``sim.actors`` nor any ``planet.actors``, so this flag, not
        # ``planet``, is the authoritative answer to "is this actor placed?".
        # ``planet`` keeps pointing at the origin until the actor lands, so
        # no code has to handle a planet-less actor.
        self.in_transit: bool = False

    def get_skill_rating(self, skill_id: str) -> float:
        """Rating for a skill; 0.5 (unskilled) if the actor lacks it."""
        return self.skills.get(skill_id, 0.5)

    def set_skill_rating(self, skill_id: str, rating: float) -> None:
        """Set a skill rating, clamped to [0.5, 3.0]."""
        self.skills[skill_id] = max(0.5, min(3.0, rating))
        self.skills_version += 1

    def improve_skill(self, skill_id: str, amount: float) -> None:
        """Raise a skill rating by ``amount``."""
        current_rating = self.get_skill_rating(skill_id)
        self.set_skill_rating(skill_id, current_rating + amount)

    def receive_remittance(self, ship: "Ship", amount: int) -> None:
        """Note a ship surplus just credited to ``self.money``.

        Satisfies the ``ShipOwner`` protocol in core/ship.py. The money has
        already moved; this is the hook a brain uses to keep its own books.
        Called from the serial ship phase.
        """
        self.brain.on_remittance(self, ship, amount)

    def take_turn(self) -> None:
        """Take one turn: an economic action, market actions, then drive ticks."""
        economic_command = self.brain.decide_economic_action(self)
        if economic_command:
            economic_command.execute(self)
            self.sim.data_logger.log_actor_command(self, economic_command)

        # Brains cancel and repost their whole book; pruning drops pairs that
        # would recreate an identical order so unchanged quotes keep resting.
        market_commands = self.brain.decide_market_actions(self)
        if self.planet and market_commands:
            market_commands = prune_unchanged_order_commands(
                self.planet.market, self, market_commands
            )
        market_actions = []
        for command in market_commands:
            success = command.execute(self)
            self.sim.data_logger.log_actor_command(self, command)
            # Only buy/sell orders go in the summary. Exact-class checks:
            # isinstance on these ABC-derived commands goes through
            # abc.__instancecheck__, which costs at this call frequency.
            command_class = command.__class__
            is_buy = command_class is PlaceBuyOrderCommand
            if success and (is_buy or command_class is PlaceSellOrderCommand):
                order_command = cast(
                    "PlaceBuyOrderCommand | PlaceSellOrderCommand", command
                )
                action_type = "Buy" if is_buy else "Sell"
                commodity_name = order_command.commodity_type.id
                market_actions.append(
                    f"{action_type} {order_command.quantity} "
                    f"{commodity_name} at {order_command.price}"
                )

        if market_actions:
            self.last_market_action = "; ".join(market_actions)
        else:
            self.last_market_action = "No market actions"

        for drive in self.drives:
            drive.tick(self)

        # Only land-claiming actors migrate. Service actors are tied to the
        # planet they serve, so they are never asked.
        if self.claims_land:
            self.migration_request = self.brain.decide_migration(self)

        self.sim.data_logger.log_actor_metrics(self)
        self.sim.data_logger.log_actor_inventory(self)
        self.sim.data_logger.log_actor_market_status(self)

    def can_execute_process(self, process_id: str) -> bool:
        """Whether the actor could run the process now, without running it."""
        process = self.sim.process_registry.get_process(process_id)
        if not process:
            return False
        return self.can_execute(process)

    def can_execute(self, process: "ProcessDefinition") -> bool:
        """Like ``can_execute_process`` for callers holding the definition.

        Hot paths scan the whole registry, so they skip the id lookup.
        """
        # process.requirements flattens inputs at their quantities plus tools
        # and facilities at 1, in check order: one pass, one method lookup.
        has_quantity = self.inventory.has_quantity
        for commodity, quantity in process.requirements:
            if not has_quantity(commodity, quantity):
                return False
        return True

    def get_market_activity_since_last_check(self) -> Dict:
        """Open orders plus events and transactions since the last check."""
        if not self.planet:
            return {}

        market = self.planet.market
        current_orders = market.get_actor_current_orders(self)
        events = market.get_actor_order_events(
            self, since_turn=self.last_market_check_turn
        )
        transactions = market.get_actor_transactions_range(
            self, since_turn=self.last_market_check_turn
        )

        self.last_market_check_turn = self.sim.current_turn

        return {
            "current_orders": current_orders,
            "events_since_last_check": events,
            "transactions_since_last_check": transactions,
        }

    def get_market_activity_this_turn(self) -> Dict:
        """Open orders plus this turn's events and transactions."""
        if not self.planet:
            return {}

        market = self.planet.market
        return {
            "current_orders": market.get_actor_current_orders(self),
            "events_this_turn": market.get_actor_order_events(
                self, since_turn=self.sim.current_turn
            ),
            "transactions_this_turn": market.get_actor_transactions_range(
                self, since_turn=self.sim.current_turn
            ),
        }

    def get_market_activity_last_n_turns(self, n: int) -> Dict:
        """Open orders plus events and transactions from the last ``n`` turns."""
        if not self.planet:
            return {}

        market = self.planet.market
        since_turn = max(0, self.sim.current_turn - n)
        return {
            "current_orders": market.get_actor_current_orders(self),
            "events_last_n_turns": market.get_actor_order_events(
                self, since_turn=since_turn
            ),
            "transactions_last_n_turns": market.get_actor_transactions_range(
                self, since_turn=since_turn
            ),
        }

    def __repr__(self) -> str:
        return f"Actor(name={self.name})"
