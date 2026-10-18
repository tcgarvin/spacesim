"""The trading company: a service actor that owns ships and buys more hulls.

A company produces nothing and consumes nothing. Its only income is the
surplus its ships remit when docked (see ``docs/dev-guide-ships.md``,
"Ownership and remittance"), and its only expense is a ship hull. Buying one
turns cash into a new ship at the company's own planet, which then remits in
turn.

The valuation is deliberately simple: a hull is worth the remittance a ship is
expected to produce over :data:`SHIP_VALUE_HORIZON_TURNS`, discounted by
:data:`SHIP_RAMP_DISCOUNT` because a new ship spends its first hundred turns
building a cash position rather than paying out. There is no nominal hull
price anywhere: what a company will pay is whatever its own fleet has earned,
so a galaxy whose ships lose money never builds another one.

A company never sells, never works, and never migrates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from spacesim2.core.actor import Actor
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.commands import (
    CancelOrderCommand,
    EconomicCommand,
    MarketCommand,
    PlaceBuyOrderCommand,
)
from spacesim2.core.ship_spawn import ShipSpawn

if TYPE_CHECKING:
    from spacesim2.core.commodity import CommodityDefinition
    from spacesim2.core.market import Market, Order
    from spacesim2.core.planet import Planet
    from spacesim2.core.ship import Ship

# Turns of remittance a hull is expected to repay. A ship is a long-lived
# asset and nothing retires one, so the horizon, not a lifetime, is what
# bounds the price.
SHIP_VALUE_HORIZON_TURNS = 200

# Fraction of the horizon's remittance a hull is actually worth. A new ship
# earns almost nothing for its first hundred turns or so: it starts with one
# endowment of working capital and has to accumulate before anything is
# surplus. Half the naive figure is the allowance for that ramp.
SHIP_RAMP_DISCOUNT = 0.5

# Weight of the newest turn in the remittance-rate EMA. At 0.05 the rate has a
# memory of roughly twenty turns, long enough to ignore the lumpiness of
# individual ships docking and short enough to follow a fleet that stops
# earning.
REMITTANCE_RATE_ALPHA = 0.05

# Turns of remittance history required before a company will bid at all. The
# rate after one or two turns is whatever the first ship happened to remit,
# which is no basis for a four-figure purchase.
MIN_REMITTANCE_HISTORY_TURNS = 50

# Relative price move that justifies cancelling and reposting the resting hull
# bid. Below it the old bid keeps its place in the book.
BID_REPRICE_FRACTION = 0.05

HULL_COMMODITY_ID = "ship_hull"


class TradingCompanyBrain(ActorBrain):
    """Owns a fleet, banks its remittances, and bids for hulls to grow it.

    State is the fleet, the remittance EMA, and a count of the turns the EMA
    has seen. Ship creation is not done here: the actor phase is threaded, so
    the brain files a :class:`ShipSpawn` request and
    ``Simulation.run_turn`` builds the ship serially.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fleet: List["Ship"] = []
        # Ships ever registered, so names stay unique for the company's life.
        self._ships_created: int = 0
        # Credits remitted since the last time the rate was updated, and the
        # rate itself: credits per owned ship per turn.
        self._income_since_accounted: int = 0
        self.remittance_rate: float = 0.0
        self.accounted_turns: int = 0

    # -------- Fleet and remittances ------------------------------------------

    def register_ship(self, ship: "Ship") -> int:
        """Record a ship this company owns; returns its ordinal in the fleet.

        Called serially, at setup and when a spawn request is drained. The
        ordinal names ships the company builds and never repeats.
        """
        self.fleet.append(ship)
        self._ships_created += 1
        return self._ships_created

    def on_remittance(self, actor: Actor, ship: "Ship", amount: int) -> None:
        """Bank a ship's surplus. The money is already on ``actor.money``."""
        self._income_since_accounted += amount

    def hull_value(self) -> int:
        """What one hull is worth to this company, in credits.

        Zero until the rate has :data:`MIN_REMITTANCE_HISTORY_TURNS` of
        history behind it, which is what keeps a company from pricing a ship
        off its first lucky turn.
        """
        if self.accounted_turns < MIN_REMITTANCE_HISTORY_TURNS:
            return 0
        return int(self.remittance_rate * SHIP_VALUE_HORIZON_TURNS * SHIP_RAMP_DISCOUNT)

    def _account_income(self) -> None:
        """Fold the remittances banked since the last turn into the rate."""
        fleet_size = max(1, len(self.fleet))
        sample = self._income_since_accounted / fleet_size
        self._income_since_accounted = 0
        self.accounted_turns += 1
        self.remittance_rate += REMITTANCE_RATE_ALPHA * (sample - self.remittance_rate)

    # -------- Required interface ---------------------------------------------

    def decide_economic_action(self, actor: Actor) -> Optional["EconomicCommand"]:
        """None. A company owns ships; it does not work."""
        return None

    def decide_market_actions(self, actor: Actor) -> List["MarketCommand"]:
        """Account income, turn any bought hull into a ship, then quote a bid."""
        self._account_income()

        planet = actor.planet
        if planet is None:
            return []
        hull = actor.sim.commodity_registry.get_commodity(HULL_COMMODITY_ID)
        if hull is None:
            actor.last_action = "Idle: ship_hull is not a commodity"
            return []

        market = planet.market
        resting = [
            order
            for order in market.get_actor_orders(actor)["buy"]
            if order.commodity_type is hull
        ]

        if actor.inventory.get_quantity(hull) > 0:
            self._request_ship(actor, planet, hull)
            # The endowment just left; price the next hull next turn.
            return [CancelOrderCommand(order.order_id) for order in resting]

        return self._hull_bid_commands(actor, market, hull, resting)

    # -------- Hull bid --------------------------------------------------------

    def _hull_bid_commands(
        self,
        actor: Actor,
        market: "Market",
        hull: "CommodityDefinition",
        resting: List["Order"],
    ) -> List["MarketCommand"]:
        """Keep exactly one hull bid resting at the company's valuation.

        Cancelling a resting bid refunds its reserved money, so the cash a new
        bid may draw on is the actor's money plus what the cancels release. The
        endowment the new ship will carry is held back: a hull with no crew
        money is a ship that cannot move.
        """
        cancels: List["MarketCommand"] = [
            CancelOrderCommand(order.order_id) for order in resting
        ]
        reserved = sum(order.quantity * order.price for order in resting)
        cash = actor.money + reserved
        endowment = actor.sim.ship_starting_capital
        value = self.hull_value()

        if value <= 0 or cash - endowment - value < 0:
            actor.last_action = (
                f"Fleet of {len(self.fleet)}: no hull bid "
                f"(value {value}, cash {cash}, endowment {endowment})"
            )
            return cancels

        if len(resting) == 1 and abs(resting[0].price - value) <= (
            BID_REPRICE_FRACTION * value
        ):
            actor.last_action = (
                f"Fleet of {len(self.fleet)}: hull bid {resting[0].price} stands"
            )
            return []

        actor.last_action = f"Fleet of {len(self.fleet)}: bidding {value} for a hull"
        cancels.append(PlaceBuyOrderCommand(hull, 1, value))
        return cancels

    # -------- Ship spawn ------------------------------------------------------

    def _request_ship(
        self, actor: Actor, planet: "Planet", hull: "CommodityDefinition"
    ) -> None:
        """Consume one hull and file a spawn request with its endowment.

        The endowment is capped at the cash on hand. A company only bids when
        it holds the endowment on top of the bid, so the cap bites only in a
        hand-built world; it is here so the request can never overdraw.
        """
        actor.inventory.remove_commodity(hull, 1)
        endowment = min(actor.sim.ship_starting_capital, actor.money)
        actor.money -= endowment
        actor.sim.pending_ship_spawns.request(
            ShipSpawn(owner=actor, planet=planet, endowment=endowment)
        )
        actor.last_action = f"Ordered a ship with {endowment} credits"
