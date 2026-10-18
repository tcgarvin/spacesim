import random
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Tuple, cast

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.commodity import CommodityDefinition
    from spacesim2.core.market import Market, MarketParticipant


class Command(ABC):
    """Base class for all commands."""

    @abstractmethod
    def execute(self, actor: "Actor") -> bool:
        """Execute the command on the actor; True on success."""
        pass


class EconomicCommand(Command):
    """Base class for economic action commands."""

    pass


class MarketCommand(Command):
    """Base class for market action commands."""

    pass


# Chance per successful run that one held capital good breaks and is removed.
# Mean life 200 runs, five times a tool's.
CAPITAL_BREAK_PROBABILITY = 0.005


class ProcessCommand(EconomicCommand):
    """Command to execute a production process."""

    def __init__(self, process_id: str) -> None:
        self.process_id = process_id

    def __repr__(self) -> str:
        return f"ProcessCommand(process_id={self.process_id!r})"

    def execute(self, actor: "Actor") -> bool:
        """Run the process; True on success.

        Fails without side effects when requirements are missing, an upkeep
        draw hits a good the actor does not hold, or the skill check fails.
        Tools may break after a successful run.
        """
        process = actor.sim.process_registry.get_process(self.process_id)
        if not process:
            return False

        for commodity, quantity in process.inputs.items():
            if not actor.inventory.has_quantity(commodity, quantity):
                return False

        for tool in process.tools_required:
            if not actor.inventory.has_quantity(tool, 1):
                return False

        for facility in process.facilities_required:
            if not actor.inventory.has_quantity(facility, 1):
                return False

        # Facility upkeep: each entry is rolled once per run. A hit demands
        # one unit on hand, and a missing unit fails the run the same way a
        # missing tool does, before anything is consumed.
        upkeep_hits = [
            commodity
            for commodity, probability in process.upkeep.items()
            if random.random() < probability
        ]
        for commodity in upkeep_hits:
            if not actor.inventory.has_quantity(commodity, 1):
                return False

        success = True
        multiplier = 1

        if process.relevant_skills:
            skill_ratings = []
            for skill_id in process.relevant_skills:
                skill_rating = actor.get_skill_rating(skill_id)
                skill_ratings.append(skill_rating)

            from spacesim2.core.skill import SkillCheck

            combined_rating = SkillCheck.get_combined_skill_rating(skill_ratings)

            success = SkillCheck.success_check(combined_rating)

            if success and SkillCheck.multiplier_check(combined_rating):
                multiplier = 2

        if not success:
            actor.last_action = f"Failed process: {process.name}"
            return False

        planet_multiplier = 1.0
        if process.resource_attribute:
            # The actor's own land, not the planet mean. See core/land.py.
            availability = actor.land.get_availability(
                process.resource_attribute.commodity
            )

            if process.resource_attribute.effect == "success":
                # Low availability fails the whole process.
                if random.random() > availability:
                    actor.last_action = (
                        f"Failed {process.name}: insufficient planetary resources"
                    )
                    return False
            elif process.resource_attribute.effect == "output":
                # Low availability scales output down.
                planet_multiplier = availability

        # The skill multiplier buys labor, not materials: a doubled run
        # consumes doubled inputs, so it needs them on hand. Otherwise the
        # run proceeds at single scale. Removal after that check cannot
        # fail; a False here would mean output created from nothing.
        if multiplier > 1 and not all(
            actor.inventory.has_quantity(commodity, quantity * multiplier)
            for commodity, quantity in process.inputs.items()
        ):
            multiplier = 1

        # Productive capital: each capital good the actor holds adds its
        # output bonus. Bonuses sum, and the goods are not consumed by the
        # run, only occasionally broken below.
        capital_applied = [
            commodity
            for commodity in process.capital
            if actor.inventory.has_quantity(commodity, 1)
        ]
        capital_multiplier = 1.0 + sum(
            process.capital[commodity] for commodity in capital_applied
        )

        # Inputs scale with the skill multiplier; outputs with the skill
        # multiplier (1 or 2), the planet multiplier (0-1) and the capital
        # bonus, min 1 unit.
        for commodity, quantity in process.inputs.items():
            if not actor.inventory.remove_commodity(commodity, quantity * multiplier):
                raise RuntimeError(
                    f"{process.id}: could not consume {quantity * multiplier} "
                    f"{commodity.id} after the availability check passed"
                )

        # Upkeep is a flat per-run draw, never scaled by the skill multiplier.
        for commodity in upkeep_hits:
            if not actor.inventory.remove_commodity(commodity, 1):
                raise RuntimeError(
                    f"Upkeep {commodity} vanished mid-run of process "
                    f"{process.id!r} for actor {actor.name}"
                )

        for commodity, quantity in process.outputs.items():
            output_quantity = max(
                1,
                round(quantity * multiplier * planet_multiplier * capital_multiplier),
            )
            actor.inventory.add_commodity(commodity, output_quantity)

        if process.relevant_skills:
            # Skill gain is 0.01, plus 0.02 when the multiplier applied.
            skill_improvement = 0.01 + (
                0.02 * (multiplier - 1)
            )  # More improvement with multiplier
            for skill_id in process.relevant_skills:
                actor.improve_skill(skill_id, skill_improvement)

        # Each tool has a 1% chance to break after a successful run.
        TOOL_BREAK_PROBABILITY = 0.01
        for tool in process.tools_required:
            if random.random() < TOOL_BREAK_PROBABILITY:
                actor.inventory.remove_commodity(tool, 1)
                if actor.sim.data_logger:
                    # Tools may be definitions or plain string ids.
                    tool_name = tool.name if hasattr(tool, "name") else str(tool)
                    actor.sim.data_logger.log_actor_note(
                        actor, f"Tool broke: {tool_name}"
                    )

        # Capital wears out far more slowly than a tool: mean life 200 runs.
        for commodity in capital_applied:
            if random.random() < CAPITAL_BREAK_PROBABILITY:
                actor.inventory.remove_commodity(commodity, 1)
                if actor.sim.data_logger:
                    actor.sim.data_logger.log_actor_note(
                        actor, f"Capital broke: {commodity.name}"
                    )

        modifiers = []
        if multiplier > 1:
            modifiers.append(f"skill ×{multiplier}")
        if planet_multiplier < 1.0:
            modifiers.append(f"planet {planet_multiplier:.0%}")
        if capital_multiplier > 1.0:
            modifiers.append(f"capital +{capital_multiplier - 1.0:.0%}")
        if upkeep_hits:
            modifiers.append(
                f"upkeep {', '.join(str(commodity) for commodity in upkeep_hits)}"
            )
        modifier_text = f" ({', '.join(modifiers)})" if modifiers else ""
        actor.last_action = f"Executed process: {process.name}{modifier_text}"
        return True


