"""The spaceport operator: a service brain that deals fuel from a facility.

The gap this fills is a coordination failure, not a production one (see
``docs/spaceport-design.md``). Industrialists list fuel at an honest
replacement-cost floor that only ships ever bid for, and stranded ships post
rescue bids too small to be worth a delivery. The operator is the standing
counterparty between them: it bids at a price a producer or a deliverer will
actually take, and asks at what restocking would cost it today plus a spread,
so a docking ship always has somewhere to refuel.

It is a *service* actor, so it does not eat: its only need is keeping its
spaceport maintained, expressed as a ``FacilityUpkeepDrive``. Condition scales
how much it will sell and, below a floor, withdraws its ask entirely. That is
the whole consequence of neglect: no bankruptcy, just a port that stops being
a fuel source.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, List, Optional, Tuple

from spacesim.core.actor import Actor
from spacesim.core.actor_brain import ActorBrain
from spacesim.core.brains import dealer
from spacesim.core.commands import (
    CancelOrderCommand,
    EconomicCommand,
    GovernmentWorkCommand,
    MarketCommand,
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
)
from spacesim.core.drives.facility_upkeep_drive import (
    DRIVE_NAME as FACILITY_UPKEEP_DRIVE_NAME,
)
from spacesim.core.drives.facility_upkeep_drive import FacilityUpkeepDrive
from spacesim.core.navigation import get_navigator
from spacesim.core.ship import fuel_capacity_for, milli_to_units_ceil

if TYPE_CHECKING:
    from spacesim.core.commodity import CommodityDefinition
    from spacesim.core.market import Market

# Facility condition below which the operator stops offering fuel at all. A
# badly neglected port cannot be trusted to fuel a ship safely, and withdrawing
# the ask is the only lever the design gives neglect: the navigator already
# reads "no live ask" as "cannot refuel here".
CONDITION_ASK_FLOOR: float = 0.25

# Cap on how far inventory skew may move a quote, matching the market maker.
INVENTORY_SKEW_CAP: float = 0.50

# Reference price floor for a standing upkeep bid, so a market with no history
# is not bid at its fabricated default of a few credits. Same rule, and same
# number, as Ship._buy_maintenance_supplies uses for its standing bids.
UPKEEP_BID_REFERENCE_FLOOR: int = 10

FUEL_COMMODITY_ID = "nova_fuel"
SPACEPORT_COMMODITY_ID = "spaceport"


class SpaceportOperatorBrain(ActorBrain):
    """Stocks and sells nova_fuel from a spaceport, and maintains the port.

    State is deliberately small: a cursor into the actor's own fill history,
    a ``(units, total_cost)`` pool for the fuel cost basis, and a per-operator
    spread drawn once so two operators on the same planet do not quote
    identically.
    """

    # Fraction of post-upkeep cash the fuel bid may commit. Upkeep is paid
    # first: a port that cannot repair itself stops being able to sell.
    BUY_CAPITAL_FRACTION: float = dealer.DEFAULT_BUY_CAPITAL_FRACTION

    def __init__(self) -> None:
        """Create an operator with an empty book and a freshly drawn spread."""
        super().__init__()
        # Opaque fill cursor, advanced by dealer.ingest_fills.
        self._fill_cursor: int = 0
        # Fuel cost basis pool: units attributed to it and money spent on them.
        self._fuel_units: int = 0
        self._fuel_total_cost: float = 0.0
        self.spread: float = dealer.draw_spread()

    # -------- Required interface ---------------------------------------------

    def decide_economic_action(self, actor: Actor) -> Optional["EconomicCommand"]:
        """Always government work.

        The operator produces nothing; the wage is what bootstraps its first
        fuel purchases and pays for upkeep materials, exactly as it does for a
        market maker.
        """
        return GovernmentWorkCommand()

    def decide_market_actions(self, actor: Actor) -> List["MarketCommand"]:
        """Cancel, re-read fills, then quote upkeep demand and both fuel sides.

        Order matters. Cancelling first releases the money reserved by last
        turn's unfilled bids so it can be re-committed. Upkeep is priced and
        funded before fuel, because the facility is what makes the fuel
        business possible. The fuel bid then draws on what is left.
        """
        planet = actor.planet
        if planet is None:
            return []

        market = planet.market
        commands: List["MarketCommand"] = []

        # Cancel and repost the whole book each turn, as every other brain
        # does; identical quotes are pruned before execution.
        existing_orders = market.get_actor_orders(actor)
        for order in existing_orders["buy"] + existing_orders["sell"]:
            commands.append(CancelOrderCommand(order.order_id))

        self._ingest_fuel_fills(actor, market)

        drive = self._upkeep_drive(actor)
        fuel = actor.sim.commodity_registry.get_commodity(FUEL_COMMODITY_ID)
        spaceport = actor.sim.commodity_registry.get_commodity(SPACEPORT_COMMODITY_ID)
        if drive is None:
            actor.last_action += " (idle: no facility upkeep drive)"
            return commands
        if spaceport is None or actor.inventory.get_quantity(spaceport) <= 0:
            actor.last_action += " (idle: no spaceport to operate)"
            return commands
        if fuel is None:
            actor.last_action += " (idle: nova_fuel is not a commodity)"
            return commands

        condition = drive.metrics.health

        upkeep_commands, cash_left = self._upkeep_commands(actor, market, drive)
        commands.extend(upkeep_commands)

        held = actor.inventory.get_quantity(fuel)
        target = self._stock_target(actor, market, fuel)

        ask = self._fuel_ask(actor, market, fuel, held, target, condition)
        bid = self._fuel_bid(actor, market, fuel, held, target, cash_left)

        # Self-trade guard: a bid at or above our own ask would match against
        # it at end-of-turn matching, churning inventory for a guaranteed loss.
        if bid is not None and ask is not None and bid.price >= ask.price:
            bid = None

        if bid is not None:
            commands.append(bid)
        if ask is not None:
            commands.append(ask)

        return commands

    # -------- Fills and cost basis -------------------------------------------

    def _ingest_fuel_fills(self, actor: Actor, market: "Market") -> None:
        """Roll the fuel cost basis forward through this turn's own fills.

        The basis does not set the ask (see :meth:`_fuel_ask`); it is the
        record of what this stock actually cost, which is what says whether
        the fuel business is making money. Every buy and sell has to be seen
        for it to mean anything.
        """
        self._fill_cursor, grouped = dealer.ingest_fills(
            actor, market, self._fill_cursor
        )
        fuel = actor.sim.commodity_registry.get_commodity(FUEL_COMMODITY_ID)
        if fuel is None:
            return
        fills = grouped.get(dealer.commodity_key(fuel))
        if fills is None:
            return
        self._fuel_units, self._fuel_total_cost, _ = dealer.cost_basis(
            self._fuel_units, self._fuel_total_cost, fills.buys, fills.sells
        )

    def _fuel_cost_basis(self) -> float:
        """Per-unit cost of the fuel pool, or 0.0 when it is empty."""
        if self._fuel_units <= 0:
            return 0.0
        return self._fuel_total_cost / self._fuel_units

    # -------- Upkeep ----------------------------------------------------------

    def _upkeep_drive(self, actor: Actor) -> Optional[FacilityUpkeepDrive]:
        """The actor's facility-upkeep drive, or None if it has none.

        Found by metric name rather than by type so a hand-built actor with a
        differently-constructed upkeep drive still works. Returning None is the
        signal that this actor is not really an operator, and the brain then
        does nothing but government work.
        """
        for drive in actor.drives:
            if drive.metrics.get_name() == FACILITY_UPKEEP_DRIVE_NAME and isinstance(
                drive, FacilityUpkeepDrive
            ):
                return drive
        return None

    def _upkeep_commands(
        self, actor: Actor, market: "Market", drive: FacilityUpkeepDrive
    ) -> Tuple[List["MarketCommand"], int]:
        """Buy repair materials, and report the cash left for fuel.

        Two things are wanted: a working buffer of the most likely failure's
        material, sized by ``drive.target_units()``, and one unit of whatever
        the last uncovered failure asked for, so a port stuck on an exotic part
        actually goes looking for it. Local asks are lifted where they exist;
        otherwise a standing, scarcity-escalated bid rests in the book so a
        producer somewhere sees the demand.

        Returns:
            ``(commands, cash_remaining)``. The remainder is what the fuel bid
            may draw on: the facility is the business, so it is funded first.
        """
        commands: List["MarketCommand"] = []
        cash = actor.money

        wants: List[Tuple["CommodityDefinition", int]] = []
        materials = drive.materials()
        if materials:
            # materials() is ordered most-likely-failure first.
            wants.append((materials[0], drive.target_units()))
        unmet = drive.last_unmet
        if unmet is not None:
            unmet_commodity = actor.sim.commodity_registry.get_commodity(
                unmet.material_id
            )
            if unmet_commodity is not None and not any(
                unmet_commodity is commodity for commodity, _ in wants
            ):
                wants.append((unmet_commodity, 1))

        for commodity, want in wants:
            shortfall = want - actor.inventory.get_quantity(commodity)
            if shortfall <= 0 or cash <= 0:
                continue
            price = self._upkeep_price(market, commodity)
            quantity = min(shortfall, cash // price)
            if quantity <= 0:
                continue
            commands.append(PlaceBuyOrderCommand(commodity, quantity, price))
            cash -= quantity * price

        return commands, max(0, cash)

    def _upkeep_price(self, market: "Market", commodity: "CommodityDefinition") -> int:
        """What to pay for one unit of a repair material.

        A resting ask is lifted at its own price, which is the cheapest way to
        get the material today. With no ask, rest a bid at a reference price
        escalated by the market's scarcity pressure, the same rule ships use
        for standing maintenance bids: the price grows each turn the demand
        goes unmet, so it eventually reaches a producer's floor.
        """
        _, ask = market.get_bid_ask_spread(commodity)
        if ask is not None and ask > 0:
            return ask
        return max(
            1,
            math.ceil(
                max(UPKEEP_BID_REFERENCE_FLOOR, market.get_avg_price(commodity))
                * (1.0 + market.scarcity_pressure_for(commodity))
            ),
        )

    # -------- Fuel ------------------------------------------------------------

    def _stock_target(
        self, actor: Actor, market: "Market", fuel: "CommodityDefinition"
    ) -> int:
        """Fuel units to hold: 30 days of flow, floored at one ship tank.

        The floor is the point of a spaceport. A quiet port that sized stock
        purely on observed flow would hold almost nothing and could not fill
        the one ship that does turn up, which is exactly the stranding it
        exists to prevent.
        """
        navigator = get_navigator(actor.sim)
        # fuel_capacity_for is millifuel; the stock floor is market units.
        one_tank = milli_to_units_ceil(
            fuel_capacity_for(navigator.mean_pair_distance(), 1.0)
        )
        return dealer.flow_stock_target(market, fuel, floor=one_tank)

    def _fuel_bid(
        self,
        actor: Actor,
        market: "Market",
        fuel: "CommodityDefinition",
        held: int,
        target: int,
        cash: int,
    ) -> Optional[PlaceBuyOrderCommand]:
        """Bid for the fuel needed to reach the stock target, within budget.

        The bound is arbitrage, not a fixed ceiling: the delivered price is
        what importing ``need`` units would cost, so it is the most the
        operator would ever rationally pay here.

        - A local ask rests at or below the delivered price: lift it. Buying
          locally beats paying for a delivery.
        - Otherwise rest a bid at the delivered price, skewed *down* by
          inventory. That price is what a deliverer needs to see for a fuel
          run to beat its alternatives, and the skew pulls the bid below it as
          stock approaches target so the operator stops paying delivery prices
          for fuel it no longer needs. The skew never pushes the bid above the
          delivered price: that price is an arbitrage ceiling, not a midpoint,
          and paying over it means paying more than importing would cost.

        The operator never sits out while it needs fuel and can afford a unit.
        An earlier version gated the lift on the galaxy-wide fuel value
        reference, which is a minimum polluted by one-unit discovery asks at a
        few credits; the resulting ceiling sat far below every real ask and
        most operators posted nothing, every turn.

        Nothing is pending: this turn's cancels release last turn's bids, so
        held stock is the whole position.
        """
        planet = actor.planet
        need = target - held
        if planet is None or need <= 0 or cash <= 0:
            return None

        budget = int(cash * self.BUY_CAPITAL_FRACTION)
        if budget <= 0:
            return None

        _, ask = market.get_bid_ask_spread(fuel)
        delivered = get_navigator(actor.sim).fuel_delivery_bid_price(planet, need)
        if ask is not None and ask > 0 and ask <= delivered:
            price = ask
        else:
            price = min(
                delivered,
                dealer.skew_midpoint(
                    delivered, held, target, INVENTORY_SKEW_CAP, min_price=1
                ),
            )

        price = max(1, price)
        quantity = min(need, budget // price)
        if quantity <= 0:
            return None
        return PlaceBuyOrderCommand(fuel, quantity, price)

    def _fuel_ask(
        self,
        actor: Actor,
        market: "Market",
        fuel: "CommodityDefinition",
        held: int,
        target: int,
        condition: float,
    ) -> Optional[PlaceSellOrderCommand]:
        """Offer stock at restock cost plus spread, scaled and gated by condition.

        The floor is what replacing a unit costs *now*, not what this stock
        happened to cost: the same rule producers follow when they floor an ask
        at replacement cost. A dealer whose suppliers got cheaper realizes the
        loss and sells; pricing off a historical cost basis instead froze
        thousands of units above market for hundreds of turns, because the
        stock had been bought during a price spike and the floor never came
        back down. The cost basis is still tracked, for analysis and for
        measuring whether the business makes money, but it no longer sets the
        price.

        Over target the skew walks the price down toward the floor to clear
        the excess, and never below it. Condition scales the offered quantity,
        and below :data:`CONDITION_ASK_FLOOR` the ask is withdrawn: a
        neglected port stops being a fuel source.
        """
        if held <= 0 or condition < CONDITION_ASK_FLOOR:
            return None

        restock = self._restock_cost(actor, market, fuel, target)
        floor = math.ceil(restock) + 1
        price = max(floor, math.ceil(restock * (1.0 + self.spread)))
        if held > target:
            price = dealer.skew_midpoint(
                price, held, target, INVENTORY_SKEW_CAP, min_price=floor
            )

        quantity = max(1, int(math.floor(held * condition)))
        return PlaceSellOrderCommand(fuel, quantity, price)

    def _restock_cost(
        self,
        actor: Actor,
        market: "Market",
        fuel: "CommodityDefinition",
        target: int,
    ) -> float:
        """What one more unit of fuel would cost the operator today.

        The cheaper of buying from somebody else's resting local ask and
        paying the delivered price for a restock of ``target`` units, which is
        exactly the choice :meth:`_fuel_bid` makes. Our own ask is excluded:
        quoting off it would just repeat yesterday's price forever.
        """
        planet = actor.planet
        delivered = (
            float(get_navigator(actor.sim).fuel_delivery_bid_price(planet, target))
            if planet is not None
            else float(max(1, market.get_avg_price(fuel)))
        )
        best_local: Optional[int] = None
        for order in market.sell_orders.get(fuel, []):
            if order.cancelled or order.actor is actor:
                continue
            if best_local is None or order.price < best_local:
                best_local = order.price
        if best_local is not None and best_local < delivered:
            return float(max(1, best_local))
        return max(1.0, delivered)