GOVERNMENT_WAGE = 10


class GovernmentWorkCommand(EconomicCommand):
    """Command to perform government work for a wage."""

    def __repr__(self) -> str:
        return f"GovernmentWorkCommand(wage={GOVERNMENT_WAGE})"

    def execute(self, actor: "Actor") -> bool:
        """Perform government work to earn a fixed wage."""
        actor.money += GOVERNMENT_WAGE
        actor.last_action = f"Government work for {GOVERNMENT_WAGE} credits"
        return True


class CancelOrderCommand(MarketCommand):
    """Command to cancel a market order."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id

    def __repr__(self) -> str:
        return f"CancelOrderCommand(order_id={self.order_id!r})"

    def execute(self, actor: "Actor") -> bool:
        """Cancel a market order."""
        if not actor.planet:
            return False

        return actor.planet.market.cancel_order(self.order_id)


class PlaceBuyOrderCommand(MarketCommand):
    """Command to place a buy order on the market."""

    def __init__(
        self, commodity_type: "CommodityDefinition", quantity: int, price: int
    ) -> None:
        self.commodity_type = commodity_type
        self.quantity = quantity
        self.price = price

    def __repr__(self) -> str:
        return f"PlaceBuyOrderCommand(commodity={self.commodity_type.id!r}, quantity={self.quantity}, price={self.price})"

    def execute(self, actor: "Actor") -> bool:
        """Place a buy order on the market."""
        if not actor.planet:
            return False

        order_id = actor.planet.market.place_buy_order(
            actor, self.commodity_type, self.quantity, self.price
        )

        if order_id:
            commodity_name = self.commodity_type.id
            actor.active_orders[order_id] = f"buy {commodity_name}"
            return True

        return False


class PlaceSellOrderCommand(MarketCommand):
    """Command to place a sell order on the market."""

    def __init__(
        self, commodity_type: "CommodityDefinition", quantity: int, price: int
    ) -> None:
        self.commodity_type = commodity_type
        self.quantity = quantity
        self.price = price

    def __repr__(self) -> str:
        return f"PlaceSellOrderCommand(commodity={self.commodity_type.id!r}, quantity={self.quantity}, price={self.price})"

    def execute(self, actor: "Actor") -> bool:
        """Place a sell order on the market."""
        if not actor.planet:
            return False

        order_id = actor.planet.market.place_sell_order(
            actor, self.commodity_type, self.quantity, self.price
        )

        if order_id:
            commodity_name = self.commodity_type.id
            actor.active_orders[order_id] = f"sell {commodity_name}"
            return True

        return False


# Identity of an open order for churn pruning: side, commodity, price, quantity.
_OrderKey = Tuple[bool, str, int, int]


def prune_unchanged_order_commands(
    market: "Market",
    actor: "MarketParticipant",
    commands: List[MarketCommand],
) -> List[MarketCommand]:
    """Drop cancel+repost pairs that would recreate an identical order.

    Brains rebuild their whole book each turn: cancel everything, repost the
    desired quotes. Most cancels are followed the same turn by a repost with
    the same side, commodity, price, and quantity. Such a pair is a no-op for
    market state, since the cancel refunds what the repost re-reserves and no
    other actor acts in between, so both commands are dropped and the
    standing order kept.

    Pairing on full identity keeps this behavior-exact: the kept order holds
    the same reservation the repost would take, so later commands see the
    same money and inventory. Kept orders get their timestamp refreshed to
    the current turn, as the repost would have. Without that, stable quotes
    such as market makers' gain permanent price-time priority over drive bids
    whose quantities drift, which starves thin markets. Same-timestamp ties
    are broken randomly at match time; see ``_match_orders_for_commodity``.

    Only cancels of ``actor``'s own orders in ``market`` are eligible. Anything
    unmatched passes through untouched, in the original relative order.
    """
    # Exact-class checks rather than isinstance: this runs for every command
    # of every actor-turn, and isinstance on ABC-derived classes goes through
    # abc.__instancecheck__. No command class here is ever subclassed.
    cancels_by_key: Dict[_OrderKey, List[CancelOrderCommand]] = defaultdict(list)
    for command in commands:
        if command.__class__ is CancelOrderCommand:
            command = cast(CancelOrderCommand, command)
            order = market.orders_by_id.get(command.order_id)
            if order is not None and order.actor is actor:
                key = (
                    order.is_buy,
                    order.commodity_type.id,
                    order.price,
                    order.quantity,
                )
                cancels_by_key[key].append(command)

    if not cancels_by_key:
        return commands

    dropped: set = set()
    for command in commands:
        command_class = command.__class__
        is_buy = command_class is PlaceBuyOrderCommand
        if is_buy or command_class is PlaceSellOrderCommand:
            command = cast("PlaceBuyOrderCommand | PlaceSellOrderCommand", command)
            key = (
                is_buy,
                command.commodity_type.id,
                command.price,
                command.quantity,
            )
            matching_cancels = cancels_by_key.get(key)
            if matching_cancels:
                cancel = matching_cancels.pop()
                dropped.add(id(cancel))
                dropped.add(id(command))
                # Stamp the kept order as if it had been reposted this turn.
                market.orders_by_id[cancel.order_id].timestamp = market.current_turn

    if not dropped:
        return commands
    return [c for c in commands if id(c) not in dropped]
