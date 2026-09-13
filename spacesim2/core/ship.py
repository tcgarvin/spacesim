import enum
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple, Union

from spacesim2.core.commodity import CommodityDefinition, Inventory
from spacesim2.core.contracts import (
    CONTRACT_STRAND_PATIENCE,
    ConsignmentPayload,
    Contract,
    ContractStatus,
    deliver_contract,
    load_contract,
    strand_contract,
)
from spacesim2.core.navigation import (
    DELIVERER_WORST_FUEL_EFFICIENCY,  # noqa: F401  re-exported for callers
    FLOW_RECENCY_TURNS,
    FUEL_BID_FALLBACK_FLOOR,
    Navigator,
    get_navigator,
)
from spacesim2.core.planet import Planet

if TYPE_CHECKING:
    from spacesim2.core.drives.actor_drive import ActorDrive
    from spacesim2.core.market import Market
    from spacesim2.core.simulation import Simulation

# FUEL_BID_MARGIN, FUEL_BID_FALLBACK_FLOOR and DELIVERER_WORST_FUEL_EFFICIENCY
# live in core/navigation.py, next to the delivery-bid pricing they
# parameterize, and are re-exported here because ships and their tests have
# always read them from this module.

# A docked ship fills its tank only while the local ask is within this
# multiple of the galaxy fuel reference price, the median believable
# per-planet valuation. Above it, it buys only the survival minimum:
# filling a whole tank at spike prices bankrupts ships. A typical planet
# sits near the reference and bunkers; only a real spike is priced out.
FUEL_BUNKER_PREMIUM = 1.3

# Fraction of a ship's money that may go to fuel beyond the survival
# minimum. Fuel is working capital parked in the tank and cargo margins are
# usually better, so bunkering must not crowd out trading cash. This is the
# fraction for an ask above the galaxy reference but still within
# FUEL_BUNKER_PREMIUM.
FUEL_BUNKER_BUDGET_FRACTION = 0.5
# The fraction when the ask is at or below the reference. Fuel bought at or
# under the typical galaxy price is not a loss to recover later and the tank
# costs no hold space, so more of the purse is worth parking there. Measured
# over 300 turns at 100 planets, the fleet bought 3921 units through the
# bunker path at 50 credits each, 26k below reference, and 1096 units through
# the survival ration at 243 credits each, 195k above it. Buying more where
# fuel is cheap is what removes the expensive purchases.
FUEL_BUNKER_BUDGET_FRACTION_CHEAP = 0.8

# A ship that is ready to depart stays docked one more turn to keep bunkering
# only while its tank is below this fraction of capacity. With a fuller tank
# there is too little room left for the price saving to beat a turn of the
# trip it is delaying.
FUEL_LINGER_TANK_FRACTION = 0.5

# En-route refuel stops. A ship flying a multi-lane route may dock at an
# intermediate planet to buy cheap fuel and then resume to its original
# destination. It stops only while the tank, once the unburned route fuel is
# refunded, is below this fraction of capacity: with a fuller tank there is
# too little room left for the saving to beat the turns the stop costs.
# Measured at 40 planets over 150 turns, the nodes this gate refused sat just
# above the old 0.5 line, at a median tank of 0.533 and a p75 of 0.56, so the
# gate was mostly turning away ships with two thirds of a tank of room.
FUEL_STOP_TANK_FRACTION = 0.6
# Millifuel the per-leg rounding may cost before a stop is refused outright.
# ``fuel_required`` ceils once, to the thousandth, so splitting a route at a
# node costs ceil(a) + ceil(b) - ceil(a + b) millifuel, which is 0 or 1 for
# any real a and b. A cap of 1 therefore admits every rounding split and
# refuses nothing else, which is all this guard was ever meant to do. The old
# cap of 2 whole units existed because fuel_required ceiled a second time
# after dividing by efficiency; that second ceiling is gone.
REFUEL_STOP_MAX_SHORTFALL = 1
# Docked turns a refuel stop may last before the ship abandons the resume and
# normal docked logic takes over. The stop is meant to last two turns, one to
# bid and one to depart; a longer wait means the departure fuel check keeps
# refusing, and holding the mode open only keeps the ship out of the planner.
REFUEL_STOP_MAX_TURNS = 3

# Max cargo units for a plan whose destination has no resting bids. Revenue
# is then a guess from the average price, so the exposure is capped.
SPECULATIVE_PLAN_CAP = 10

# Extra commodities loaded alongside a plan's cargo for the same
# destination. Measured at plan departures over a 400-turn 100-planet run,
# the hold was 21% full at the median and a second good to the same
# destination had positive marginal profit at 84% of departures, worth a
# third of the plan's own profit. The fuel is already paid for, so an add-on
# only has to beat its purchase cost by the plan margin.
ADDON_MAX_COMMODITIES = 3

# Flow-based planning. With deferred end-of-turn matching, the resting book
# holds only what the local auction rejected: no asks for goods in local
# demand, lowball leftover bids. Real supply and demand clear in the per-turn
# flow, so plans estimate from recent traded prices and volume, bid into the
# auction, and fill over several docked turns. Trading only the residual
# book cannot pay for fuel.
#
# Turns of destination flow a plan expects to sell into. Sizes cargo so a
# hold is not dumped into a market that clears one unit a turn. The
# volume-history window that counts a good as trading is FLOW_RECENCY_TURNS,
# shared with spacesim2.core.navigation.
DEMAND_HORIZON_TURNS = 15
# Docked turns a ship keeps a resting buy order open to fill its plan before
# departing with what it has, or replanning if nothing filled.
ACCUMULATION_PATIENCE = 8
# Confidence discount on avg-price revenue estimates and resting asks. The
# flow price is a forecast, not a resting order.
SELL_PRICE_HAIRCUT = 0.9

# Cargo revenue. A ship prices its asks against what the cargo cost it, and
# marks that floor down the longer the cargo sits unsold, so a load that no
# market wants at cost still turns back into cash instead of riding along for
# hundreds of turns.
#
# Fraction of the cost basis the floor drops per docked turn a listed
# commodity goes unfilled, and the fraction of basis it never falls below.
UNSOLD_DECAY = 0.1
LIQUIDATION_FLOOR = 0.4
# Cargo hops - departures taken to sell a load somewhere else rather than
# under a trade plan - a load may make before it must be sold where it is.
# Each hop costs fuel against a forecast that is usually gone by arrival.
MAX_CARGO_HOPS = 1
# Consecutive docked turns the departure gate may refuse a ship holding cargo
# for another market before the cargo is listed where the ship actually is.
HOLD_PATIENCE = 3

# Candidate origin planets an empty repositioning ship surveys, in proximity
# order. Surveying every origin is O(planets^2 x commodities) per ship per
# turn, and nearby origins need less fuel to reach anyway. Galaxies with
# fewer planets than the cap are still surveyed exhaustively.
REPOSITION_ORIGIN_CANDIDATES = 12
# Docked turns with no plan of either kind after which an empty ship will
# reposition toward a contract queue on the payments alone, without the
# comparison against trade plans elsewhere picking that origin. A queue sits
# on planets ships rarely call at, so nothing else brings a ship there.
REPOSITION_CONTRACT_PATIENCE = 3

# Chance per departure that a ship rolls a maintenance stop, and the fuel
# units the legacy maintenance tier consumes. Used by Ship.check_maintenance
# and to price expected maintenance into trade plans.
MAINTENANCE_CHANCE = 0.1
MAINTENANCE_FUEL_UNITS = 5

# Maintenance goods, best quality first, as (commodity id, units per repair,
# label). A "repair kit" is one complete tier of the cargo goods. The fuel
# tier is the legacy fallback: it burns tank fuel rather than hold cargo, and
# is what MAINTENANCE_FUEL_UNITS of arrival reserve exists to survive.
MAINTENANCE_KIT_TIERS: Tuple[Tuple[str, int, str], ...] = (
    ("ship_components", 1, "ship components"),
    ("ship_parts", 2, "ship parts"),
    ("ship_supplies", 3, "ship supplies"),
)
MAINTENANCE_TIERS: Tuple[Tuple[str, int, str], ...] = MAINTENANCE_KIT_TIERS + (
    ("nova_fuel", MAINTENANCE_FUEL_UNITS, "fuel"),
)
MAINTENANCE_KIT_IDS = frozenset(tier[0] for tier in MAINTENANCE_KIT_TIERS)

# Consecutive docked turns with no cargo, no plan and less cash than one
# short round trip's fuel before a ship counts as distressed. A distressed
# ship may liquidate the working capital parked in its tank, selling fuel
# down to its survival target instead of holding a full tank it cannot
# trade around. Below the survival target it would strand, so that is the
# floor; selling only to the target also keeps the next turn's fuel top-up
# from re-buying what was just sold.
DISTRESS_PATIENCE = 5

# Starting capital, scaled to galaxy geometry. Every trade plan must fund
# round-trip fuel, a refuel floor and expected maintenance before a credit
# goes to cargo, so a fixed purse silently grounds the fleet as lane routes
# grow: at 100 planets a mean round trip burns roughly six times the fuel it
# does at five. Capital is SHIP_CAPITAL_ROUND_TRIPS average round trips of
# fuel at a reference price, plus a working-capital fraction for cargo.
SHIP_CAPITAL_ROUND_TRIPS = 3
# Credits per fuel unit assumed when sizing capital at setup, where no market
# has traded yet. Calibrated so a five-planet galaxy reproduces the 1000
# credits the economy was originally tuned with.
SHIP_CAPITAL_FUEL_PRICE_REFERENCE = 40
SHIP_CAPITAL_RESERVE_FRACTION = 0.25
SHIP_CAPITAL_FLOOR = 1000

# Tank size. The tank is separate from the hold, so it costs no cargo space
# and is sized for several mean round trips: a ship can bunker where fuel is
# cheap and fly past spiked markets instead of buying its next leg wherever
# it happens to be docked. Measured before the split, spiked survival top-ups
# were 252k of the fleet's 266k fuel premium over 600 turns.
# Tank fuel is stored in integer thousandths of a market unit (millifuel),
# so a leg burns exactly what its distance implies instead of rounding up to
# a whole unit, and the tank never holds a float. One market unit of
# nova_fuel pumped from the hold adds FUEL_MILLI to the tank. Constants that
# describe market units keep the _UNITS suffix; tank values are millifuel.
FUEL_MILLI = 1000
# Distance one market unit of fuel carries a ship at efficiency 1.0.
DISTANCE_PER_FUEL_UNIT = 200.0
# Distance a ship covers in one turn. Travel time is independent of fuel
# efficiency and of the millifuel resolution.
DISTANCE_PER_TURN = 20.0
BASE_FUEL_CAPACITY = 6 * FUEL_MILLI
FUEL_CAPACITY_ROUND_TRIP_HEADROOM = 3.0
# Share of the tank a new ship is launched with.
INITIAL_FUEL_FRACTION = 0.6


@dataclass
class TradePlan:
    """A complete trade opportunity for a ship trader.

    Holds where to buy, where to sell, the commodity, and the expected costs
    and profit of one haul.

    Two prices per unit, deliberately distinct:
    - ``bid_price_per_unit`` is what the ship posts in the book. It may sit
      above the cheapest resting ask so the order also wins units out of the
      turn's flow; matching executes at each seller's ask, so bidding high
      never overpays for the cheap fills.
    - ``purchase_price_per_unit`` is what the cargo is expected to *cost*:
      the resting asks walked for ``quantity`` units, any remainder priced at
      the bid. Judging the margin on the bid inflated the cost basis of every
      plan that had a real cheap ask behind it, and that inflated number also
      filtered the destination's bids and flow.

    Fuel is charged one way, not round trip. This cargo rides the outbound
    leg only; the return leg is capital the *next* trade spends. Charging
    both legs to one haul killed genuine spreads whose quantity was
    flow-capped to a handful of units. The round trip still has to be
    *funded*: that gate lives in :meth:`TraderBrain._pair_economics`, which
    reserves round-trip fuel cash and a refuel floor before a credit reaches
    cargo.
    """

    origin: Planet
    destination: Planet
    commodity: CommodityDefinition

    # Quantities and costs
    quantity: int
    bid_price_per_unit: int
    purchase_price_per_unit: int
    expected_sell_price_per_unit: int

    # Fuel calculations. ``fuel_needed_one_way`` and ``fuel_units_from_tank``
    # are millifuel (see FUEL_MILLI); the two prices are credits per market
    # unit, so every product of the two is divided by FUEL_MILLI. Of the
    # outbound leg's burn, ``fuel_units_from_tank`` is already aboard and is
    # valued at ``fuel_price_from_tank`` - the galaxy fuel reference, what
    # replacing it typically costs. The rest must be bought here at
    # ``fuel_price_at_origin``, the local ask, however spiked that is.
    distance: float
    fuel_needed_one_way: int
    fuel_price_at_origin: int
    fuel_units_from_tank: int
    fuel_price_from_tank: int

    # Expected cost of maintenance rolls over the round trip: 2 departures x
    # MAINTENANCE_CHANCE x the fuel-tier repair cost. Without it penny-margin
    # trades look profitable when one repair wipes out several trips of
    # profit.
    expected_maintenance_cost: int = 0

    @property
    def fuel_needed_round_trip(self) -> int:
        """Fuel for the round trip, which the cash gate budgets for."""
        return self.fuel_needed_one_way * 2

    @property
    def total_fuel_cost(self) -> int:
        """Cost of the outbound leg's fuel, the only leg this cargo rides.

        Units already aboard are charged at the fuel reference, units that
        must be bought at the local ask. Both are charged: fuel burned is
        value spent, whenever it was bought.
        """
        from_tank_milli = min(self.fuel_units_from_tank, self.fuel_needed_one_way)
        to_buy_milli = self.fuel_needed_one_way - from_tank_milli
        return round(
            (
                from_tank_milli * self.fuel_price_from_tank
                + to_buy_milli * self.fuel_price_at_origin
            )
            / FUEL_MILLI
        )

    @property
    def total_purchase_cost(self) -> int:
        """Expected cost of the cargo, at the evaluation price."""
        return self.quantity * self.purchase_price_per_unit

    @property
    def expected_revenue(self) -> int:
        """Expected revenue from selling at destination."""
        return self.quantity * self.expected_sell_price_per_unit

    @property
    def expected_profit(self) -> int:
        """Expected profit after all costs."""
        return (
            self.expected_revenue
            - self.total_purchase_cost
            - self.total_fuel_cost
            - self.expected_maintenance_cost
        )

    @property
    def return_leg_fuel_cost(self) -> int:
        """Cost of buying the return leg's fuel at the origin's ask.

        The haul itself is charged one way (see the class docstring), because
        the return leg is capital the next trade spends. A ship with no
        capital has no next trade to spend it: the profit of this haul is the
        only money the return leg can come from, so a distressed ship judges
        the haul against this too. Priced at the origin ask, which is what
        such a ship actually pays before it leaves.
        """
        return round(self.fuel_needed_one_way * self.fuel_price_at_origin / FUEL_MILLI)

    @property
    def profit_margin(self) -> float:
        """Profit as a percentage of costs."""
        total_costs = (
            self.total_purchase_cost
            + self.total_fuel_cost
            + self.expected_maintenance_cost
        )
        if total_costs <= 0:
            return 0.0
        return self.expected_profit / total_costs

    MIN_MARGIN = 0.15

    def is_profitable(self) -> bool:
        """Whether the trade meets the minimum profitability threshold."""
        return self.expected_profit > 0 and self.profit_margin >= self.MIN_MARGIN


@dataclass
class ContractPlan:
    """A trip flown for the payments of a group of contracts, not for cargo.

    Every contract in ``contracts`` runs from ``origin`` to ``destination``,
    so one departure delivers the whole group. The fuel is priced exactly as
    :class:`TradePlan` prices it - the outbound leg only, units already
    aboard at the galaxy reference and the rest at the local ask - because
    the return leg is capital the next trip spends.

    There is no cargo and so no margin to take a percentage of. The trip is
    worth flying when the payments cover the fuel and the expected
    maintenance, which is the test a distressed ship already applies to a
    trade plan.
    """

    origin: Planet
    destination: Planet
    contracts: Tuple[Contract, ...]

    distance: float
    fuel_needed_one_way: int
    fuel_price_at_origin: int
    fuel_units_from_tank: int
    fuel_price_from_tank: int
    expected_maintenance_cost: int = 0

    @property
    def hold_units(self) -> int:
        """Hold space the whole group occupies."""
        return sum(contract.payload.hold_units for contract in self.contracts)

    @property
    def advance_total(self) -> int:
        """Payments made when the payloads load, before the ship departs."""
        return sum(contract.advance for contract in self.contracts)

    @property
    def on_delivery_total(self) -> int:
        """Payments made on arrival at the destination."""
        return sum(contract.on_delivery for contract in self.contracts)

    @property
    def total_fuel_cost(self) -> int:
        """Cost of the outbound leg's fuel, mixed-priced as a trade plan's is."""
        from_tank_milli = min(self.fuel_units_from_tank, self.fuel_needed_one_way)
        to_buy_milli = self.fuel_needed_one_way - from_tank_milli
        return round(
            (
                from_tank_milli * self.fuel_price_from_tank
                + to_buy_milli * self.fuel_price_at_origin
            )
            / FUEL_MILLI
        )

    @property
    def expected_profit(self) -> int:
        """Payments less the fuel burned and the maintenance expected."""
        return (
            self.advance_total
            + self.on_delivery_total
            - self.total_fuel_cost
            - self.expected_maintenance_cost
        )


@dataclass(frozen=True)
class _RepositionIntent:
    """An empty reposition chosen but not yet flown.

    ``origin`` is the planet the choice was made at, so the intent can be
    recognised as stale anywhere else. One record rather than a target and a
    separate marker keeps the two from disagreeing.
    """

    origin: Planet
    target: Planet


@dataclass
class _TripFuel:
    """Fuel logistics and safety of flying one (origin, destination) leg.

    Everything about the trip that does not depend on what it carries.
    :meth:`TraderBrain._trip_fuel_economics` computes it, and both a trade
    plan and a contract trip price their fuel from it, so the safety gates
    are stated once.
    """

    distance: float
    # Millifuel for one leg; see FUEL_MILLI.
    fuel_one_way: int
    # Local ask (or the avg-price fallback): what fuel bought here costs.
    fuel_price: int
    # Typical believable fuel price across the galaxy: what fuel already in
    # the tank is worth, and what replacing it normally costs.
    fuel_reference_price: int
    # Round-trip shortfall the ship buys here, in whole market units, at
    # ``fuel_price``.
    fuel_to_buy: int
    # Millifuel of the outbound leg's burn already aboard. The rest come out
    # of ``fuel_to_buy`` and are charged at the local ask.
    fuel_from_tank_one_way: int
    expected_maintenance_cost: int


@dataclass
class _PairEconomics:
    """Commodity-independent economics of one (origin, destination) pair.

    Everything a trade plan needs that does not depend on the commodity:
    geometry, fuel logistics and safety, and the trading budget. Computed
    once per pair by :meth:`TraderBrain._pair_economics` and reused for every
    commodity evaluated between the two planets.
    """

    distance: float
    # Millifuel, as in :class:`_TripFuel`; ``fuel_to_buy`` is market units.
    fuel_one_way: int
    fuel_price: int
    fuel_reference_price: int
    fuel_to_buy: int
    fuel_from_tank_one_way: int
    expected_maintenance_cost: int
    money_for_trading: int
    max_by_cargo: int


@dataclass
class _OriginAcquisition:
    """What one commodity costs to acquire at one origin planet.

    Commodity-specific but destination-independent, so
    :meth:`TraderBrain._best_plan_from` computes it once per commodity and
    reuses it across every candidate destination.
    """

    # Resting asks as (price, quantity), cheapest first: the units the ship
    # could lift right now, and at what price.
    ask_levels: List[Tuple[int, int]]
    # Recent clearing price at the origin, 0 when nothing is flowing. Units
    # beyond the resting asks have to be won out of the flow at this price.
    flow_price: int
    # Price the ship posts, max(best ask, flow price); see TradePlan.
    bid_price: int
    # Cheapest liftable unit price: the best resting ask when one exists,
    # else the flow price. Screens destination demand before the exact
    # quantity, and so the exact walked cost, is known.
    entry_price: int


class ShipStatus(enum.Enum):
    """Possible statuses for a ship."""

    DOCKED = "docked"
    TRAVELING = "traveling"
    NEEDS_MAINTENANCE = "needs_maintenance"


class ShipBrain:
    """Base class for ship decision making strategies."""

    def __init__(self, ship: "Ship") -> None:
        """Initialize the brain with a reference to its ship."""
        self.ship = ship

    def decide_trade_actions(self) -> None:
        """Decide what trade actions to take at the current planet."""
        raise NotImplementedError("Subclasses must implement this method")

    def decide_travel(self) -> Optional[Planet]:
        """Decide whether to travel to another planet, and if so, which one."""
        raise NotImplementedError("Subclasses must implement this method")

    def fuel_cargo_to_keep(self) -> int:
        """Hold fuel that is trade cargo and must stay out of the tank."""
        return 0

    def wants_refuel_stop(
        self,
        planet: Planet,
        destination: Planet,
        fuel_refund: int,
        fuel_shortfall: int,
        stop_turns: int,
    ) -> bool:
        """Whether to break the journey at ``planet`` to buy fuel.

        Called by :meth:`Ship.update_journey` for each intermediate planet the
        ship passes. ``fuel_refund`` is the unburned route fuel the stop would
        return to the tank, ``fuel_shortfall`` is how much less that is than
        the rest of the route needs, and ``stop_turns`` is what the stop
        costs; all three are computed from the route by the ship. The default
        is never to stop.
        """
        return False

    def wants_pickup_stop(
        self,
        planet: Planet,
        destination: Planet,
        fuel_refund: int,
        fuel_shortfall: int,
        stop_turns: int,
    ) -> bool:
        """Whether to break the journey at ``planet`` to take contracts aboard.

        Asked about the same nodes and with the same numbers as
        :meth:`wants_refuel_stop`, and one stop may be made for both reasons.
        The default is never to stop.
        """
        return False


class TraderBrain(ShipBrain):
    """Trader ship brain that maximizes profit.

    Plan-driven: evaluates complete buy, travel, sell opportunities before
    committing to purchases, and budgets round-trip fuel so the ship can
    return.
    """

    def __init__(self, ship: "Ship") -> None:
        """Initialize the trader brain."""
        super().__init__(ship)
        # The trip the ship is working on: a haul it is buying cargo for, or a
        # group of contracts it is flying for the payments.
        self._current_plan: Optional[Union[TradePlan, ContractPlan]] = None
        # True once the plan's cargo is aboard, fully or as a partial load
        # after patience ran out, and the ship should fly to the plan's
        # destination rather than keep buying or sell locally.
        self._plan_loaded = False
        # Docked turns left in the current plan phase, accumulating or loaded
        # but unable to depart, before the plan is abandoned.
        self._plan_turns_left = 0
        # Set while this turn's local sell orders are pending so decide_travel
        # does not depart and strand them in the book. It lasts one turn: see
        # _refresh_local_sale_staleness.
        self._selling_locally = False
        # Local-sale bookkeeping: the raw held units of each commodity listed
        # here last turn, and the planet they were listed at. Comparing held
        # units, not sellable ones, is what tells a fill from a change of
        # mind - sellable fuel moves with the local bid, not with fills.
        # _local_sale_stale means every listed good went a full turn untouched.
        self._local_sale_held: Dict[CommodityDefinition, int] = {}
        self._local_sale_planet_name = ""
        self._local_sale_stale = False
        # Fuel the trip this ship has committed its cargo to will need at
        # departure. Recomputed from scratch every docked turn, before any
        # fuel order is placed, so it can never speak for a planet or a plan
        # the ship has moved on from. Fuel for a committed profitable trip is
        # not speculative bunkering, so the top-up buys it even at a scarcity
        # price.
        self._committed_fuel_need = 0
        # Commodities bid for as add-on cargo to the current plan's
        # destination; skipped by the local sell step while accumulating.
        self._addon_commodities: set[CommodityDefinition] = set()
        # Shared per-simulation geometry and fuel-reachability cache.
        self._nav: Navigator = get_navigator(ship.simulation)
        # Memo of the last full plan search: (turn, planet, result). The
        # search runs in decide_trade_actions and, when it found nothing,
        # again in decide_travel the same turn; the memo answers the second
        # call without re-surveying the galaxy.
        self._plan_search_memo: Optional[tuple[int, Planet, Optional[TradePlan]]] = None
        # Consecutive turns the ship has been docked, empty, planless and too
        # poor to fund a short round trip. Past DISTRESS_PATIENCE the ship is
        # distressed and may sell tank fuel down to its survival target.
        self._distress_turns = 0
        # How many times this ship has entered distress. Exposed for analysis;
        # a rising count across the fleet means capital is mis-sized.
        self._distress_entries = 0
        # Empty reposition the ship has chosen but cannot fly yet because the
        # fuel for it is still in the local ask book rather than the tank.
        # Held across turns so decide_trade_actions can fund it through
        # ``_committed_fuel_need``, and kept while it stays viable so the
        # ship does not re-aim at a new target every turn it waits for fuel;
        # dropped as soon as the ship is anywhere other than the planet the
        # choice was made at.
        self._reposition_intent: Optional[_RepositionIntent] = None
        # Planet where this stop's one linger-to-bunker turn has been spent,
        # and whether the previous docked turn was that linger. The planet
        # caps a stop at one extra turn; it is cleared as soon as the ship is
        # docked anywhere else. The flag is consumed at the top of the next
        # decide_trade_actions, where it keeps the linger from counting
        # against a loaded plan's departure patience.
        self._fuel_linger_planet: Optional[Planet] = None
        self._fuel_linger_pending = False
        # Value of one turn of the trip the ship last departed on, recorded by
        # decide_travel whenever it returns a destination. An en-route refuel
        # stop weighs its fuel saving against this, and the ship is in flight
        # by then, so the number has to be remembered rather than recomputed.
        # Zero means an empty reposition, which loses nothing by stopping.
        self._departed_trip_turn_value = 0.0
        # Station this ship is flying to purely to refuel, chosen once per
        # docked turn in decide_trade_actions and reused by decide_travel so
        # the two agree. None means no such reposition this turn.
        self._refuel_reposition: Optional[Planet] = None
        # Cost basis of the hold: commodity id -> (units, credits paid), fed
        # from this ship's own fills. Fuel is excluded; the tank is priced
        # against the galaxy reference, not against what it cost.
        self._cost_basis: Dict[str, Tuple[int, int]] = {}
        # Highest transaction id already read off each planet's per-actor
        # history, keyed by planet name. Ids, not indices: the market trims
        # histories from the front. See core/brains/dealer.ingest_fills.
        self._basis_cursors: Dict[str, int] = {}
        # Docked turns each listed commodity has gone unfilled, and the units
        # held when it was listed, which is what a fill is judged against.
        self._unsold_turns: Dict[str, int] = {}
        self._listed_held: Dict[str, int] = {}
        # Cargo hops each held commodity has made. Reset when the hold runs
        # out of it.
        self._cargo_hops: Dict[str, int] = {}
        # Planet where the cargo was last held for another market, and the
        # consecutive docked turns the departure gate has refused since. Past
        # HOLD_PATIENCE the cargo is listed here instead.
        self._hold_planet: Optional[Planet] = None
        self._hold_refused_turns = 0
        # Consecutive docked turns the ship has held no plan of either kind.
        # Past REPOSITION_CONTRACT_PATIENCE a reposition toward a contract
        # queue is worth flying on the payments alone; see
        # _find_reposition_target.
        self._turns_without_plan = 0

    def _recent_flow_per_turn(
        self, market: "Market", commodity: CommodityDefinition
    ) -> float:
        """Mean traded units per turn over the recent volume window."""
        volumes = market.volume_history.get(commodity, [])[-FLOW_RECENCY_TURNS:]
        return sum(volumes) / len(volumes) if volumes else 0.0

    def _flow_value(
        self, market: "Market", commodity: CommodityDefinition
    ) -> Optional[int]:
        """Recent clearing price, or None when the good has never traded here.

        The avg-price default of 10 for a never-traded good is fabricated and
        must not back a valuation.
        """
        if not market.has_price_signal(commodity):
            return None
        return market.get_avg_price(commodity)

    def _get_tradeable_commodities(self) -> List[CommodityDefinition]:
        """Commodities that can be traded between planets."""
        return self._nav.tradeable_commodities()

    def _fuel_commodity(self) -> Optional[CommodityDefinition]:
        """The nova_fuel commodity, or None if it is not defined."""
        return self._nav.fuel_commodity()

    def _fuel_purchasable_at(self, planet: Planet) -> bool:
        """Whether nova_fuel can be lifted from ``planet``'s book right now."""
        return self._nav.fuel_purchasable_at(planet)

    def _refuel_need_at(self, planet: Planet) -> int:
        """Fuel a ship sitting at ``planet`` must be able to obtain to stay free.

        The leg to the nearest other fuel-selling planet, or, when there is
        no other, the shortest lane hop out. A market that cannot supply
        this much is not an escape route, however cheap its top ask looks.
        """
        escape_fuel = self._min_escape_fuel(planet)
        if escape_fuel is not None:
            return escape_fuel
        nearest = self._nav.nearest_other_distance(planet)
        if nearest is None:
            return 0
        return self.ship.fuel_required(nearest)

    def _min_escape_fuel(self, from_planet: Planet) -> Optional[int]:
        """Fuel needed to reach the nearest fuel station from ``from_planet``.

        Returns None if no other planet in the galaxy is a station. The
        measure is to a station, not to any resting ask: a one-unit trickle
        ask cannot refill a tank, so escaping to it leaves the ship in the
        same position one hop further on. Fuel burn is monotone in distance,
        so the navigator's nearest station minimizes this ship's escape cost
        too.
        """
        distance = self._nav.nearest_fuel_station_distance(from_planet)
        if distance is None:
            return None
        return self.ship.fuel_required(distance)

    def _repair_kit_tier(self) -> Optional[Tuple[str, int, str]]:
        """The complete maintenance tier the ship would repair with, if any.

        Best quality first, matching :meth:`Ship.perform_maintenance`, so the
        tier named here is the one a repair would actually consume. Returns
        None when the hold carries no complete tier.
        """
        registry = self.ship.simulation.commodity_registry
        for tier in MAINTENANCE_KIT_TIERS:
            commodity = registry.get_commodity(tier[0])
            if commodity is None:
                continue
            if self.ship.cargo.get_quantity(commodity) >= tier[1]:
                return tier
        return None

    def _holds_repair_kit(self) -> bool:
        """Whether the hold carries a complete maintenance tier."""
        return self._repair_kit_tier() is not None

    def _maintenance_fuel_buffer(self) -> int:
        """Tank millifuel a maintenance roll could burn before the next departure.

        Zero while the ship carries a repair kit, because the cargo tiers are
        tried before the fuel tier. Otherwise MAINTENANCE_FUEL_UNITS, the
        legacy tier's cost, which is charged on top of every arrival reserve
        so a repair cannot leave the ship below the leg it was funded for.
        """
        return 0 if self._holds_repair_kit() else units_to_milli(MAINTENANCE_FUEL_UNITS)

    def _arrival_fuel_requirement(
        self, destination: Planet, return_planet: Planet
    ) -> int:
        """Fuel that must remain on arrival for ``destination`` to be safe.

        The single source of truth behind :meth:`_fuel_safe_destination`, so
        callers that have to *fund* a trip ask the same question the gate
        that approves it asks, instead of guessing.

        The requirement is normally the escape leg to the nearest fuel
        station, plus :meth:`_maintenance_fuel_buffer`. The buffer covers the
        legacy maintenance tier, which burns MAINTENANCE_FUEL_UNITS of tank
        fuel on the turn after a departure roll: a ship funded to exactly its
        escape leg lands, repairs, and is then below the leg it was funded
        for. A ship carrying a repair kit needs no buffer.

        Demanding that leg unconditionally is self-ratcheting. A ship funded
        to exactly its escape leg can never spend it, because the hop to the
        fuel seller demands *that* seller's own escape leg on arrival. Ships
        sat for hundreds of turns with money in hand and a live fuel market
        three units away, vetoed by a floor they were already sitting on.

        So the requirement drops to zero when the destination is itself a
        fuel station (:meth:`Navigator.fuel_station_at`): depth enough to
        refill a tank at a price near the galaxy reference. Any burn there is
        re-buyable. Weaker evidence does not qualify. A resting ask paired
        with recent trading used to waive the leg, and ships landed on the
        strength of it with no ask left by arrival: 39% of spiked fuel spend
        came from ships in that state.

        Only when the galaxy holds no station at all is there no escape leg
        to demand. Grounding the whole fleet would then be worse than the
        risk, so the requirement falls back to the return leg to
        ``return_planet``, which the destination's own ask depth may still
        offset.
        """
        escape_fuel = self._min_escape_fuel(destination)
        if escape_fuel is not None:
            if self._nav.fuel_station_at(destination):
                return 0
            return escape_fuel + self._maintenance_fuel_buffer()
        requirement = self.ship.fuel_required(
            self._nav.distance(destination, return_planet)
        )
        if self._fuel_purchasable_at(destination):
            depth_milli = units_to_milli(self._nav.fuel_ask_depth_at(destination))
            unsupplied = self._refuel_need_at(destination) - depth_milli
            requirement = min(requirement, max(0, unsupplied))
        return requirement

    def _fuel_safe_destination(
        self, destination: Planet, return_planet: Planet, fuel_after_arrival: int
    ) -> bool:
        """Whether flying to ``destination`` leaves the ship an escape route."""
        return fuel_after_arrival >= self._arrival_fuel_requirement(
            destination, return_planet
        )

    def _departure_fuel_requirement(self, destination: Planet) -> int:
        """Fuel the departure gate will demand before flying to ``destination``.

        The one-way leg plus the escape fuel the destination requires on
        arrival, capped by the tank. :meth:`decide_travel` departs exactly
        when the tank holds this much, so a hold decision that judges reach
        by this number and a top-up that buys up to it agree with the gate by
        construction. Committing to the bare leg was what left ships holding
        cargo forever: they bought one leg's worth, and the gate then refused
        them for the arrival reserve they had never funded. Because the
        arrival reserve comes from one place, hold-cargo trips, plan
        departures and empty repositions all fund the same number, and
        :meth:`_opportunistic_fuel_topup` buys up to it via
        ``_committed_fuel_need``.
        """
        origin = self.ship.planet
        if origin is None:
            return 0
        leg = self.ship.fuel_required(self._nav.distance(origin, destination))
        return min(
            self.ship.fuel_capacity,
            leg + self._arrival_fuel_requirement(destination, origin),
        )

    def _fuel_reserve_need(self) -> int:
        """Round-trip fuel to the nearest other planet.

        Below this the ship risks stranding: it cannot make even the shortest
        round trip.
        """
        current_planet = self.ship.planet
        if current_planet is None:
            return 0
        nearest = self._nav.nearest_other_distance(current_planet)
        if nearest is None:
            return 0
        return 2 * self.ship.fuel_required(nearest)

    def is_stranded(self) -> bool:
        """Whether this ship is docked, fuel-short, and has no local ask.

        True when the ship is docked (not traveling), holds less fuel than
        its round-trip reserve need, and its current planet has no live
        nova_fuel ask to buy up from. Used by the KPI summary to count ships
        at risk of being unable to leave.
        """
        planet = self.ship.planet
        if planet is None or self.ship.destination is not None:
            return False
        if self.ship.fuel >= self._fuel_reserve_need():
            return False
        return not self._nav.fuel_purchasable_at(planet)

    def _fuel_mobility_reserve(self) -> int:
        """Tank level that keeps the ship able to leave from here.

        Covers both the shortest round trip and the leg to the nearest planet
        where fuel is purchasable; they can differ on fuel-poor planets.
        """
        current_planet = self.ship.planet
        if current_planet is None:
            return 0
        reserve = self._fuel_reserve_need()
        escape_fuel = self._min_escape_fuel(current_planet)
        if escape_fuel is not None:
            reserve = max(reserve, escape_fuel)
        return reserve

    def _affordable_local_fuel(self, planet: Planet) -> int:
        """Whole market units of fuel the ship could buy at ``planet`` now.

        Callers that add this to a tank level must convert it with
        :func:`units_to_milli`.

        The reach of a docked ship is the tank plus this: ships run near the
        survival target, so judging any trip on tank fuel alone rejects trips
        the ship could trivially fund from the book in front of it. Bounded
        by 90% of money at the local ask - the same operating buffer every
        other buy keeps - and the tank's remaining capacity.
        Zero where fuel is not purchasable.
        """
        ship = self.ship
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None or not self._fuel_purchasable_at(planet):
            return 0
        _, ask = planet.market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask <= 0:
            return 0
        tank_room_units = milli_to_units_floor(ship.fuel_capacity - ship.fuel)
        return max(0, min(tank_room_units, int(ship.money * 0.9) // ask))

    def _fuel_survival_target(self) -> int:
        """Tank millifuel to keep on hand to stay mobile.

        The larger of two shortest round trips and the escape leg to a
        refueling planet, capped by the tank. This is the level worth paying
        scarcity prices for; anything beyond it is bunkering, only worth
        doing when fuel is cheap.
        """
        return min(
            self.ship.fuel_capacity,
            max(2 * self._fuel_reserve_need(), self._fuel_mobility_reserve()),
        )

    def _fuel_escape_target(self) -> Optional[int]:
        """Tank level that buys a way off this planet and no more, or None.

        The rationed alternative to :meth:`_fuel_survival_target` when the
        local ask is spiked: the leg to the nearest fuel station plus that
        station's own arrival floor, so the ship is not merely
        moved to a second trap. It is exactly the number
        :meth:`_departure_fuel_requirement` would demand for that hop, which
        is the number the departure gate checks, so a ship that funds this
        can actually leave.

        Returns None when there is no such hop the arrival floor approves
        (typically because the requirement exceeds the tank). Callers must
        then buy the full survival target: overpaying beats dying in place.

        Only the nearest station is considered. Fuel burn is monotone in
        distance, so a farther station costs strictly more leg fuel, and the
        point of rationing is to spend as little as possible at a spiked ask.
        """
        origin = self.ship.planet
        if origin is None:
            return None
        for candidate in self._nav.planets_by_proximity(origin):
            if not self._nav.fuel_station_at(candidate):
                continue
            target = self._departure_fuel_requirement(candidate)
            leg = self.ship.fuel_required(self._nav.distance(origin, candidate))
            if self._fuel_safe_destination(candidate, origin, target - leg):
                return target
            return None
        return None

    @property
    def is_distressed(self) -> bool:
        """Whether the ship has been idle and cash-starved long enough to act.

        See DISTRESS_PATIENCE. Distress only widens what the ship may sell;
        it never relaxes a fuel-safety gate.
        """
        return self._distress_turns >= DISTRESS_PATIENCE

    def _short_trip_cash_floor(self) -> int:
        """Cash needed to fund the shortest round trip's fuel from here.

        Priced at the local fuel market, ignoring the tank: a ship whose
        whole purse is worth less than one short trip of fuel cannot fund a
        trade even with a full tank, because cargo must be paid for too.
        With no local price at all, falls back to the galaxy's typical
        believable fuel price rather than its cheapest, so the floor
        reflects what fuel really costs.
        """
        planet = self.ship.planet
        fuel_commodity = self._fuel_commodity()
        if planet is None or fuel_commodity is None:
            return 0
        _, ask = planet.market.get_bid_ask_spread(fuel_commodity)
        price = (
            ask if ask is not None else self._flow_value(planet.market, fuel_commodity)
        )
        if price is None or price <= 0:
            reference = self._fuel_value_reference()
            price = math.ceil(reference) if reference else FUEL_BID_FALLBACK_FLOOR
        return milli_to_units_ceil(self._fuel_reserve_need()) * price

    def _update_distress(self, idle_and_broke: bool) -> None:
        """Advance or clear the distress counter for this docked turn.

        Entering and leaving are deliberately different tests, and so are
        entering and *staying*. ``idle_and_broke`` only gates the first turn
        of the count: a ship starts the clock when it is idle *and* poorer
        than one short round trip of fuel, but once the count is running it
        advances every docked turn the purse stays under that floor, whether
        or not the ship is idle. Winning cargo is not the same as being able
        to fund a trip, and the count must not restart every time a bid
        fills. It leaves only when its purse clears the floor again. Clearing
        on cargo alone was the bug: a distressed ship that won a few units
        into its hold dropped straight out of distress while still unable to
        fund a trip, lost the wider selling and margin rules that were about
        to get it moving, and fell back in a turn later. Cash above the floor
        is the only evidence that the ship can trade unaided.
        """
        if self.ship.money >= self._short_trip_cash_floor():
            self._distress_turns = 0
            return
        if self._distress_turns == 0 and not idle_and_broke:
            # Below the floor but still working a hold of cargo: not idle, so
            # the clock has not started.
            return
        self._distress_turns += 1
        if self._distress_turns == DISTRESS_PATIENCE:
            self._distress_entries += 1

    def _fuel_value_reference(self) -> Optional[float]:
        """Typical believable fuel valuation across the galaxy.

        The median of the per-planet believable valuations, not the galaxy
        minimum. See :meth:`Navigator.fuel_value_reference` for the
        rationale.
        """
        return self._nav.fuel_value_reference()

    def fuel_cargo_to_keep(self) -> int:
        """Hold fuel that is a delivery plan's load, kept out of the tank.

        At either end of a nova_fuel plan the plan's quantity is trade
        cargo. Everywhere else every unit of hold fuel is pumped into the
        tank by :meth:`Ship.pump_fuel` before the turn's decisions run.
        """
        plan = self._current_plan
        if not isinstance(plan, TradePlan) or plan.commodity.id != "nova_fuel":
            return 0
        planet = self.ship.planet
        if planet is plan.origin or planet is plan.destination:
            return plan.quantity
        return 0

    def _sellable_quantity(self, commodity: CommodityDefinition) -> int:
        """Hold units of ``commodity`` the ship may treat as trade goods.

        The tank is not part of the hold, so this is mostly what the hold
        carries. Fuel in the hold is either a delivery plan's load or the
        remainder the pump could not fit, and both are for sale.

        One repair kit is held back. Maintenance goods the ship needs to
        repair itself are equipment, not trade cargo: selling them puts the
        arrival reserve back up by MAINTENANCE_FUEL_UNITS, which costs more
        fuel than the kit is worth. Only the tier the ship would repair with
        is reserved, so a second tier bought as cargo still sells.
        """
        held = max(0, self.ship.cargo.get_quantity(commodity))
        if held and commodity.id in MAINTENANCE_KIT_IDS:
            kit = self._repair_kit_tier()
            if kit is not None and kit[0] == commodity.id:
                held = max(0, held - kit[1])
        return held

    def _refresh_local_sale_staleness(self) -> None:
        """Judge whether last turn's local asks went a full turn unfilled.

        Call once per docked turn, after resting orders are cancelled and
        before new ones are placed, so the cargo counted here is the whole
        hold rather than the part not tied up in an order.

        The one-turn sell veto exists so a ship does not depart and abort the
        sale it just placed. Left unqualified it became permanent: the ask
        rested at a price nobody took, the ship never departed, and it could
        not replan while holding cargo. A sale that survived a full turn with
        nothing filled is stale, and the veto no longer applies to it. Any
        fill, a move to another planet, or an empty hold clears the flag.

        Staleness is judged per listed commodity against the raw units held
        when the asks went in, so only a fill can clear it. Judging it on the
        sellable total instead let unrelated events flip the verdict: a
        rescue bid appearing in the local fuel book turns tank fuel into
        sellable cargo, which is not a fill and must not reset the clock.
        """
        planet = self.ship.planet
        listed = self._local_sale_held
        if not listed or planet is None or self._local_sale_planet_name != planet.name:
            self._local_sale_stale = False
            self._clear_local_sale_record()
            return
        filled = any(
            self.ship.cargo.get_quantity(commodity) < held_at_listing
            for commodity, held_at_listing in listed.items()
        )
        self._local_sale_stale = not filled
        if filled:
            # Something filled: the book is working, start the clock over.
            self._clear_local_sale_record()

    def _clear_local_sale_record(self) -> None:
        """Forget the local asks being tracked for staleness."""
        self._local_sale_held = {}
        self._local_sale_planet_name = ""

    def _record_local_sale(
        self, planet: Planet, commodities: Sequence[CommodityDefinition]
    ) -> None:
        """Note the asks just placed at ``planet`` so next turn can judge fills."""
        self._local_sale_held = {
            commodity: self.ship.cargo.get_quantity(commodity)
            for commodity in commodities
        }
        self._local_sale_planet_name = planet.name

    def _ingest_cost_basis(self) -> None:
        """Read this ship's new fills at the current planet into the basis.

        The cursor is the highest transaction id already seen, not an index:
        the market trims each per-actor history from the front, so an index
        cursor would skip fills or replay them. This is the cursor pattern
        :func:`core.brains.dealer.ingest_fills` documents; the loop is written
        out here because the basis needs the fills in the order they happened
        and needs each fill's total amount.

        Call once per docked turn, before any decision reads the basis. A fill
        on either side also restarts that commodity's unsold clock: the book
        is working, so the price it is listed at does not need marking down.
        """
        ship = self.ship
        planet = ship.planet
        if planet is None:
            return
        history = planet.market.get_actor_transaction_history(ship)
        cursor = self._basis_cursors.get(planet.name, 0)
        start = len(history)
        while start > 0 and history[start - 1].transaction_id > cursor:
            start -= 1
        for transaction in history[start:]:
            if transaction.transaction_id > cursor:
                cursor = transaction.transaction_id
            commodity_id = transaction.commodity_type.id
            if commodity_id == "nova_fuel":
                continue
            if transaction.buyer is ship:
                units, credits = self._cost_basis.get(commodity_id, (0, 0))
                self._cost_basis[commodity_id] = (
                    units + transaction.quantity,
                    credits + transaction.total_amount,
                )
            elif transaction.seller is ship:
                self._reduce_cost_basis(commodity_id, transaction.quantity)
            else:
                continue
            self._unsold_turns.pop(commodity_id, None)
        self._basis_cursors[planet.name] = cursor

    def _reduce_cost_basis(self, commodity_id: str, quantity: int) -> None:
        """Remove ``quantity`` units from a basis entry at its average cost."""
        units, credits = self._cost_basis.get(commodity_id, (0, 0))
        if units <= 0:
            return
        remaining = units - quantity
        if remaining <= 0:
            del self._cost_basis[commodity_id]
            return
        self._cost_basis[commodity_id] = (
            remaining,
            int(round(credits * remaining / units)),
        )

    def _forget_empty_cargo_state(self) -> None:
        """Drop per-commodity state for goods the hold no longer carries."""
        for commodity in self._get_tradeable_commodities():
            if self.ship.cargo.get_quantity(commodity) > 0:
                continue
            self._cost_basis.pop(commodity.id, None)
            self._cargo_hops.pop(commodity.id, None)
            self._unsold_turns.pop(commodity.id, None)

    def _age_unsold_listings(self) -> None:
        """Count another unfilled turn for each commodity listed last turn.

        Judged on raw held units, the same test :meth:`_refresh_local_sale_staleness`
        uses: only a fill reduces them.
        """
        listed = self._listed_held
        self._listed_held = {}
        registry = self.ship.simulation.commodity_registry
        for commodity_id, held_at_listing in listed.items():
            commodity = registry.get_commodity(commodity_id)
            if commodity is None:
                continue
            if self.ship.cargo.get_quantity(commodity) >= held_at_listing:
                self._unsold_turns[commodity_id] = (
                    self._unsold_turns.get(commodity_id, 0) + 1
                )

    def _note_listed(self, commodities: Sequence[CommodityDefinition]) -> None:
        """Record the units held when these asks went in, for the unsold clock."""
        self._listed_held = {
            commodity.id: self.ship.cargo.get_quantity(commodity)
            for commodity in commodities
        }

    def basis_per_unit(self, commodity: CommodityDefinition) -> Optional[float]:
        """Credits per unit this ship paid for the cargo it holds, if known.

        None when the cargo was aboard before the basis was tracked, or was
        never bought on a market. Fuel is never tracked.
        """
        units, credits = self._cost_basis.get(commodity.id, (0, 0))
        if units <= 0:
            return None
        return credits / units

    def _sell_floor_price(self, commodity: CommodityDefinition) -> int:
        """Lowest price the ship will list ``commodity`` at locally.

        Cargo with a known cost basis floors at what it cost, marked down by
        UNSOLD_DECAY for every docked turn it has sat listed and unfilled and
        never below LIQUIDATION_FLOOR of cost. Selling at cost is the goal;
        holding a load nobody buys at cost is worse than taking the markdown,
        because the hold space and the fuel spent carrying it around cost
        more than the difference. Cargo of unknown basis has no floor.

        Fuel floors at the galaxy fuel reference price, the median believable
        per-planet valuation, which is what the ship pays to refill the tank.
        Listing below it books a guaranteed loss on the round trip, and on a
        planet that has never traded fuel the resting price came out at 1
        credit, which is also what pinned :meth:`Navigator.cheapest_fuel_ask`
        and opened the sell gate for the whole fleet. Before any fuel has
        traded anywhere the reference is unknown and the floor is
        FUEL_BID_FALLBACK_FLOOR, the same fabricated-price guard the rest of
        the fuel code uses.
        """
        floor = 0
        basis = self.basis_per_unit(commodity)
        if basis is not None:
            markdown = max(
                LIQUIDATION_FLOOR,
                1.0 - UNSOLD_DECAY * self._unsold_turns.get(commodity.id, 0),
            )
            floor = int(basis * markdown)
        if commodity.id == "nova_fuel":
            reference = self._fuel_value_reference()
            fuel_floor = (
                FUEL_BID_FALLBACK_FLOOR
                if reference is None
                else max(1, math.ceil(reference))
            )
            floor = max(floor, fuel_floor)
        return floor

    def _place_flow_sell_orders(
        self, market: "Market", commodity: CommodityDefinition, quantity: int
    ) -> List[str]:
        """Sell ``quantity`` into both the resting bids and the flow.

        Matching executes at the sell order's price, so a single ask at the
        top bid liquidates the entire load at that one price, even into a
        1-credit probe bid. Instead, every resting bid level the floor allows
        is captured by an ask priced at that level, and the remainder rests
        where it can still be taken: at the best bid, or at the haircut flow
        price when that is lower.

        The remainder used to rest at the flow price whatever the book said.
        Measured over 300 turns at 100 planets, 82% of unfilled ship listings
        rested at 1.78x the best resting bid, and 42% of listed units never
        filled at all. An ask above every live bid is a forecast, not a sale.

        The floor is what the cargo cost, marked down as it goes unsold; see
        :meth:`_sell_floor_price`. It is the one thing that can put an ask
        above the flow price, and it does so only for fuel.

        Returns action strings for the placed orders.
        """
        actions: List[str] = []
        flow_value = self._flow_value(market, commodity)
        flow_px = int(flow_value * SELL_PRICE_HAIRCUT) if flow_value else 0
        floor_px = self._sell_floor_price(commodity)
        levels = market.get_bid_levels(commodity)
        best_bid = levels[0][0] if levels else 0
        remaining = quantity
        for price, level_qty in levels:
            if remaining <= 0 or price < floor_px:
                break
            take = min(level_qty, remaining)
            order_id = market.place_sell_order(self.ship, commodity, take, price)
            if order_id:
                actions.append(f"Selling {take} {commodity.name} at {price}")
                self.ship.active_orders[order_id] = f"sell {commodity.id}"
                remaining -= take
        if remaining > 0:
            if best_bid > 0 and flow_px > 0:
                rest_price = min(flow_px, best_bid)
            elif flow_px > 0:
                rest_price = flow_px
            else:
                rest_price = max(1, best_bid or market.get_avg_price(commodity) or 1)
            rest_price = max(rest_price, floor_px)
            order_id = market.place_sell_order(
                self.ship, commodity, remaining, rest_price
            )
            if order_id:
                actions.append(f"Offering {remaining} {commodity.name} at {rest_price}")
                self.ship.active_orders[order_id] = f"sell {commodity.id}"
        return actions

    def _realizable_value(
        self, market: "Market", commodity: CommodityDefinition, quantity: int
    ) -> int:
        """What selling ``quantity`` units into ``market`` would actually fetch.

        The resting bid levels are walked from the top for as many units as
        they hold; whatever is left is valued at the price
        :meth:`_place_flow_sell_orders` would rest it at, the lower of the
        haircut flow price and the best bid. A market with neither a bid nor a
        flow price values the remainder at nothing.

        The old valuation, ``max(best bid, flow) * quantity``, paid the top bid
        for every unit and ignored depth, so it valued a ten-unit load at ten
        times a one-unit bid. That is the number the ship then flew to.
        """
        if quantity <= 0:
            return 0
        levels = market.get_bid_levels(commodity)
        best_bid = levels[0][0] if levels else 0
        flow_value = self._flow_value(market, commodity)
        flow_px = int(flow_value * SELL_PRICE_HAIRCUT) if flow_value else 0
        remaining = quantity
        total = 0
        for price, level_qty in levels:
            if remaining <= 0:
                break
            take = min(level_qty, remaining)
            total += take * price
            remaining -= take
        if remaining > 0:
            rest_price = min(flow_px, best_bid) if best_bid else flow_px
            total += remaining * rest_price
        return total

    def _opportunistic_fuel_topup(
        self, pending_fuel: int = 0, required_floor: int = 0
    ) -> Optional[str]:
        """Buy fuel with leftover cargo space and money, price-aware.

        When the local ask is within FUEL_BUNKER_PREMIUM of the galaxy's
        typical believable fuel price, the median over planets, bunker
        toward a full tank. When it is priced well above that median, buy
        only up to the survival target, raised to cover the destination the
        ship has committed its cargo to; filling tanks at spike prices
        bankrupts ships.

        The order is posted at max(local ask, recent clearing price), so it
        also wins units out of the turn's flow of fresh asks rather than only
        the leftovers resting in the book. The bunker budget has two tiers:
        FUEL_BUNKER_BUDGET_FRACTION_CHEAP of cash when the ask is at or below
        the galaxy reference, FUEL_BUNKER_BUDGET_FRACTION when it is above the
        reference but still inside the premium.

        Above the premium the ship buys nothing at all when a fuel station is
        within tank range: :meth:`_refuel_reposition_target` flies it there
        instead, where the same money buys depth at the reference price. Only
        a trapped ship, or one mid refuel stop, still buys, and its bid is
        capped at :meth:`Navigator.fuel_bid_ceiling` so it cannot pay its own
        escalation.

        Above the premium the survival portion is rationed further, to
        :meth:`_fuel_escape_target`: enough to reach the nearest planet that
        sells fuel and still clear its arrival floor, rather than two round
        trips' worth. The survival top-up was the last unguarded fuel
        purchase in the ship, spending ``money * 0.9`` at any ask; measured
        over 300 turns it was where insolvent ships lost their capital,
        paying 88 credits a unit against a reference near 54, with spiked
        buys alone exceeding their starting purse. Buying the way out and
        refuelling properly at the next market costs strictly less than
        stockpiling here at spike prices.

        Two guards keep this from grounding the fleet the way rationing
        *plan* fuel to one leg did. Committed-destination fuel is exempt, so
        a ship with a profitable trip still funds the whole trip. And the
        escape hop must clear :meth:`_fuel_safe_destination` with the
        rationed tank; if no hop does, the full survival target is bought
        anyway, since overpaying beats stranding. A ship that would rather
        wait for a cheaper price still has :meth:`_post_standing_fuel_bid`.

        ``pending_fuel`` counts fuel buy orders already placed this turn,
        whose units arrive at end-of-turn matching, so a top-up never
        double-buys the plan's fuel. Order placement reserves funds, so
        ``ship.money`` already excludes the plan's committed money. Fuel
        does not take hold space: it fills into the hold and is pumped into
        the tank next turn.

        ``pending_fuel`` and the order quantity are whole market units; every
        tank target here is millifuel.

        ``required_floor`` is a tank level (millifuel) the caller treats as
        non-negotiable whatever the price, alongside the survival and
        committed-trip targets. An en-route refuel stop passes the millifuel
        the rest of its route needs, so the rounding the split costs is
        bought rather than bunkered.
        """
        ship = self.ship
        planet = ship.planet
        if planet is None:
            return None
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return None
        market = planet.market
        _, fuel_ask = market.get_bid_ask_spread(fuel_commodity)
        if fuel_ask is None or fuel_ask <= 0:
            return None

        current_milli = ship.fuel + units_to_milli(pending_fuel)

        # Post above the resting ask when the recent clearing price is higher,
        # the rule cargo bids already use (TradePlan.bid_price_per_unit).
        # Matching executes at the seller's ask and refunds the difference, so
        # the extra costs nothing on the units resting now and wins units out
        # of the turn's flow of fresh asks. Money is reserved at the bid, so
        # every quantity below is sized on the bid, not the ask.
        fuel_bid = max(fuel_ask, self._flow_value(market, fuel_commodity) or 0)

        reference = self._fuel_value_reference()
        # The local ask is itself a galaxy signal, so reference is never None
        # here; the guard inside the helper is defensive.
        bunkering = self._local_fuel_bunkerable(planet)
        # Cheapness is judged on the ask, which is what the fill actually
        # costs, rounded the same way the premium gate rounds.
        cheap = reference is not None and fuel_ask <= math.ceil(reference)

        # Fuel the ship must have to fly the destination it is holding cargo
        # for is as non-negotiable as the survival minimum: the hold-or-sell
        # comparison already proved that trip profitable at this ask, so it
        # is not speculative bunkering and the premium gate does not apply.
        survival_target = self._fuel_survival_target()
        if not bunkering:
            # Spiked ask. A ship that can fly to a fuel station buys nothing
            # here: the reposition in decide_travel takes it to depth at the
            # reference price, and any unit bought at this ask is money the
            # trip needs. A ship mid refuel stop is not repositioned, so it
            # still buys, at the capped price. A trapped ship, one with no
            # station in tank range, buys the rationed minimum and no bunker.
            if ship.stop_resume is None and self._can_reach_station():
                return None
            fuel_bid = min(fuel_bid, self._nav.fuel_bid_ceiling())
            escape_target = self._fuel_escape_target()
            if escape_target is not None:
                survival_target = min(survival_target, escape_target)
        required_target = min(
            ship.fuel_capacity,
            max(survival_target, self._committed_fuel_need, required_floor),
        )
        # The target is millifuel; the market sells whole units, so round the
        # shortfall up to the unit that covers it.
        required_units = min(
            milli_to_units_ceil(required_target - current_milli),
            int(ship.money * 0.9) // fuel_bid,
        )
        required_units = max(0, required_units)

        bunker_units = 0
        if bunkering:
            budget_fraction = (
                FUEL_BUNKER_BUDGET_FRACTION_CHEAP
                if cheap
                else FUEL_BUNKER_BUDGET_FRACTION
            )
            bunker_budget = (
                int(ship.money * budget_fraction) - required_units * fuel_bid
            )
            room_after_required = (
                ship.fuel_capacity - current_milli - units_to_milli(required_units)
            )
            bunker_units = min(
                milli_to_units_floor(room_after_required),
                max(0, bunker_budget) // fuel_bid,
            )
            bunker_units = max(0, bunker_units)

        quantity = required_units + bunker_units
        if quantity <= 0:
            return None

        order_id = market.place_buy_order(ship, fuel_commodity, quantity, fuel_bid)
        if not order_id:
            return None
        ship.active_orders[order_id] = "buy fuel"
        return order_id

    def _local_fuel_bunkerable(self, planet: Planet) -> bool:
        """Whether ``planet``'s fuel ask is cheap enough to buy freely at.

        True when a fuel ask rests here at no more than FUEL_BUNKER_PREMIUM
        times the galaxy reference. False covers both an absent ask and a
        spiked one, which are the two cases a ship should leave rather than
        pay: :meth:`_opportunistic_fuel_topup` rations against this test and
        :meth:`_refuel_reposition_target` moves the ship on it.
        """
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return False
        _, ask = planet.market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask <= 0:
            return False
        reference = self._fuel_value_reference()
        if reference is None:
            return False
        return ask <= math.ceil(reference * FUEL_BUNKER_PREMIUM)

    def _reachable_stations(self) -> List[Planet]:
        """Fuel stations elsewhere the tank can already reach, nearest first.

        A station passes when the tank alone covers
        :meth:`_departure_fuel_requirement`, which for a station is just the
        leg: a station has depth at the reference price, so it demands no
        arrival reserve. No local fuel is counted, because the point of the
        list is what the ship can do without buying at the local ask.
        """
        origin = self.ship.planet
        if origin is None:
            return []
        return [
            station
            for station in self._nav.planets_by_proximity(origin)
            if station is not origin
            and self._nav.fuel_station_at(station)
            and self.ship.fuel >= self._departure_fuel_requirement(station)
        ]

    def _can_reach_station(self) -> bool:
        """Whether the tank alone reaches a fuel station somewhere else."""
        return bool(self._reachable_stations())

    def _refuel_reposition_target(self) -> Optional[Planet]:
        """Station to fly to instead of buying fuel at a spiked local ask.

        Returns a planet when the ship is docked outside a refuel stop, the
        current planet is not a station, the local ask is absent or above the
        bunkering premium, the ship is short of fuel for what it has
        committed to, and a station is in tank range. Measured over 400
        turns, 46% of the fleet's spiked fuel spend came from ships in
        exactly this state: they could already fly to a fuel seller under the
        departure gate and posted a bid instead.

        Among the reachable stations, the one worth most: the hold's value
        there at bid-or-flow prices, less the leg's fuel at the galaxy
        reference. An empty hold and ties go to the nearest, which is the
        cheapest leg. Cargo rides along and the ordinary sell logic lists it
        on arrival.
        """
        ship = self.ship
        origin = ship.planet
        if origin is None or ship.status != ShipStatus.DOCKED:
            return None
        if ship.stop_resume is not None:
            return None
        if self._nav.fuel_station_at(origin):
            return None
        if self._local_fuel_bunkerable(origin):
            return None
        if (
            ship.fuel >= self._fuel_survival_target()
            and self._committed_fuel_need <= ship.fuel
        ):
            return None
        stations = self._reachable_stations()
        if not stations:
            return None

        cargo = {
            commodity: quantity
            for commodity in self._get_tradeable_commodities()
            if (quantity := self._sellable_quantity(commodity)) > 0
        }
        reference = self._fuel_value_reference()
        fuel_price = (
            reference if reference is not None else float(FUEL_BID_FALLBACK_FLOOR)
        )

        best: Optional[Planet] = None
        best_score = 0.0
        for station in stations:  # nearest first, so ties keep the nearest
            value = 0.0
            for commodity, quantity in cargo.items():
                bid, _ = station.market.get_bid_ask_spread(commodity)
                price = max(bid or 0, self._flow_value(station.market, commodity) or 0)
                value += price * quantity
            leg_milli = ship.fuel_required(self._nav.distance(origin, station))
            score = value - (leg_milli / FUEL_MILLI) * fuel_price
            if best is None or score > best_score:
                best = station
                best_score = score
        return best

    def _bunker_fillable_units(
        self, bid_price: int, planet: Planet, tank_milli: int
    ) -> int:
        """Market units of fuel the ship could buy at ``planet`` in one turn.

        The smallest of tank room above ``tank_milli``, what the cheap-tier
        bunker budget affords at ``bid_price``, and the supply a single turn
        offers: the resting ask depth or the recent traded units per turn,
        whichever is larger. The depth resting now and the flow arriving next
        turn are alternative estimates of the same thing, not additive.

        ``planet`` and ``tank_milli`` are passed rather than read off the ship
        because an en-route refuel stop asks about a planet the ship has not
        docked at yet and a tank that still has to be credited with the
        unburned route fuel.
        """
        ship = self.ship
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None or bid_price <= 0:
            return 0
        room_units = milli_to_units_floor(ship.fuel_capacity - tank_milli)
        affordable = int(ship.money * FUEL_BUNKER_BUDGET_FRACTION_CHEAP) // bid_price
        supply = max(
            self._nav.fuel_ask_depth_at(planet),
            int(self._recent_flow_per_turn(planet.market, fuel_commodity)),
        )
        return max(0, min(room_units, affordable, supply))

    def _trip_turn_value(self, profit: float, destination: Planet) -> float:
        """Value of one docked turn to a ship about to fly to ``destination``.

        The trip's profit spread over the turns it occupies: both legs plus
        the docked turn at each end, approximated as ``2 * travel + 1``.
        Delaying departure by a turn costs about this much.
        """
        origin = self.ship.planet
        if origin is None:
            return 0.0
        distance = self._nav.distance(origin, destination)
        travel_turns = max(1, math.ceil(distance / DISTANCE_PER_TURN))
        return profit / (2 * travel_turns + 1)

    def _should_linger_to_bunker(self, turn_value: float) -> bool:
        """Whether one more docked turn of cheap bunkering beats departing.

        A departing ship cancels its resting orders, so the bunker bid it
        placed this turn buys nothing unless the ship stays for the
        end-of-turn match. Staying is worth it when the fuel here is below the
        galaxy reference and the ship can take enough of it that the saving,
        fillable units times the price gap, beats one turn of the trip.

        Conditions, all required: the tank is below FUEL_LINGER_TANK_FRACTION
        of capacity, the local ask is inside FUEL_BUNKER_PREMIUM and strictly
        below the reference, a meaningful quantity is fillable, and this stop
        has not already spent its one linger turn.
        """
        ship = self.ship
        planet = ship.planet
        if planet is None or turn_value <= 0:
            return False
        if self._fuel_linger_planet is planet:
            return False
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return False
        if ship.fuel >= FUEL_LINGER_TANK_FRACTION * ship.fuel_capacity:
            return False
        reference = self._fuel_value_reference()
        if reference is None:
            return False
        _, ask = planet.market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask <= 0:
            return False
        if ask > math.ceil(reference * FUEL_BUNKER_PREMIUM):
            return False
        saving_per_unit = reference - ask
        if saving_per_unit <= 0:
            return False
        bid = max(ask, self._flow_value(planet.market, fuel_commodity) or 0)
        fillable = self._bunker_fillable_units(bid, planet, ship.fuel)
        if fillable <= 0:
            return False
        return fillable * saving_per_unit > turn_value

    def wants_refuel_stop(
        self,
        planet: Planet,
        destination: Planet,
        fuel_refund: int,
        fuel_shortfall: int,
        stop_turns: int,
    ) -> bool:
        """Whether cheap fuel at ``planet`` is worth breaking the journey for.

        Departure charged the whole route up front and the stop refunds what
        is not yet burned, so the only fuel a stop can cost is the per-leg
        rounding: ``fuel_shortfall`` millifuel, at most
        REFUEL_STOP_MAX_SHORTFALL. The market sells whole units, so the ship
        buys the one unit that covers it.
        The stop buys them here rather than treating them as a reason to fly
        on. The criteria
        below already establish that the ask is cheap and the book deep, so
        the purchase is the cheapest fuel the trip will see; the resume is
        held until it lands (:meth:`_route_stop_travel`).

        Conditions, all required: the tank after the refund is below
        FUEL_STOP_TANK_FRACTION of capacity; a fuel ask rests here; the planet
        has a real price signal and the ask is at or below its 30-day average,
        so a planet with no price history is passed by rather than guessed
        about; the ask is inside FUEL_BUNKER_PREMIUM of the galaxy reference
        and strictly below it, or there is no saving to collect; the shortfall
        is affordable here and the book can cover it with at least one unit to
        spare; and the saving on the units beyond the shortfall beats what the
        stop costs, ``stop_turns`` turns of the trip's own value.
        """
        ship = self.ship
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return False
        tank_after_refund = ship.fuel + fuel_refund
        if tank_after_refund >= FUEL_STOP_TANK_FRACTION * ship.fuel_capacity:
            return False
        market = planet.market
        _, ask = market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask <= 0:
            return False
        if not market.has_price_signal(fuel_commodity):
            return False
        if ask > market.get_30_day_average_price(fuel_commodity):
            return False
        reference = self._fuel_value_reference()
        if reference is None:
            return False
        if ask > math.ceil(reference * FUEL_BUNKER_PREMIUM):
            return False
        saving_per_unit = reference - ask
        if saving_per_unit <= 0:
            return False
        bid = max(ask, self._flow_value(market, fuel_commodity) or 0)
        shortfall_units = milli_to_units_ceil(fuel_shortfall)
        if ship.money < shortfall_units * bid:
            return False
        fillable = self._bunker_fillable_units(bid, planet, tank_after_refund)
        if fillable < shortfall_units + 1:
            return False
        # The shortfall is replacing fuel the rounding took away, not saving
        # anything, so only the units past it count toward the saving.
        saving = (fillable - shortfall_units) * saving_per_unit
        return saving > self._departed_trip_turn_value * stop_turns

    def _stop_fuel_price(self, planet: Planet) -> int:
        """What the shortfall costs a unit at ``planet``.

        The local ask when one rests there, and otherwise the galaxy fuel
        reference: a stop is decided in flight, before the ship can see what
        the board will hold when it docks, so a planet with no ask is priced
        at the typical galaxy price rather than refused outright.
        """
        fuel_commodity = self._fuel_commodity()
        reference = self._fuel_value_reference() or FUEL_BID_FALLBACK_FLOOR
        if fuel_commodity is None:
            return math.ceil(reference)
        _, ask = planet.market.get_bid_ask_spread(fuel_commodity)
        if ask is None or ask <= 0:
            return math.ceil(reference)
        return ask

    def wants_pickup_stop(
        self,
        planet: Planet,
        destination: Planet,
        fuel_refund: int,
        fuel_shortfall: int,
        stop_turns: int,
    ) -> bool:
        """Whether the contracts waiting at ``planet`` are worth stopping for.

        The candidates are the open contracts on the local board bound for a
        planet farther along the remaining route, the terminus included: the
        ship is going there anyway, so such a rider costs the trip only its
        hold space. They are filled greedily by payment per hold unit into the
        free hold, the same order :meth:`_accept_riders` uses.

        The stop is worth it when that payment beats what the stop costs: the
        turns it takes, valued at the trip's own per-turn value, plus the
        shortfall fuel the per-leg rounding charges. The ship must also be
        able to pay for the shortfall, or it would dock somewhere it cannot
        leave.
        """
        ship = self.ship
        free_hold = ship.free_hold()
        if free_hold <= 0:
            return False
        payment = self._route_rider_value(planet, destination, free_hold)
        if payment <= 0:
            return False
        fuel_price = self._stop_fuel_price(planet)
        shortfall_cost = milli_to_units_ceil(fuel_shortfall) * fuel_price
        if ship.money < shortfall_cost:
            return False
        return payment > self._departed_trip_turn_value * stop_turns + shortfall_cost

    def _linger_to_bunker(self, turn_value: float) -> bool:
        """Take this stop's one linger turn if it is worth more than the trip.

        Records the stop so no second linger is taken here, and flags the turn
        so the next decide_trade_actions does not charge it to a loaded plan's
        departure patience.
        """
        if not self._should_linger_to_bunker(turn_value):
            return False
        self._fuel_linger_planet = self.ship.planet
        self._fuel_linger_pending = True
        self.ship.last_action = (
            f"{self.ship.last_action}; Staying one turn to buy cheap fuel"
        )
        return True

    def _fuel_bid_price(self, planet: Planet, quantity: int) -> int:
        """Price for a standing fuel bid that makes delivery profitable.

        Delegates to :meth:`Navigator.fuel_delivery_bid_price`, which spaceport
        operators use for the same purpose.
        """
        return self._nav.fuel_delivery_bid_price(planet, quantity)

    def _local_fuel_reference_price(self, planet: Planet) -> int:
        """Scarcity-escalated price a local fuel producer would plausibly take.

        Delegates to :meth:`Navigator.local_fuel_reference_price`.
        """
        return self._nav.local_fuel_reference_price(planet)

    def _post_standing_fuel_bid(self) -> Optional[str]:
        """Post a standing buy order for fuel priced to entice a supply run.

        This is the escape hatch for the stranding condition: fuel below the
        travel reserve and no local ask to lift. The bid rests in the book so
        other traders' cross-planet scans see it as a sell opportunity.
        decide_trade_actions re-posts it every docked turn while the
        condition holds.

        Two limits keep the bid from being the ship's ruin. It posts nothing
        when a fuel station is within tank range, because R4 flies the ship
        there instead and a resting bid would only reserve the money the trip
        needs. And every price is capped at
        :meth:`Navigator.fuel_bid_ceiling`: a seller fills a resting bid at
        the bid price, so an escalating bid buys its own escalation.
        """
        ship = self.ship
        planet = ship.planet
        if planet is None:
            return None
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return None
        if self._can_reach_station():
            return None
        market = planet.market
        ceiling = self._nav.fuel_bid_ceiling()

        # Bid only up to the survival target, not a full tank. A tank-sized
        # rescue bid at delivery prices reserves most of the ship's money for
        # as long as it goes unfilled, locking it out of the trading that
        # could earn its way out. The target is millifuel; the bid is whole
        # market units.
        max_units = milli_to_units_ceil(self._fuel_survival_target() - ship.fuel)
        if max_units <= 0:
            return None

        budget = int(ship.money * 0.9)  # keep a small operating buffer
        price = min(self._fuel_bid_price(planet, max_units), ceiling)
        quantity = min(max_units, budget // price) if price > 0 else 0
        if 0 < quantity < max_units:
            # Smaller bids amortize the delivery burn over fewer units, so
            # reprice once for the quantity we can afford.
            price = min(self._fuel_bid_price(planet, quantity), ceiling)
            quantity = min(quantity, budget // price)
        if quantity <= 0:
            # Too poor for a delivery-viable bid; a 1-unit rescue run can
            # never amortize its burn. A local producer needs no delivery
            # margin, so bid the local reference with the money that remains
            # rather than going silent.
            price = min(self._local_fuel_reference_price(planet), ceiling)
            quantity = min(max_units, budget // price) if price > 0 else 0
        if quantity <= 0:
            return None

        order_id = market.place_buy_order(ship, fuel_commodity, quantity, price)
        if not order_id:
            return None
        ship.active_orders[order_id] = "buy fuel (standing bid)"
        return order_id

    def _trip_fuel_economics(
        self, origin: Planet, destination: Planet
    ) -> Optional["_TripFuel"]:
        """Fuel logistics and safety of flying ``origin`` -> ``destination``.

        Returns None when the leg cannot be flown at all:
        - round-trip fuel would need buying where fuel is not purchasable
        - the fuel would have to be bought at a spiked ask while a station is
          in tank range
        - the destination would leave the ship without an escape route

        Nothing here is about what the trip carries, so a trade plan and a
        contract trip ask the same question and get the same answer.
        """
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return None

        # Efficiency-adjusted fuel must match what start_journey consumes.
        distance = self._nav.distance(origin, destination)
        fuel_one_way = self.ship.fuel_required(distance)
        fuel_round_trip = fuel_one_way * 2

        origin_market = origin.market
        _, fuel_ask = origin_market.get_bid_ask_spread(fuel_commodity)
        fuel_price = (
            fuel_ask
            if fuel_ask is not None
            else origin_market.get_avg_price(fuel_commodity)
        )
        if fuel_price is None or fuel_price <= 0:
            fuel_price = 10  # Default fuel price if no market data
        reference = self._fuel_value_reference()
        fuel_reference_price = math.ceil(reference) if reference else fuel_price

        current_fuel = self.ship.fuel

        round_trip_shortfall = max(0, fuel_round_trip - current_fuel)
        if round_trip_shortfall > 0 and not self._fuel_purchasable_at(origin):
            # The plan needs fuel that cannot be bought at the origin: no ask
            # and no recent flow. Committing would strand the ship with cargo.
            return None

        # The ship buys the whole round-trip shortfall, even where the local
        # ask is scarcity-priced. Rationing to this leg's shortfall and
        # refuelling at the far end was tried, gated on the destination still
        # being fuel-safe on that much, and it grounded the fleet: over a
        # 200-turn 30-planet run stranded ships went 3 -> 14 and departures 68
        # -> 22. A fuel-safe destination is a market that has depth *now*,
        # and by arrival it usually does not. Only the *margin* is charged one
        # way; the tank is still filled for the round trip.
        #
        # A non-station destination now also demands an escape leg plus the
        # maintenance buffer on arrival, and that can exceed what the return
        # leg leaves in the tank. Fund the larger of the round trip and the
        # outbound leg plus that arrival requirement, or the gate below
        # rejects every such pair outright. The tank caps the purchase; if
        # even a full tank cannot satisfy the gate, the pair is infeasible.
        arrival_requirement = self._arrival_fuel_requirement(destination, origin)
        fuel_wanted = max(fuel_round_trip, fuel_one_way + arrival_requirement)
        tank_room = self.ship.fuel_capacity - current_fuel
        # The tank is millifuel; the market sells whole units. Round the
        # shortfall up to the unit that covers it, but never past the whole
        # units the tank still has room for.
        fuel_to_buy = min(
            milli_to_units_ceil(min(fuel_wanted - current_fuel, tank_room)),
            milli_to_units_floor(tank_room),
        )
        if fuel_to_buy > 0 and not self._fuel_purchasable_at(origin):
            return None
        # Fuel that has to be bought here at a spiked ask, while a station is
        # within reach, is fuel the ship should buy at the station instead:
        # the plan is not adopted, the reposition logic moves the ship, and
        # it plans again from a market that sells fuel at a sane price.
        if (
            fuel_to_buy > 0
            and not self._local_fuel_bunkerable(origin)
            and self._can_reach_station()
        ):
            return None

        # Never fly somewhere that leaves no escape route: the destination
        # must sell fuel, or the fuel left after arrival must reach a planet
        # that does.
        fuel_after_arrival = current_fuel + units_to_milli(fuel_to_buy) - fuel_one_way
        if not self._fuel_safe_destination(destination, origin, fuel_after_arrival):
            return None

        # Expected maintenance over the round trip: two departure rolls, each
        # possibly costing the fuel-tier repair at origin fuel prices.
        maintenance_cost = math.ceil(
            2 * MAINTENANCE_CHANCE * MAINTENANCE_FUEL_UNITS * fuel_price
        )

        return _TripFuel(
            distance=distance,
            fuel_one_way=fuel_one_way,
            fuel_price=fuel_price,
            fuel_reference_price=fuel_reference_price,
            fuel_to_buy=fuel_to_buy,
            fuel_from_tank_one_way=min(current_fuel, fuel_one_way),
            expected_maintenance_cost=maintenance_cost,
        )

    def _pair_economics(
        self, origin: Planet, destination: Planet
    ) -> Optional["_PairEconomics"]:
        """Commodity-independent economics of trading ``origin`` -> ``destination``.

        The fuel logistics and safety gates come from
        :meth:`_trip_fuel_economics`; this adds the trading budget, so
        :meth:`_best_plan_from` can evaluate many commodities per pair
        without redoing either. Returns None when the leg itself is
        infeasible, or when no money remains for cargo after fuel, the refuel
        floor, expected maintenance, and the operating buffer.

        The cash gate below is deliberately round-trip: a trade that
        disappoints must never leave the ship both broke and dry. Only the
        *margin* is charged one way, in :class:`TradePlan`.
        """
        trip = self._trip_fuel_economics(origin, destination)
        if trip is None:
            return None

        current_fuel = self.ship.fuel
        cargo_space = self.ship.cargo_capacity - self.ship.cargo.get_total_quantity()
        fuel_price = trip.fuel_price
        fuel_round_trip = trip.fuel_one_way * 2
        fuel_cost = trip.fuel_to_buy * fuel_price
        maintenance_cost = trip.expected_maintenance_cost

        # Money for cargo. Withhold the fuel purchase, a cash floor that can
        # re-buy the travel reserve after the trip, expected maintenance, and
        # a 10% operating buffer. A trade that disappoints must never leave
        # the ship both broke and dry; that is the stranding spiral.
        # Only the reserve the trip does not leave in the tank has to be
        # re-bought. Charging the whole reserve in cash on top of fuel the
        # ship already holds is double counting, and it locked full-tank
        # ships out of every pair whenever fuel prices spiked.
        fuel_left_after_trip = max(current_fuel, fuel_round_trip) - fuel_round_trip
        refuel_shortfall = max(0, self._fuel_reserve_need() - fuel_left_after_trip)
        refuel_floor = milli_to_units_ceil(refuel_shortfall) * fuel_price
        money_for_trading = int(
            (self.ship.money - fuel_cost - refuel_floor - maintenance_cost) * 0.9
        )
        if money_for_trading <= 0:
            return None

        return _PairEconomics(
            distance=trip.distance,
            fuel_one_way=trip.fuel_one_way,
            fuel_price=fuel_price,
            fuel_reference_price=trip.fuel_reference_price,
            fuel_to_buy=trip.fuel_to_buy,
            fuel_from_tank_one_way=trip.fuel_from_tank_one_way,
            expected_maintenance_cost=maintenance_cost,
            money_for_trading=money_for_trading,
            max_by_cargo=cargo_space,
        )

    def _origin_acquisition(
        self, origin: Planet, commodity: CommodityDefinition
    ) -> Optional["_OriginAcquisition"]:
        """What acquiring ``commodity`` at ``origin`` costs, and what to bid.

        A resting ask is directly liftable at its own price; an active local
        flow - recent volume with a real price signal - has to be bid for at
        the clearing price. The ship posts the higher of the two, since
        matching executes at each seller's ask and a generous bid wins flow
        without overpaying for the cheap fills. What the units are expected
        to *cost* is a different number, and the two are kept apart here.

        This ship's own asks are excluded from the walk. Matching has no
        buyer-is-seller guard, so pricing an entry off a book that contains
        our own ask puts the bid at or above it and the ship buys its own
        cargo back - which is exactly what a ship listing stale cargo and
        replanning in the same turn would otherwise do.

        Returns None when the commodity cannot be acquired at all: no ask
        resting and no flow to bid into.
        """
        market = origin.market
        _, origin_ask = market.get_bid_ask_spread(commodity)
        origin_flow_px = self._flow_value(market, commodity)
        if origin_flow_px is not None and (
            self._recent_flow_per_turn(market, commodity) <= 0
        ):
            origin_flow_px = None  # a price with no recent volume buys nothing
        flow_price = origin_flow_px if origin_flow_px and origin_flow_px > 0 else 0

        ask_levels = (
            market.get_ask_levels(commodity, exclude_actor=self.ship)
            if origin_ask is not None and origin_ask > 0
            else []
        )
        if not ask_levels and flow_price <= 0:
            return None

        entry_price = ask_levels[0][0] if ask_levels else flow_price
        bid_price = max(entry_price, flow_price)
        return _OriginAcquisition(
            ask_levels=ask_levels,
            flow_price=flow_price,
            bid_price=bid_price,
            entry_price=entry_price,
        )

    def _walked_purchase_price(
        self, acquisition: "_OriginAcquisition", quantity: int
    ) -> int:
        """Expected per-unit cost of lifting ``quantity`` units at the origin.

        Walks the resting asks cheapest first, then prices whatever the book
        cannot supply at the bid, which is what winning it out of the flow
        costs. Rounded up, so the margin gate never flatters the plan.
        """
        if quantity <= 0:
            return acquisition.bid_price
        remaining = quantity
        cost = 0
        for price, qty in acquisition.ask_levels:
            take = min(qty, remaining)
            cost += price * take
            remaining -= take
            if remaining == 0:
                break
        cost += acquisition.bid_price * remaining
        return math.ceil(cost / quantity)

    def _evaluate_trade_opportunity(
        self,
        origin: Planet,
        destination: Planet,
        commodity: CommodityDefinition,
        pair: Optional["_PairEconomics"] = None,
        acquisition: Optional["_OriginAcquisition"] = None,
    ) -> Optional[TradePlan]:
        """Evaluate a single trade opportunity between two planets.

        Returns a TradePlan if the trade is feasible, None otherwise.
        Feasibility checks:
        - Commodity is acquirable at origin: a resting ask, or an active
          local flow the ship can bid into
        - Enough money for purchase plus fuel
        - Enough cargo space
        - Enough fuel for the round trip, held or purchasable

        Prices and quantities come from the flow, recent clearing prices and
        volume, as well as the resting book. Costs are judged on what the
        cargo is expected to cost - the resting asks walked for the planned
        quantity - not on the bid the ship posts, which is deliberately
        higher so the order also wins units out of the flow.

        ``pair`` carries the commodity-independent economics of the pair and
        ``acquisition`` the destination-independent origin prices;
        :meth:`_best_plan_from` precomputes both, and each is derived on the
        fly when omitted.
        """
        if pair is None:
            pair = self._pair_economics(origin, destination)
        if pair is None:
            return None
        if acquisition is None:
            acquisition = self._origin_acquisition(origin, commodity)
        if acquisition is None:
            # Nothing for sale and no active flow to bid into.
            return None

        dest_market = destination.market
        # Screen demand against what the cargo costs, not what the ship bids.
        entry_price = acquisition.entry_price

        # Destination demand: resting bids that beat the purchase price...
        bid_levels = [
            (price, qty)
            for price, qty in dest_market.get_bid_levels(commodity)
            if price > entry_price
        ]
        depth = sum(qty for _, qty in bid_levels)
        # ...plus the flow: recent clearing volume at the haircut clearing
        # price, a forecast rather than a resting order.
        dest_flow_px = self._flow_value(dest_market, commodity)
        flow_px = int(dest_flow_px * SELL_PRICE_HAIRCUT) if dest_flow_px else 0
        flow_qty = 0
        if flow_px > entry_price:
            flow_qty = int(
                self._recent_flow_per_turn(dest_market, commodity)
                * DEMAND_HORIZON_TURNS
            )
        if depth + flow_qty > 0:
            sellable = depth + flow_qty
        elif flow_px > entry_price:
            # Price signal but no recent volume: latent demand is a guess
            # that often fails to realize, so such plans are capped small.
            sellable = SPECULATIVE_PLAN_CAP
        else:
            return None

        # Budget and cargo limits are pair-level facts; only the price is
        # commodity-specific. Budget against the bid, since that is what an
        # order actually reserves.
        max_by_money = pair.money_for_trading // acquisition.bid_price
        max_quantity = max(0, min(max_by_money, pair.max_by_cargo))

        # Project revenue from the resting bids first, since their prices are
        # firm, then value the remainder at the haircut flow price.
        quantity = min(max_quantity, sellable)
        if quantity <= 0:
            return None
        remaining = quantity
        revenue = 0
        for price, qty in bid_levels:
            take = min(qty, remaining)
            revenue += price * take
            remaining -= take
            if remaining == 0:
                break
        if remaining > 0:
            revenue += flow_px * remaining
        sell_price = revenue // quantity

        return TradePlan(
            origin=origin,
            destination=destination,
            commodity=commodity,
            quantity=quantity,
            bid_price_per_unit=acquisition.bid_price,
            purchase_price_per_unit=self._walked_purchase_price(acquisition, quantity),
            expected_sell_price_per_unit=sell_price,
            distance=pair.distance,
            fuel_needed_one_way=pair.fuel_one_way,
            fuel_price_at_origin=pair.fuel_price,
            fuel_units_from_tank=pair.fuel_from_tank_one_way,
            fuel_price_from_tank=pair.fuel_reference_price,
            expected_maintenance_cost=pair.expected_maintenance_cost,
        )

    def _find_best_trade_plan(self) -> Optional[TradePlan]:
        """Survey all possible trades from here and return the most profitable.

        Memoized per (turn, planet): decide_trade_actions runs the survey and,
        when it finds nothing, decide_travel asks again the same turn before
        repositioning. The memo answers that second call.
        """
        current_planet = self.ship.planet
        if current_planet is None or not self.ship.simulation.planets:
            return None
        memo = self._plan_search_memo
        turn = self.ship.simulation.current_turn
        if memo is not None and memo[0] == turn and memo[1] is current_planet:
            return memo[2]
        plan = self._best_plan_from(current_planet)
        self._plan_search_memo = (turn, current_planet, plan)
        return plan

    def _plan_acceptable(self, plan: TradePlan) -> bool:
        """Whether this ship will adopt ``plan``.

        Normally the plan must clear ``TradePlan.MIN_MARGIN``, which is a
        quality bar for a ship that has alternatives. A distressed ship has
        none: it is parked, out of cash, and every turn it waits for a
        15%-margin haul is a turn of no income at all. For it the bar drops
        to round-trip honesty rather than to zero: the haul's expected profit
        must also cover :attr:`TradePlan.return_leg_fuel_cost`, the return
        leg the ship has to buy out of it.

        Dropping the bar to bare profitability was a distress trap. The haul
        is costed one way, while the cash gate makes the ship buy the round
        trip, so a haul earning less than the return leg leaves the ship
        poorer than it started, still under the cash floor, and still
        distressed - which re-qualifies it for the same kind of haul, for
        good.

        Nothing about safety is relaxed: the round-trip cash gate and the
        refuel floor in :meth:`_pair_economics` still size the haul to the
        cash the ship actually has, and the fuel-safety gates are untouched.
        """
        if plan.expected_profit <= 0:
            return False
        if plan.is_profitable():
            return True
        return self.is_distressed and plan.expected_profit > plan.return_leg_fuel_cost

    def _best_plan_from(self, origin: Planet) -> Optional[TradePlan]:
        """Most profitable trade plan exporting from ``origin``, if any.

        Only commodities acquirable at the origin are evaluated, and only
        against the navigator's per-turn candidate destinations for each
        commodity: the top-value demand planets united with the nearest
        ones, or every demand planet in small galaxies. Both are superset
        filters; the evaluation re-verifies prices. When the galaxy shows no
        trade signal at all, planning is skipped. The commodity-independent
        economics of each pair are computed once and shared across
        commodities, and the destination-independent origin prices once per
        commodity.

        Goods this ship is itself offering here are skipped. A ship cannot
        profitably buy what it is trying to sell in the same book, and
        planning to would have it bid against - and match - its own ask.

        Plans are ranked on their expected profit plus the riders the trip
        could take on (:meth:`_route_rider_value`), which is why two hauls to
        different destinations no longer come down to profit alone. The
        acceptance test is untouched: riders never qualify a haul, only pick
        between qualified ones.
        """
        nav = self._nav
        if not nav.has_any_trade_signal():
            return None
        exportable = nav.exportable_commodities(origin)
        if not exportable:
            return None
        listing_here = {
            order.commodity_type
            for order in origin.market.get_actor_orders(self.ship)["sell"]
        }

        best_plan: Optional[TradePlan] = None
        best_score = 0
        # The ship arrives empty at an origin it is not at, so the whole hold
        # is free for riders there.
        hold = (
            self.ship.free_hold()
            if origin is self.ship.planet
            else self.ship.cargo_capacity
        )
        # Per-pair facts for this search only; the navigator owns all
        # cross-ship, cross-turn caching.
        pair_economics: Dict[Planet, Optional[_PairEconomics]] = {}

        for commodity in self._get_tradeable_commodities():
            if commodity not in exportable or commodity in listing_here:
                continue
            acquisition = self._origin_acquisition(origin, commodity)
            if acquisition is None:
                continue
            for destination in nav.candidate_destinations(origin, commodity):
                if destination in pair_economics:
                    pair = pair_economics[destination]
                else:
                    pair = self._pair_economics(origin, destination)
                    pair_economics[destination] = pair
                if pair is None:
                    continue
                plan = self._evaluate_trade_opportunity(
                    origin=origin,
                    destination=destination,
                    commodity=commodity,
                    pair=pair,
                    acquisition=acquisition,
                )
                if plan is None or not self._plan_acceptable(plan):
                    continue
                score = plan.expected_profit + self._plan_rider_value(plan, hold)
                if score > best_score:
                    best_plan = plan
                    best_score = score

        return best_plan

    def _accepted_contracts(self) -> List[Contract]:
        """Contracts this ship has taken on but not yet loaded."""
        return [
            contract
            for contract in self.ship.contracts
            if contract.status is ContractStatus.ACCEPTED
        ]

    def _release_accepted_contracts(self) -> None:
        """Put every unloaded contract back on its board.

        Acceptance costs nothing and is re-made at the next departure
        decision, so the ship starts each docked turn holding only what it
        has actually loaded. Without this, a departure that fails its
        maintenance roll or its fuel check would leave contracts bound to a
        ship that is no longer going there, occupying hold space no cargo
        decision could use.
        """
        for contract in self._accepted_contracts():
            contract.origin.contracts.release(contract)

    def _accept_riders(self, destination: Planet) -> None:
        """Take on every open contract this trip's route serves that fits the hold.

        Called once the departure is settled, so the fuel is already paid for
        by whatever the ship is flying for and a rider is pure revenue. No
        margin test applies; the only limit is space, and the best-paying
        contracts per hold unit are taken first.

        A contract to a planet the route passes counts as well as one to the
        terminus. The ship flies past intermediate planets anyway and
        :meth:`Ship._deliver_passed_contracts` puts the payload down as it
        goes, so an intermediate rider costs the trip nothing but hold space.
        """
        planet = self.ship.planet
        if planet is None:
            return
        served = set(self._nav.route(planet, destination))
        served.discard(planet)
        riders = sorted(
            (
                contract
                for contract in planet.contracts.open_contracts()
                if contract.destination in served and contract.payload.hold_units > 0
            ),
            key=lambda contract: contract.total_payment / contract.payload.hold_units,
            reverse=True,
        )
        for contract in riders:
            if contract.payload.hold_units <= self.ship.free_hold():
                planet.contracts.accept(contract, self.ship)

    def _route_rider_value(
        self, origin: Planet, destination: Planet, free_hold: int
    ) -> int:
        """Payment the open contracts on this route would add to the trip.

        The contracts on ``origin``'s board bound for any planet the route
        from ``origin`` to ``destination`` serves, filled greedily by payment
        per hold unit into ``free_hold``. That is exactly what
        :meth:`_accept_riders` takes on once the departure is settled, so it
        is what the trip is worth beyond whatever else it carries.

        Ranking only. A rider rides a trip the ship was flying anyway, so it
        must never make a marginal haul look worth flying: nothing here
        reaches :meth:`TradePlan.is_profitable` or :meth:`_plan_acceptable`.
        It decides between trips that each already clear those tests, so a
        good haul with riders beats a slightly better haul without.
        """
        if free_hold <= 0:
            return 0
        offers = [
            contract
            for contract in origin.contracts.open_contracts()
            if contract.payload.hold_units > 0
        ]
        if not offers:
            return 0
        served = set(self._nav.route(origin, destination))
        served.discard(origin)
        remaining = free_hold
        value = 0
        for contract in sorted(
            (contract for contract in offers if contract.destination in served),
            key=lambda contract: contract.total_payment / contract.payload.hold_units,
            reverse=True,
        ):
            if contract.payload.hold_units <= remaining:
                remaining -= contract.payload.hold_units
                value += contract.total_payment
        return value

    def _plan_rider_value(self, plan: TradePlan, hold: int) -> int:
        """Rider payment a trade plan's trip could add, after its own cargo."""
        return self._route_rider_value(
            plan.origin, plan.destination, hold - plan.quantity
        )

    def _pinned_contract_destination(self) -> Optional[Planet]:
        """Where a loaded contract obliges the ship to fly, if anywhere.

        A payload is aboard and is owed a delivery, so that destination
        outranks cargo hops and repositions. Core strands the payload after
        CONTRACT_STRAND_PATIENCE docked turns short of it, which is what
        bounds the pin.

        With several payloads aboard the pin goes to the destination whose
        route covers the most of the others, farthest first among equals: a
        payload on that route is put down as the ship passes it. Anything the
        chosen route misses is left to the stranding rule. This is a choice
        between single destinations, not a multi-stop plan.
        """
        planet = self.ship.planet
        if planet is None:
            return None
        destinations = [
            contract.destination
            for contract in self.ship.contracts
            if contract.status is ContractStatus.LOADED
            and contract.destination is not planet
        ]
        if not destinations:
            return None
        best: Optional[Planet] = None
        best_key = (-1, -1.0)
        for candidate in destinations:
            served = set(self._nav.route(planet, candidate))
            key = (
                sum(1 for other in destinations if other in served),
                self._nav.distance(planet, candidate),
            )
            if key > best_key:
                best_key = key
                best = candidate
        return best

    def _value_contract_group(
        self,
        origin: Planet,
        destination: Planet,
        contracts: Sequence[Contract],
        hold: int,
    ) -> Optional[ContractPlan]:
        """Value the best-paying subset of ``contracts`` that fits ``hold``.

        Filled by payment per hold unit, so a full hold carries the most
        valuable jobs. Returns None when the leg is infeasible, when nothing
        fits, or when the fuel the trip needs is beyond the ship's money plus
        the advances the group pays at load.
        """
        trip = self._trip_fuel_economics(origin, destination)
        if trip is None or hold <= 0:
            return None

        remaining = hold
        chosen: List[Contract] = []
        for contract in sorted(
            (c for c in contracts if c.payload.hold_units > 0),
            key=lambda c: c.total_payment / c.payload.hold_units,
            reverse=True,
        ):
            if contract.payload.hold_units <= remaining:
                chosen.append(contract)
                remaining -= contract.payload.hold_units
        if not chosen:
            return None

        plan = ContractPlan(
            origin=origin,
            destination=destination,
            contracts=tuple(chosen),
            distance=trip.distance,
            fuel_needed_one_way=trip.fuel_one_way,
            fuel_price_at_origin=trip.fuel_price,
            fuel_units_from_tank=trip.fuel_from_tank_one_way,
            fuel_price_from_tank=trip.fuel_reference_price,
            expected_maintenance_cost=trip.expected_maintenance_cost,
        )
        # The cash gate a trade plan applies, with the advances counted as
        # cash: they are paid at load, and a consignment can be loaded on the
        # docked turn so the money is in hand before the fuel bid goes in.
        if self.ship.money + plan.advance_total < trip.fuel_to_buy * trip.fuel_price:
            return None
        return plan

    def _best_contract_trip(
        self, origin: Optional[Planet] = None
    ) -> Optional[ContractPlan]:
        """The most profitable contract-only trip out of ``origin``.

        Open contracts on the board are grouped by destination, since one
        departure delivers a whole group, and each group is filled into the
        hold and priced. A trip is worth flying when its payments beat its
        fuel and expected maintenance; there is no cargo to take a margin on.

        ``origin`` defaults to where the ship is docked, and the hold is then
        what contracts and cargo have left free. Passing another planet
        values what the ship could pick up there, with the whole hold, which
        is what :meth:`_find_reposition_target` scores an origin on.
        """
        here = self.ship.planet
        if here is None:
            return None
        source = origin if origin is not None else here
        hold = self.ship.cargo_capacity if source is not here else self.ship.free_hold()
        if hold <= 0:
            return None

        groups: Dict[Planet, List[Contract]] = {}
        for contract in source.contracts.open_contracts():
            if contract.destination is source:
                continue
            groups.setdefault(contract.destination, []).append(contract)

        best: Optional[ContractPlan] = None
        for destination, contracts in groups.items():
            plan = self._value_contract_group(source, destination, contracts, hold)
            if plan is None or plan.expected_profit <= 0:
                continue
            if best is None or plan.expected_profit > best.expected_profit:
                best = plan
        return best

    def _pending_fuel_units(self) -> int:
        """Fuel units this ship already has resting on the local book."""
        planet = self.ship.planet
        fuel_commodity = self._fuel_commodity()
        if planet is None or fuel_commodity is None:
            return 0
        return sum(
            order.quantity
            for order in planet.market.get_actor_orders(self.ship)["buy"]
            if order.commodity_type is fuel_commodity
        )

    def _execute_contract_plan(
        self, plan: ContractPlan, fuel_blocked: bool
    ) -> List[str]:
        """Accept a contract trip's jobs and fund the fuel it needs.

        The only purchase a contract trip makes is fuel, so this is the whole
        of its execution. The commitment goes through ``_committed_fuel_need``,
        the same channel a held cargo's trip uses, so the top-up buys what the
        departure gate will ask for even at a scarcity price.

        A ship too poor to buy that fuel outright loads its consignments here,
        on the docked turn, rather than at departure: the advance is paid at
        load, and a consignment load has no other effect, so the money is in
        hand for the bid. This is what lets a broke ship with a dry tank take
        a government job. Passengers are not boarded early; taking an actor
        off its planet before the ship can leave is a real cost to the actor.

        The fuel step runs even when upkeep already bid for fuel this turn:
        that bid was placed before the commitment existed, and before the
        advance was in hand, so it can be short of the leg. Units already
        resting on the book are passed as ``pending_fuel``, so the top-up
        tops up rather than buying the leg twice. ``fuel_blocked`` is the one
        case it must not run at all: a fuel ask of this ship's own is resting
        here, and bidding would meet it.
        """
        actions: List[str] = []
        board_planet = plan.origin
        for contract in plan.contracts:
            board_planet.contracts.accept(contract, self.ship)

        fuel_cash_needed = 0
        trip = self._trip_fuel_economics(plan.origin, plan.destination)
        if trip is not None:
            fuel_cash_needed = trip.fuel_to_buy * trip.fuel_price
        if self.ship.money < fuel_cash_needed:
            for contract in plan.contracts:
                if isinstance(contract.payload, ConsignmentPayload):
                    load_contract(self.ship, contract)

        self._committed_fuel_need = self._departure_fuel_requirement(plan.destination)
        actions.append(
            f"Contract trip to {plan.destination.name}: {len(plan.contracts)} job(s) "
            f"paying {plan.advance_total + plan.on_delivery_total}"
        )
        if not fuel_blocked:
            topup = self._opportunistic_fuel_topup(
                pending_fuel=self._pending_fuel_units()
            )
            if topup is not None:
                actions.append("Topping up fuel tank")
        return actions

    def _adopt_contract_trip(
        self, plan: ContractPlan, placed_fuel_sell: bool
    ) -> List[str]:
        """Make ``plan`` this turn's trip and take its first actions."""
        self._current_plan = plan
        self._plan_loaded = False
        self._addon_commodities = set()
        self._turns_without_plan = 0
        self._clear_reposition_intent()
        return self._execute_contract_plan(plan, fuel_blocked=placed_fuel_sell)

    def _maintain_fuel(self) -> List[str]:
        """Keep the tank viable at the docked planet, returning action strings.

        Top up when fuel is for sale locally; otherwise, below the stranding
        reserve, post a standing bid priced to entice a delivery run. Callers
        must not run this in the same turn as a fuel sell: buying into our
        own ask would self-trade.

        Both callees now decline where the local price is bad and a fuel
        station is within tank range: the top-up buys nothing above the
        bunkering premium and the standing bid is not posted at all, because
        :meth:`_refuel_reposition_target` flies the ship to the station this
        turn and a resting bid would only tie up the money the trip needs.
        The structure here is unchanged; those turns simply produce no
        action string.
        """
        planet = self.ship.planet
        fuel_commodity = self._fuel_commodity()
        if planet is None or fuel_commodity is None:
            return []
        market = planet.market
        actions: List[str] = []
        _, fuel_ask = market.get_bid_ask_spread(fuel_commodity)
        if fuel_ask is not None:
            if self._opportunistic_fuel_topup() is not None:
                actions.append("Topping up fuel tank")
        elif self.ship.fuel < self._fuel_reserve_need():
            bid_order = self._post_standing_fuel_bid()
            if bid_order:
                order = market.orders_by_id[bid_order]
                actions.append(
                    f"Standing fuel bid: {order.quantity} at {order.price} "
                    "(no local fuel for sale)"
                )
        return actions

    def _buy_repair_kit(self) -> List[str]:
        """Buy one complete maintenance tier when it is cheaper than the fuel it saves.

        A ship with no kit carries MAINTENANCE_FUEL_UNITS of extra arrival
        reserve on every leg, because the legacy maintenance tier burns tank
        fuel. Owning a kit removes that reserve, so any kit costing less than
        those units at the galaxy fuel reference pays for itself immediately,
        and again at every repair. Buys the cheapest complete tier with a
        resting ask, at that ask, one tier per turn.

        This is the planned purchase; :meth:`Ship._buy_maintenance_supplies`
        remains the fallback for a ship that has already rolled a repair it
        cannot make.
        """
        ship = self.ship
        planet = ship.planet
        if planet is None or self._holds_repair_kit():
            return []
        reference = self._fuel_value_reference()
        budget_ceiling = MAINTENANCE_FUEL_UNITS * (
            reference if reference is not None else float(FUEL_BID_FALLBACK_FLOOR)
        )
        spendable = int(ship.money * 0.9)
        market = planet.market
        registry = ship.simulation.commodity_registry
        best: Optional[Tuple[int, CommodityDefinition, int, int]] = None
        for commodity_id, units, _label in MAINTENANCE_KIT_TIERS:
            commodity = registry.get_commodity(commodity_id)
            if commodity is None:
                continue
            if ship.cargo.get_quantity(commodity) > 0:
                # Partly held: those units are listed as cargo this turn, and
                # bidding for the rest would meet our own ask.
                continue
            _, ask = market.get_bid_ask_spread(commodity)
            if ask is None or ask <= 0:
                continue
            cost = ask * units
            if cost >= budget_ceiling or cost > spendable:
                continue
            if best is None or cost < best[0]:
                best = (cost, commodity, units, ask)
        if best is None:
            return []
        _cost, commodity, units, ask = best
        order_id = market.place_buy_order(ship, commodity, units, ask)
        if not order_id:
            return []
        ship.active_orders[order_id] = f"buy {commodity.id} (repair kit)"
        return [f"Buying repair kit: {units} {commodity.id} at {ask}"]

    def _execute_trade_plan(
        self,
        plan: TradePlan,
        fuel_handled: bool = False,
        prior_actions: Sequence[str] = (),
    ) -> None:
        """Execute a trade plan by placing buy orders for fuel and cargo.

        Buy orders are posted at the plan's bid price and rest in the book,
        filling from the turn's flow of asks; matching executes at each
        seller's ask, never above the bid. Called every accumulating turn,
        it re-posts for whatever the plan still lacks.

        With ``fuel_handled`` the fuel steps are skipped: the caller ran
        :meth:`_maintain_fuel` first because the tank was below the
        stranding reserve, and that bid owns the fuel side of the book this
        turn. Two fuel bids would double-buy and double-reserve the money
        the plan's cargo needs.

        The caller cancels this ship's resting orders before calling, so
        orders placed earlier this turn survive; ``prior_actions`` carries
        what those orders were, since this method owns ``last_action``.
        """
        planet = self.ship.planet
        if planet is None:
            return
        market = planet.market
        fuel_commodity = self._fuel_commodity()

        actions = list(prior_actions)

        # Step 1: buy fuel if needed.
        pending_fuel = 0
        if fuel_commodity is not None and not fuel_handled:
            current_fuel = self.ship.fuel
            # The same quantity _pair_economics funded: the round trip, or
            # the outbound leg plus what the destination demands on arrival,
            # whichever is larger, within the tank.
            fuel_needed = min(
                self.ship.fuel_capacity,
                max(
                    plan.fuel_needed_round_trip,
                    plan.fuel_needed_one_way
                    + self._arrival_fuel_requirement(plan.destination, plan.origin),
                ),
            )

            if current_fuel < fuel_needed:
                # Tank targets are millifuel; the book sells whole units.
                fuel_to_buy = min(
                    milli_to_units_ceil(fuel_needed - current_fuel),
                    milli_to_units_floor(self.ship.fuel_capacity - current_fuel),
                )
                _, fuel_ask = market.get_bid_ask_spread(fuel_commodity)
                fuel_bid = (
                    fuel_ask
                    if fuel_ask is not None
                    else self._flow_value(market, fuel_commodity)
                )
                # Same ceiling every other fuel bid honors, and no spiked buy
                # while a station is reachable; _pair_economics rejects such
                # pairs at adoption, this guards the re-executed plan.
                if fuel_bid is not None:
                    fuel_bid = min(fuel_bid, self._nav.fuel_bid_ceiling())
                if (
                    not self._local_fuel_bunkerable(plan.origin)
                    and self._can_reach_station()
                ):
                    fuel_bid = None

                if fuel_bid is not None and fuel_bid > 0:
                    affordable_fuel = min(fuel_to_buy, self.ship.money // fuel_bid)
                    if affordable_fuel > 0:
                        order_id = market.place_buy_order(
                            self.ship, fuel_commodity, affordable_fuel, fuel_bid
                        )
                        if order_id:
                            actions.append(
                                f"Buying {affordable_fuel} fuel at {fuel_bid}"
                            )
                            self.ship.active_orders[order_id] = "buy fuel"
                            pending_fuel = affordable_fuel

        # Step 2: bid for the plan's remaining cargo at the plan's bid price
        # - not its evaluation price, which is what the cargo is expected to
        # cost. The order rests in the book and fills at sellers' asks, never
        # above the bid.
        bid_price = plan.bid_price_per_unit
        already_held = self._sellable_quantity(plan.commodity)
        still_needed = plan.quantity - already_held
        pending_plan_cargo = 0
        if bid_price > 0 and still_needed > 0:
            # Recompute what is affordable after the fuel purchase.
            money_available = int(self.ship.money * 0.9)  # Keep 10% reserve
            cargo_available = (
                self.ship.cargo_capacity - self.ship.cargo.get_total_quantity()
            )

            quantity = min(still_needed, money_available // bid_price, cargo_available)

            if quantity > 0:
                order_id = market.place_buy_order(
                    self.ship, plan.commodity, quantity, bid_price
                )
                if order_id:
                    actions.append(
                        f"Bidding for {quantity} {plan.commodity.name} at {bid_price} "
                        f"(plan: sell at {plan.destination.name} for ~{plan.expected_sell_price_per_unit})"
                    )
                    self.ship.active_orders[order_id] = f"buy {plan.commodity.id}"
                    pending_plan_cargo = quantity

        # Step 2b: fill the rest of the hold with other goods that also sell
        # at the destination, out of the money and space the plan left over.
        actions.extend(self._place_addon_bids(plan, market, pending_plan_cargo))

        # Step 3: top up toward a full tank with the space and money the plan
        # left over, unless the plan is itself a fuel run or the caller
        # already ran fuel upkeep this turn.
        if plan.commodity.id != "nova_fuel" and not fuel_handled:
            topup_order = self._opportunistic_fuel_topup(pending_fuel=pending_fuel)
            if topup_order:
                actions.append("Topping up fuel tank")

        if actions:
            self.ship.last_action = "; ".join(actions)
        else:
            self.ship.last_action = "Trade plan execution failed - no orders placed"

    def _addon_candidates(
        self, plan: TradePlan, money: int, space: int
    ) -> List[Tuple[int, TradePlan]]:
        """Other goods worth carrying to ``plan.destination`` with what is left.

        Each candidate is evaluated exactly as a plan would be, against the
        destination's bids and flow, but with the pair economics replaced by
        the leftover budget and hold space and no fuel to buy: the trip is
        already paid for by the primary plan. Returns ``(marginal, plan)``
        pairs where marginal is revenue less purchase cost, for candidates
        that clear ``TradePlan.MIN_MARGIN`` on their purchase cost.
        """
        origin = plan.origin
        if money <= 0 or space <= 0:
            return []
        listing_here = {
            order.commodity_type
            for order in origin.market.get_actor_orders(self.ship)["sell"]
        }
        pair = _PairEconomics(
            distance=plan.distance,
            fuel_one_way=plan.fuel_needed_one_way,
            fuel_price=plan.fuel_price_at_origin,
            fuel_reference_price=plan.fuel_price_from_tank,
            fuel_to_buy=0,
            fuel_from_tank_one_way=plan.fuel_needed_one_way,
            expected_maintenance_cost=0,
            money_for_trading=money,
            max_by_cargo=space,
        )
        candidates: List[Tuple[int, TradePlan]] = []
        for commodity in self._nav.exportable_commodities(origin):
            if (
                commodity is plan.commodity
                or commodity.id == "nova_fuel"
                or commodity in listing_here
                or commodity in self._addon_commodities
            ):
                continue
            acquisition = self._origin_acquisition(origin, commodity)
            if acquisition is None:
                continue
            addon = self._evaluate_trade_opportunity(
                origin, plan.destination, commodity, pair=pair, acquisition=acquisition
            )
            if addon is None:
                continue
            marginal = addon.expected_revenue - addon.total_purchase_cost
            if (
                marginal <= 0
                or marginal < addon.total_purchase_cost * TradePlan.MIN_MARGIN
            ):
                continue
            candidates.append((marginal, addon))
        return candidates

    def _place_addon_bids(
        self, plan: TradePlan, market: "Market", reserved_cargo: int
    ) -> List[str]:
        """Bid for up to ADDON_MAX_COMMODITIES extra goods bound for the plan's destination.

        Greedy by marginal profit: the best candidate is bid for, the space it
        takes is removed, and the rest are re-evaluated against the money
        left, since a smaller budget changes what fits. ``reserved_cargo`` is
        hold space the plan's own bid this turn will fill. Order placement
        reserves money, so ``ship.money`` already reflects earlier bids.

        A commodity joins ``_addon_commodities`` once chosen, whether or not
        a bid is placed, so the local sell step leaves its units alone while
        the plan accumulates. The set persists across accumulating turns, so
        add-ons are chosen once per plan and not re-bid later.
        """
        actions: List[str] = []
        ship = self.ship
        space = ship.cargo_capacity - ship.cargo.get_total_quantity() - reserved_cargo
        for _ in range(ADDON_MAX_COMMODITIES):
            money = int(ship.money * 0.9)
            candidates = self._addon_candidates(plan, money, space)
            if not candidates:
                break
            _, addon = max(candidates, key=lambda c: c[0])
            self._addon_commodities.add(addon.commodity)
            held = self._sellable_quantity(addon.commodity)
            quantity = min(
                addon.quantity - held, space, money // addon.bid_price_per_unit
            )
            if quantity <= 0:
                continue
            order_id = market.place_buy_order(
                ship, addon.commodity, quantity, addon.bid_price_per_unit
            )
            if not order_id:
                continue
            ship.active_orders[order_id] = f"buy {addon.commodity.id} (add-on)"
            actions.append(
                f"Add-on: bidding for {quantity} {addon.commodity.name} at "
                f"{addon.bid_price_per_unit} (sell at {plan.destination.name} for "
                f"~{addon.expected_sell_price_per_unit})"
            )
            space -= quantity
        return actions

    def _plan_departure_blocked(self, plan: TradePlan) -> bool:
        """Whether a loaded plan cannot fly to its destination this turn.

        The same fuel and safety test :meth:`decide_travel` applies, asked
        early so a blocked ship lists its hold rather than sitting on it until
        the plan's patience runs out.
        """
        origin = self.ship.planet
        if origin is None or origin is not plan.origin:
            return True
        leg = self.ship.fuel_required(self._nav.distance(origin, plan.destination))
        if self.ship.fuel < leg:
            return True
        return not self._fuel_safe_destination(
            plan.destination, origin, self.ship.fuel - leg
        )

    def _cargo_disposition(self, market: "Market") -> Tuple[bool, int]:
        """Decide whether to sell the hold here, and what fuel that commits.

        Returns ``(should_sell_here, committed_fuel_need)``. The commitment is
        the departure requirement of the trip the cargo is being held for,
        the same number :meth:`decide_travel` gates on, so a ship that funds
        it can actually leave. Zero when selling here.

        Both sides are valued with :meth:`_realizable_value`, which walks bid
        depth, and a load that has already made MAX_CARGO_HOPS hops is sold
        where it is whatever another market forecasts. Cargo that keeps
        hopping burns fuel on every leg against a price that has usually gone
        by arrival, and never turns into cash.
        """
        ship = self.ship
        current_planet = ship.planet
        fuel_commodity = self._fuel_commodity()
        if current_planet is None or fuel_commodity is None:
            return True, 0
        fuel_in_tank = ship.fuel

        # Reachability is about the fuel the ship can have when it leaves,
        # not only what is in the tank. Ships run near the survival target, a
        # handful of units, so judging destinations on tank fuel alone
        # rejected almost every one of them and the cargo was dumped into a
        # local book that had no buyer.
        _, local_fuel_ask = market.get_bid_ask_spread(fuel_commodity)
        fuel_available = fuel_in_tank + units_to_milli(
            self._affordable_local_fuel(current_planet)
        )

        for commodity in self._get_tradeable_commodities():
            quantity = self._sellable_quantity(commodity)
            if quantity <= 0:
                continue
            if self._cargo_hops.get(commodity.id, 0) >= MAX_CARGO_HOPS:
                continue

            local_value = self._realizable_value(market, commodity, quantity)

            for planet in ship.simulation.planets:
                if planet == current_planet:
                    continue

                # Only a trip decide_travel would approve can justify holding
                # cargo: leg plus the escape fuel the destination demands on
                # arrival. Judging reach on the leg alone held cargo for trips
                # the departure gate then refused, forever.
                required_fuel = self._departure_fuel_requirement(planet)
                if fuel_available < required_fuel:
                    continue

                dest_value = self._realizable_value(planet.market, commodity, quantity)
                if dest_value <= 0:
                    continue

                # Compare net values the way decide_travel does: destination
                # revenue minus the fuel to get there, against revenue here. A
                # bare unit-price comparison holds tiny cargoes forever for
                # trips whose fuel cost decide_travel never approves. Fuel
                # already in the tank is charged at the local clearing price,
                # fuel that has to be bought at the price it would actually
                # cost. Only the leg is burned; the arrival reserve is carried,
                # not spent, so it is not a cost of the trip.
                fuel_needed = ship.fuel_required(
                    self._nav.distance(current_planet, planet)
                )
                origin_fuel_price = market.get_avg_price(fuel_commodity) or 10
                from_tank_milli = min(fuel_needed, fuel_in_tank)
                to_buy_milli = fuel_needed - from_tank_milli
                fuel_cost = (
                    from_tank_milli * origin_fuel_price
                    + to_buy_milli * (local_fuel_ask or origin_fuel_price)
                ) / FUEL_MILLI
                dest_net = dest_value - fuel_cost
                if dest_net > local_value * 1.15:
                    # The trip this cargo is being held for is profitable at
                    # the local fuel ask, so the top-up must buy every unit
                    # the departure gate will ask for.
                    return False, required_fuel

        return True, 0

    def _list_cargo_locally(
        self, market: "Market", skip: frozenset[str] = frozenset()
    ) -> Tuple[List[str], List[CommodityDefinition], int]:
        """Offer sellable cargo into the local book, skipping ``skip`` ids.

        Returns the action strings, the commodities that got an order, and the
        units listed. ``skip`` serves the accumulation window, where the
        plan's own commodity is being bought rather than sold.
        """
        actions: List[str] = []
        listed: List[CommodityDefinition] = []
        quantity_listed = 0
        for commodity in self._get_tradeable_commodities():
            if commodity.id in skip:
                continue
            quantity = self._sellable_quantity(commodity)
            if quantity <= 0:
                continue
            sell_actions = self._place_flow_sell_orders(market, commodity, quantity)
            if sell_actions:
                actions.extend(sell_actions)
                listed.append(commodity)
                quantity_listed += quantity
        return actions, listed, quantity_listed

    def _route_stop_trade_actions(self, planet: Planet) -> None:
        """Take on riders and buy fuel while docked for an en-route stop.

        These are the only two things a stop does. The plan, its loaded flag,
        its add-on set and the committed fuel need all survive untouched, so
        arrival at the plan's destination sells the cargo normally. In
        particular the plan lifecycle does not run: the ship is not at the
        plan's origin, so it would read the stop as a diversion and abandon
        the plan. Nor does the release of accepted contracts that opens a
        normal docked turn, which would put back the very contracts a pickup
        stop was made for; they stay aboard until ``start_journey`` loads them
        on the resume, or until the stop is abandoned and the next ordinary
        docked turn releases them.

        A pickup stop accepts the riders on the turn it docks, taking them off
        the board before another ship can. A fuel stop tops the tank up on
        that same turn, so the bid rests in the auction of the turn the
        journey was interrupted, and again on any later turn where the tank
        still does not cover the rest of the route. A pickup-only stop runs
        the top-up only in that second case: it is not here for cheap fuel,
        but the per-leg rounding still has to be paid for before it can leave.
        The fuel the remaining route needs goes in as the top-up's required
        floor, so it is bought rather than left to the bunker budget.
        """
        ship = self.ship
        self._nav.refresh_market_facts(turn=ship.simulation.current_turn)
        ship.pump_fuel(keep_in_hold=self.fuel_cargo_to_keep())

        market = planet.market
        resting = market.get_actor_orders(ship)
        for order in resting["buy"] + resting["sell"]:
            market.cancel_order(order.order_id)

        destination = ship.stop_resume
        remaining_need = (
            ship.fuel_required(self._nav.distance(planet, destination))
            if destination is not None
            else 0
        )
        first_turn = ship.simulation.current_turn == ship.stop_turn

        if ship.stop_for_pickup and destination is not None and first_turn:
            self._accept_riders(destination)

        if (ship.stop_for_fuel and first_turn) or ship.fuel < remaining_need:
            self._opportunistic_fuel_topup(required_floor=remaining_need)
            ship.last_action = f"Stopped at {planet.name}: bidding for fuel"
        elif ship.stop_for_pickup and first_turn:
            ship.last_action = (
                f"Stopped at {planet.name}: {len(self._accepted_contracts())} "
                f"job(s) aboard"
            )
        else:
            ship.last_action = f"Stopped at {planet.name}: waiting to resume"

    def _route_stop_travel(self) -> Optional[Planet]:
        """Resume the interrupted journey once the leg's fuel is in the tank.

        Returns the destination the ship was flying to when it stopped, or
        None while it is still waiting for fuel; :meth:`Ship.take_turn`
        departs with ``start_journey(destination, resuming=True)``, which is
        what loads the contracts a pickup stop took on. The bound on the mode
        lives in ``take_turn`` too, not here.
        """
        ship = self.ship
        destination = ship.stop_resume
        planet = ship.planet
        if destination is None or planet is None:
            return None
        if ship.status != ShipStatus.DOCKED or planet is destination:
            return None
        ship.pump_fuel(keep_in_hold=self.fuel_cargo_to_keep())
        needed = ship.fuel_required(self._nav.distance(planet, destination))
        if ship.fuel < needed:
            return None
        return destination

    def decide_trade_actions(self) -> None:
        """Take this turn's trading actions at the current planet.

        The order matters and is deliberate: cancel resting orders, judge
        last turn's asks, run the plan lifecycle, decide what the cargo is
        for (which fixes this turn's fuel commitment), then fuel upkeep, then
        the orders each branch places. Fuel upkeep sits after the commitment
        so a low-fuel ship funds the trip it just chose, and before every
        early return so the ships that need a rescue bid always post one.

        1. If there is cargo to sell, sell it here or hold it for a better
           market
        2. Otherwise find the best trade plan and execute it
        3. If no profitable trade exists, maintain fuel and wait
        """
        current_planet = self.ship.planet
        if current_planet is None:
            return

        if self.ship.stop_resume is not None:
            self._route_stop_trade_actions(current_planet)
            return

        # The first ship deciding this turn rebuilds the market-fact snapshot;
        # the rest share it.
        self._nav.refresh_market_facts(turn=self.ship.simulation.current_turn)
        self.ship.pump_fuel(keep_in_hold=self.fuel_cargo_to_keep())

        market = current_planet.market
        fuel_commodity = self._fuel_commodity()

        if not fuel_commodity:
            self.ship.last_action = "No trading - fuel commodity not available"
            return

        existing_orders = market.get_actor_orders(self.ship)
        for order in existing_orders["buy"] + existing_orders["sell"]:
            market.cancel_order(order.order_id)

        # Contracts are taken on for a departure, at the departure decision.
        # Anything still unloaded goes back on its board here, and with it any
        # contract trip built around it; both are re-made below if they are
        # still the best thing to do.
        self._release_accepted_contracts()
        if isinstance(self._current_plan, ContractPlan):
            self._current_plan = None

        actions = []
        self._selling_locally = False
        # Now that the hold is whole again, judge last turn's local asks.
        self._refresh_local_sale_staleness()
        # Cargo bookkeeping, before anything prices or values the hold: read
        # this ship's own fills into the cost basis, age the listings that did
        # not fill, and forget commodities the hold has run out of.
        self._ingest_cost_basis()
        self._age_unsold_listings()
        self._forget_empty_cargo_state()

        # Still docked where the cargo was last held for another market: the
        # departure gate refused. Past HOLD_PATIENCE such turns the cargo is
        # listed here instead of waiting for a trip that never leaves.
        if self._hold_planet is current_planet:
            self._hold_refused_turns += 1
        else:
            self._hold_refused_turns = 0
        self._hold_planet = None

        # A reposition intent speaks for one planet only. Anywhere else - the
        # ship flew, or was diverted - it is stale and must not fund fuel.
        intent = self._reposition_intent
        if intent is not None and intent.origin is not current_planet:
            self._clear_reposition_intent()

        # Linger bookkeeping. The flag is consumed here, once: it says the
        # previous docked turn was bought for fuel, not spent waiting, so it
        # must not count against a loaded plan's departure patience below. The
        # planet caps this stop at one linger and is stale anywhere else.
        lingered_last_turn = self._fuel_linger_pending
        self._fuel_linger_pending = False
        if self._fuel_linger_planet is not current_planet:
            self._fuel_linger_planet = None

        # Distress bookkeeping, before anything reads is_distressed. A ship
        # that is docked, empty of trade goods and poorer than one short
        # round trip of fuel is locked out of the planner: every pair fails
        # the cash gate and there is no income without a sale. After
        # DISTRESS_PATIENCE such turns it may sell tank fuel down to its
        # survival target and trade its way back.
        #
        # Holding a plan does not clear the condition. A plan whose bid
        # never fills is re-adopted turn after turn, so requiring
        # ``_current_plan is None`` meant the ships most in need of the exit
        # were exactly the ones that could never reach it. Filling cargo,
        # not merely intending to, is what proves a ship is trading again.
        non_fuel_cargo = any(
            self.ship.cargo.get_quantity(c) > 0
            for c in self._get_tradeable_commodities()
            if c.id != "nova_fuel"
        )
        self._update_distress(
            not non_fuel_cargo and self.ship.money < self._short_trip_cash_floor()
        )

        placed_fuel_sell = False

        # Whether the tank is under the stranding reserve. The upkeep itself
        # runs further down, once this turn's fuel commitment is known; see
        # the ordering comment there.
        fuel_upkeep_ran = self.ship.fuel < self._fuel_reserve_need()

        # Plan lifecycle. Flow-based plans fill a resting bid over several
        # docked turns, then fly the load to the plan's destination.
        accumulating = False
        plan = self._current_plan if isinstance(self._current_plan, TradePlan) else None
        if plan is None:
            self._addon_commodities = set()
        if plan is not None:
            if self.ship.planet is plan.destination:
                # Arrived: the sell logic below disposes of the cargo. The
                # plan lingers only so fuel_cargo_to_keep keeps a fuel
                # delivery's load out of the tank, and is dropped once the
                # cargo is gone.
                self._plan_loaded = False
                if self._sellable_quantity(plan.commodity) <= 0:
                    self._current_plan = None
            elif self.ship.planet is not plan.origin:
                # Diverted by a maintenance or survival reposition: abandon.
                self._current_plan = None
                self._plan_loaded = False
            elif not self._plan_loaded:
                held = self._sellable_quantity(plan.commodity)
                if held >= plan.quantity:
                    self._plan_loaded = True
                    self._plan_turns_left = ACCUMULATION_PATIENCE
                elif self._plan_turns_left > 0:
                    # Keep accumulating: refresh the resting bid, stay docked.
                    # The bid goes in below, after fuel upkeep.
                    self._plan_turns_left -= 1
                    accumulating = True
                elif held > 0:
                    # Patience exhausted: depart with the partial load.
                    self._plan_loaded = True
                    self._plan_turns_left = ACCUMULATION_PATIENCE
                else:
                    # Patience exhausted, nothing filled: replan from scratch.
                    self._current_plan = None
            else:
                # Loaded at origin. Departure happens in decide_travel, so
                # being here next turn means it was blocked, usually on fuel.
                # Wait a bounded while, then release the cargo to the plain
                # sell-or-fly logic. A turn the ship chose to spend bunkering
                # cheap fuel is not a blocked turn and does not spend patience.
                if lingered_last_turn:
                    pass
                elif self._plan_turns_left > 0:
                    self._plan_turns_left -= 1
                else:
                    self._current_plan = None
                    self._plan_loaded = False

        # How long the ship has gone with nothing to do, counted after the
        # lifecycle has dropped whatever did not survive this turn. Adopting
        # either kind of plan below resets it.
        if self._current_plan is None:
            self._turns_without_plan += 1
        else:
            self._turns_without_plan = 0

        # Fuel below the travel reserve is not trade cargo; see
        # _sellable_quantity. Judged after the plan lifecycle, which can drop
        # a fuel plan and with it the tank's standing as cargo.
        has_trade_cargo = any(
            self._sellable_quantity(c) > 0 for c in self._get_tradeable_commodities()
        )

        # What this turn's cargo is for, and so what fuel the ship has
        # committed to buying. Recomputed from scratch, before any fuel order
        # goes in, so a stale commitment can never fund the wrong trip.
        should_sell_here = True
        self._committed_fuel_need = 0
        if accumulating:
            pass  # the plan's own fuel step funds the trip it is loading for
        elif plan is not None and self._plan_loaded:
            # Loaded and still here: the departure gate refused, almost always
            # on fuel. Commit to exactly what that gate asks for, or the ship
            # tops up to the survival target and is refused again next turn.
            self._committed_fuel_need = self._departure_fuel_requirement(
                plan.destination
            )
        elif has_trade_cargo:
            should_sell_here, self._committed_fuel_need = self._cargo_disposition(
                market
            )
            if not should_sell_here:
                if self._hold_refused_turns >= HOLD_PATIENCE:
                    should_sell_here = True
                    self._committed_fuel_need = 0
                else:
                    self._hold_planet = current_planet
        elif self._reposition_intent is not None:
            # Empty, but committed to flying somewhere worth trading from and
            # short of the fuel for it. Same commitment a held cargo makes, so
            # the top-up buys what the departure gate will ask for.
            self._committed_fuel_need = self._departure_fuel_requirement(
                self._reposition_intent.target
            )

        # A payload aboard outranks every other use of the tank: the ship owes
        # a delivery and core strands the payload if it sits here too long.
        pinned = self._pinned_contract_destination()
        if pinned is not None:
            self._committed_fuel_need = self._departure_fuel_requirement(pinned)

        # Fly to a fuel station rather than pay a spiked local ask. Decided
        # once here, after the fuel commitment it reads, and cached for
        # decide_travel so both agree on the turn's plan. An accumulating
        # plan is left alone: its own fuel step funds the trip it is loading
        # for, and its patience is bounded, so a ship that really is stuck
        # reaches this branch within a few turns anyway.
        self._refuel_reposition = (
            None if accumulating else self._refuel_reposition_target()
        )

        # Fuel upkeep, whenever the tank is under the stranding reserve, and
        # ahead of every branch that can end the turn: an accumulating plan
        # holds the turn for up to ACCUMULATION_PATIENCE turns and adopting a
        # plan returns outright, so upkeep placed after them never ran for the
        # ships that needed it, and a dry ship with a plan it could not fly
        # posted no rescue bid at all. It runs after the commitment above so
        # the top-up funds this turn's decision rather than last turn's. No
        # fuel sell can have happened yet, and none can happen later this turn
        # either: below the reserve _sellable_quantity yields no fuel, so
        # there is no self-trade to guard against here.
        if fuel_upkeep_ran:
            actions.extend(self._maintain_fuel())

        # A repair kit removes the maintenance buffer from every arrival
        # reserve this ship funds, so it goes in ahead of cargo listing and
        # cargo buying: the reserve it frees is worth more than the hold
        # space it takes.
        actions.extend(self._buy_repair_kit())

        if self._refuel_reposition is not None:
            # Departing this turn. Listing the hold now would leave resting
            # asks behind and strand the cargo's money, and adopting a plan
            # here would only be abandoned in decide_travel.
            actions.append(f"Repositioning to {self._refuel_reposition.name} to refuel")
            self.ship.last_action = "; ".join(actions)
            return

        listed_locally = 0
        if accumulating and plan is not None:
            # Cargo the plan is not about would otherwise sit unlisted for the
            # whole accumulation window. Fuel is held back: the plan's own
            # fuel bid goes in below and must not meet an ask of ours.
            sell_actions, listed, _ = self._list_cargo_locally(
                market,
                skip=frozenset(
                    {plan.commodity.id, "nova_fuel"}
                    | {c.id for c in self._addon_commodities}
                ),
            )
            if listed:
                actions.extend(sell_actions)
                self._record_local_sale(current_planet, listed)
                self._note_listed(listed)
            self._execute_trade_plan(
                plan, fuel_handled=fuel_upkeep_ran, prior_actions=actions
            )
            return

        # Cargo that is not leaving this turn goes in the book. A loaded plan
        # whose departure the fuel gate refuses is the case the old code left
        # unlisted: it waited out ACCUMULATION_PATIENCE turns with the hold
        # full and no ask anywhere. Listing costs it nothing when it does
        # leave, since start_journey cancels resting orders before departure.
        # Cargo held for another market is listed once HOLD_PATIENCE turns of
        # refused departures have shown the trip is not happening.
        list_here = should_sell_here
        if has_trade_cargo and self._plan_loaded and plan is not None:
            list_here = self._plan_departure_blocked(plan)
        if has_trade_cargo and list_here:
            sell_actions, listed, listed_locally = self._list_cargo_locally(market)
            if listed:
                actions.extend(sell_actions)
                placed_fuel_sell = any(c.id == "nova_fuel" for c in listed)
                self._record_local_sale(current_planet, listed)
                self._note_listed(listed)
                # Hold the ship here only for a sale it actually chose, and
                # only while that sale is fresh: one that already sat a full
                # turn unfilled has earned no more patience. A loaded plan is
                # never pinned here by an ask it may yet fly away from.
                if (
                    should_sell_here
                    and not self._plan_loaded
                    and not self._local_sale_stale
                ):
                    self._selling_locally = True
        if has_trade_cargo and not should_sell_here:
            # decide_travel will fly it there.
            actions.append("Holding cargo for better price elsewhere")

        # No cargo, or cargo listed locally that nobody has taken for a full
        # turn: look for a plan. Without the second case a ship holding an
        # unsellable load could never replan, which is the other half of the
        # idle livelock. Plan sizing already treats the held cargo as
        # occupied hold space (see _pair_economics).
        if not has_trade_cargo or (
            listed_locally > 0 and self._local_sale_stale and not self._plan_loaded
        ):
            # Adopt the best trade plan. The search skips whatever this ship
            # is listing here, so it cannot plan to buy back its own cargo.
            plan = self._find_best_trade_plan()

            # A trip flown for contract payments alone competes with that haul
            # rather than waiting for it to fail. Both are judged on what they
            # earn per turn they occupy, so a short well-paid job beats a long
            # thin haul; a trade plan wins ties, since it also moves goods the
            # economy wants. A contract trip still has to cover its own fuel
            # and maintenance, which _best_contract_trip enforces. The haul is
            # credited with the riders its own route could carry, so a haul
            # that also clears the board's queue is not displaced by a job
            # worth slightly more on its own.
            contract_plan = None if pinned is not None else self._best_contract_trip()
            plan_turn_value = (
                self._trip_turn_value(
                    plan.expected_profit
                    + self._plan_rider_value(plan, self.ship.free_hold()),
                    plan.destination,
                )
                if plan is not None
                else 0.0
            )
            if contract_plan is not None and (
                plan is None
                or self._trip_turn_value(
                    contract_plan.expected_profit, contract_plan.destination
                )
                > plan_turn_value
            ):
                actions.extend(
                    self._adopt_contract_trip(contract_plan, placed_fuel_sell)
                )
                self.ship.last_action = "; ".join(actions)
                return

            if plan:
                self._current_plan = plan
                self._plan_loaded = False
                self._plan_turns_left = ACCUMULATION_PATIENCE
                self._addon_commodities = set()
                self._turns_without_plan = 0
                # The commitment spoke for cargo the ship was holding for a
                # trip, or for a reposition it no longer needs; the plan's own
                # fuel step owns the tank now.
                self._committed_fuel_need = 0
                self._clear_reposition_intent()
                self._execute_trade_plan(
                    plan,
                    # A fuel ask of ours is resting: bidding for fuel now
                    # would buy from ourselves.
                    fuel_handled=fuel_upkeep_ran or placed_fuel_sell,
                    prior_actions=actions,
                )
                return  # _execute_trade_plan sets last_action
            self._current_plan = None

        elif (
            list_here
            and self._current_plan is None
            and pinned is None
            and not self._selling_locally
        ):
            # The ship is listing cargo here, so the trade-plan search above is
            # skipped until the listing goes a full turn untouched. A partly
            # filled listing never goes stale, which left ships sitting beside
            # a paying job for turns on end: 188 of 557 such docked turns at 12
            # planets had a viable contract group out of that very planet. A
            # contract trip buys nothing but fuel, so it does not compete with
            # the sale; the asks stay in the book until the ship departs.
            contract_plan = self._best_contract_trip()
            if contract_plan is not None:
                actions.extend(
                    self._adopt_contract_trip(contract_plan, placed_fuel_sell)
                )
                self.ship.last_action = "; ".join(actions)
                return

        # Fuel upkeep for the ships that did not need it before the plan
        # branches: above the stranding reserve this is opportunistic
        # bunkering, and it must not run in the same turn as a fuel sell,
        # since buying into our own ask would self-trade.
        if not fuel_upkeep_ran and not placed_fuel_sell:
            actions.extend(self._maintain_fuel())

        if actions:
            self.ship.last_action = "; ".join(actions)
        else:
            self.ship.last_action = "No trade actions (waiting for opportunities)"

    def decide_travel(self) -> Optional[Planet]:
        """Choose this turn's destination and take on any riders bound there.

        Every real departure - a loaded plan, a cargo hop, a reposition, a
        contract trip - goes past this one place, so contracts to where the
        ship is already going are picked up whatever the reason for the trip.
        """
        destination = self._choose_destination()
        if destination is not None:
            self._accept_riders(destination)
        return destination

    def _choose_destination(self) -> Optional[Planet]:
        """Decide whether to travel, and where.

        1. Stay docked while a plan is accumulating; buying happens locally
        2. With cargo, pick the destination with the highest value after
           fuel cost
        3. Empty and with nothing to source here, reposition
        """
        if self.ship.status != ShipStatus.DOCKED:
            return None

        if not self.ship.planet or not self.ship.simulation.planets:
            return None

        # Docked for an en-route refuel stop: the only decision is whether the
        # interrupted journey can resume. Returning before the reset below
        # keeps the original trip's turn value, which the resumed leg may need
        # again if it passes another cheap planet.
        if self.ship.stop_resume is not None:
            return self._route_stop_travel()

        self._departed_trip_turn_value = 0.0

        # Refuelling reposition, chosen this turn by decide_trade_actions.
        # It outranks the local-sale hold and any unloaded plan: the ship is
        # short of fuel where fuel is unaffordable, and nothing it can do
        # here is worth more than being able to leave. A loaded plan survives
        # only when its destination is the station itself.
        station = self._refuel_reposition
        if station is not None:
            self._selling_locally = False
            if not (
                self._plan_loaded
                and self._current_plan is not None
                and self._current_plan.destination is station
            ):
                self._current_plan = None
                self._plan_loaded = False
            self._clear_reposition_intent()
            # An empty reposition loses nothing by taking a cheap-fuel stop
            # en route, and this trip earns nothing per turn of its own.
            self._departed_trip_turn_value = 0.0
            return station

        # We committed to selling here this turn. Departing would strand the
        # fresh sell orders in the book, or with cancel-on-depart abort the
        # sale decide_trade_actions just chose. This holds for one turn only:
        # once the asks have sat a full turn unfilled, decide_trade_actions
        # stops setting the flag and the destination logic below runs.
        if self._selling_locally:
            return None

        # Ensure the galaxy's per-turn market-fact snapshot is current.
        self._nav.refresh_market_facts(turn=self.ship.simulation.current_turn)
        self.ship.pump_fuel(keep_in_hold=self.fuel_cargo_to_keep())

        fuel_commodity = self._fuel_commodity()
        if not fuel_commodity:
            return None

        current_planet = self.ship.planet
        fuel_available = self.ship.fuel

        # A loaded contract pins the ship to its destination: the payload is
        # aboard and is owed a delivery, so no cargo hop or reposition runs
        # while it is. The wait is bounded by the stranding patience in
        # Ship._age_undelivered_contracts.
        pinned = self._pinned_contract_destination()
        if pinned is not None:
            fuel_needed = self.ship.fuel_required(
                self._nav.distance(current_planet, pinned)
            )
            if fuel_available >= fuel_needed and self._fuel_safe_destination(
                pinned, current_planet, fuel_available - fuel_needed
            ):
                self._departed_trip_turn_value = 0.0
                return pinned
            return None

        # A contract trip is flown for the payments, so it departs as soon as
        # the fuel gate allows; there is no cargo to accumulate.
        plan = self._current_plan
        if isinstance(plan, ContractPlan) and current_planet is plan.origin:
            fuel_needed = self.ship.fuel_required(
                self._nav.distance(current_planet, plan.destination)
            )
            if fuel_available >= fuel_needed and self._fuel_safe_destination(
                plan.destination, current_planet, fuel_available - fuel_needed
            ):
                self._departed_trip_turn_value = self._trip_turn_value(
                    plan.expected_profit, plan.destination
                )
                return plan.destination
            return None

        # Plan-driven travel: stay docked while accumulating; once loaded,
        # fly to the destination if fuel and safety allow. Otherwise stay and
        # let fuel upkeep work; the loaded-phase patience in
        # decide_trade_actions bounds the wait.
        if isinstance(plan, ContractPlan):
            plan = None
        if plan is not None and current_planet is plan.origin:
            if not self._plan_loaded:
                return None
            fuel_needed = self.ship.fuel_required(
                self._nav.distance(current_planet, plan.destination)
            )
            if fuel_available >= fuel_needed and self._fuel_safe_destination(
                plan.destination, current_planet, fuel_available - fuel_needed
            ):
                turn_value = self._trip_turn_value(
                    plan.expected_profit, plan.destination
                )
                if self._linger_to_bunker(turn_value):
                    return None
                self._departed_trip_turn_value = turn_value
                # The cargo is going to the market the plan priced it for, so
                # the markdown clock starts over there.
                self._unsold_turns.clear()
                return plan.destination
            return None

        # Reserve fuel is not cargo.
        commodities = self._get_tradeable_commodities()
        cargo_to_sell = {
            c: self._sellable_quantity(c)
            for c in commodities
            if self._sellable_quantity(c) > 0
        }

        if not cargo_to_sell:
            # No cargo. If a profitable export can be sourced here, stay and
            # let decide_trade_actions buy it. Otherwise reposition empty
            # toward the best sourcing planet rather than sit where nothing is
            # worth exporting.
            if self._find_best_trade_plan() is not None:
                # Sourcing here beats repositioning, so any standing intent is
                # abandoned now. Leaving it set had the next docked turn
                # commit fuel to a target this ship had already given up on,
                # and buy it before plan adoption cleared the intent.
                self._clear_reposition_intent()
                return None
            reposition = self._reposition_intent_target(
                fuel_available
            ) or self._find_reposition_target(fuel_available, fuel_commodity)
            if reposition is not None:
                return self._reposition_destination(reposition, fuel_available)
            self._clear_reposition_intent()
            # Nothing profitable anywhere. Idling is only safe where fuel can
            # be bought. On a fuel desert every waiting turn risks the tank
            # dropping below the escape threshold, and no local producer can
            # answer a rescue bid. Move to where refueling is plausible while
            # we still can. A supplier who traded recently counts here even
            # with an empty book: the question is whether anyone local can
            # ever fill a standing bid, not whether fuel is liftable today.
            if not (
                self._fuel_purchasable_at(current_planet)
                or self._nav.fuel_traded_recently(current_planet)
            ):
                return self._survival_reposition_target(fuel_available)
            return None

        best_planet = None
        best_expected_value = 0.0

        for destination in self.ship.simulation.planets:
            if destination == current_planet:
                continue

            distance = self._nav.distance(current_planet, destination)
            fuel_needed = self.ship.fuel_required(distance)

            if fuel_available < fuel_needed:
                continue

            # Never fly cargo into a fuel dead end.
            if not self._fuel_safe_destination(
                destination, current_planet, fuel_available - fuel_needed
            ):
                continue

            dest_market = destination.market
            total_value = 0

            for commodity, quantity in cargo_to_sell.items():
                total_value += self._realizable_value(dest_market, commodity, quantity)

            # Fuel is costed at origin prices.
            origin_fuel_price = (
                current_planet.market.get_avg_price(fuel_commodity) or 10
            )
            fuel_cost = fuel_needed * origin_fuel_price / FUEL_MILLI
            # Riders the hop could take on. The hop is already paid for by the
            # cargo aboard, so this only decides between destinations.
            net_value = (
                total_value
                - fuel_cost
                + self._route_rider_value(
                    current_planet, destination, self.ship.free_hold()
                )
            )

            if net_value > best_expected_value:
                best_expected_value = net_value
                best_planet = destination

        if best_planet is not None:
            turn_value = self._trip_turn_value(best_expected_value, best_planet)
            if self._linger_to_bunker(turn_value):
                return None
            self._departed_trip_turn_value = turn_value
            # A cargo hop: cargo flown somewhere else to sell, outside any
            # plan. Counted per commodity aboard so _cargo_disposition can
            # stop the second one.
            for commodity in cargo_to_sell:
                self._cargo_hops[commodity.id] = (
                    self._cargo_hops.get(commodity.id, 0) + 1
                )
        return best_planet

    def _survival_reposition_target(self, fuel_available: int) -> Optional[Planet]:
        """Nearest reachable planet where refueling is plausible.

        This is the last resort, so it accepts weaker evidence than a plan
        gate does. First choice is a planet with a live fuel ask, stations
        ahead of thinner books; second is one where fuel traded in the recent
        window; third is one that has ever traded fuel, since a producer
        there can answer a standing rescue bid, unlike on a never-traded fuel
        desert. Returns None when no such planet is in range; then staying
        put and posting a standing bid is all that is left.

        Candidates that also clear :meth:`_fuel_safe_destination` sort ahead
        of ones that do not, so a hop that leaves an escape route always
        beats an equally plausible hop that does not.

        An unsafe candidate - one the ship would land on below its arrival
        floor - is only acceptable in tier 0, where there is a live ask to
        buy up from on landing. Unsafe hops into the history-only tiers are
        dropped: they were a sink, arriving under the floor at a planet with
        nothing to lift and no way back out, and ships that took them sat
        for hundreds of turns. Staying put with a standing bid is the better
        of two bad options there.
        """
        current = self.ship.planet
        if current is None:
            return None
        fuel_commodity = self._fuel_commodity()
        if fuel_commodity is None:
            return None
        best: Optional[tuple[int, int, int, float, Planet]] = None
        for planet in self.ship.simulation.planets:
            if planet is current:
                continue
            distance = self._nav.distance(current, planet)
            leg = self.ship.fuel_required(distance)
            if fuel_available < leg:
                continue
            station = self._nav.fuel_station_at(planet)
            if station or self._fuel_purchasable_at(planet):
                tier = 0
            elif self._nav.fuel_traded_recently(planet):
                tier = 1
            elif planet.market.has_price_signal(fuel_commodity):
                tier = 2
            else:
                continue
            unsafe = (
                0
                if self._fuel_safe_destination(planet, current, fuel_available - leg)
                else 1
            )
            if unsafe and tier > 0:
                continue
            key = (unsafe, tier, 0 if station else 1, distance)
            if best is None or key < (best[0], best[1], best[2], best[3]):
                best = (unsafe, tier, 0 if station else 1, distance, planet)
        return best[4] if best is not None else None

    def _find_reposition_target(
        self, fuel_available: int, fuel_commodity: "CommodityDefinition"
    ) -> Optional[Planet]:
        """Pick a planet to fly to empty when nothing here is worth exporting.

        Surveys up to REPOSITION_ORIGIN_CANDIDATES reachable planets, nearest
        first, as candidate origins and finds the best profitable export plan
        from each, credited with the riders that plan's route would carry.
        Returns the origin backing the most profitable one, or None if none is
        reachable or profitable.

        Reach is the tank *plus* the fuel the ship could buy here, since
        repositioning may buy fuel like any other trip. Judging it on the
        tank alone was the single biggest source of idle turns: ships running
        at a couple of units of fuel, with hundreds of credits and a fuel ask
        in the book in front of them, could neither plan (the round-trip cash
        gate refuses a plan they cannot fund) nor move. The purchase is not
        free, so it is charged against the plan the origin backs, and the
        arrival floor in :meth:`_fuel_safe_destination` is applied to the
        fuel the ship would actually land with.

        Buying it is the caller's job: :meth:`_reposition_destination` records
        the intent so the next docked turn commits ``_committed_fuel_need``
        to it, exactly as a ship holding cargo for a trip does.

        A ship that has gone REPOSITION_CONTRACT_PATIENCE docked turns with no
        plan at all takes a weaker second reason to move: any surveyed origin
        whose contract queue pays more than its own fuel and maintenance. That
        trip need not beat the trade plans elsewhere, because the ship has
        nothing to compare it against, and a queue builds up exactly on the
        planets no export would draw a ship to.
        """
        current_planet = self.ship.planet
        if current_planet is None:
            return None

        starved = self._turns_without_plan >= REPOSITION_CONTRACT_PATIENCE

        # Cold galaxy: no commodity has both supply and demand anywhere, so
        # no origin can back a plan; skip the per-origin surveys. A starved
        # ship still surveys, since a contract queue needs no trade signal.
        if not starved and not self._nav.has_any_trade_signal():
            return None

        buyable_units = self._affordable_local_fuel(current_planet)
        _, local_fuel_ask = current_planet.market.get_bid_ask_spread(fuel_commodity)
        fuel_price = local_fuel_ask if buyable_units > 0 and local_fuel_ask else 0
        reach = fuel_available + units_to_milli(buyable_units)

        best_origin: Optional[Planet] = None
        best_profit = 0
        # Fallback for a plan-starved ship: the best contract queue found,
        # judged on its own profit rather than against the trade plans.
        queue_origin: Optional[Planet] = None
        queue_profit = 0
        surveyed = 0

        for origin in self._nav.planets_by_proximity(current_planet):
            if surveyed >= REPOSITION_ORIGIN_CANDIDATES:
                break
            # Must be able to reach this origin empty, out of the tank or the
            # local book, and arriving there must leave an escape route.
            distance_to_origin = self._nav.distance(current_planet, origin)
            fuel_to_origin = self.ship.fuel_required(distance_to_origin)
            if reach < fuel_to_origin:
                continue
            # The departure gate asks for the leg plus the arrival reserve, so
            # that is what the trip has to be funded to.
            departure_need = self._departure_fuel_requirement(origin)
            if reach < departure_need:
                continue
            fuel_to_buy_milli = max(0, departure_need - fuel_available)
            fuel_to_buy_units = milli_to_units_ceil(fuel_to_buy_milli)
            if not self._fuel_safe_destination(
                origin,
                current_planet,
                fuel_available + fuel_to_buy_milli - fuel_to_origin,
            ):
                continue

            surveyed += 1
            plan = self._best_plan_from(origin)
            contract_trip = self._best_contract_trip(origin)
            # A planet with a queue of jobs and nothing worth exporting is
            # still worth flying to, which is the case v1 migration could not
            # reach: the poor planets ships do not call at. The queue is valued
            # against the whole hold, since the ship arrives empty.
            contract_profit = (
                contract_trip.expected_profit if contract_trip is not None else 0
            )
            if starved and contract_profit > queue_profit:
                queue_profit = contract_profit
                queue_origin = origin
            # A plan out of this origin can also carry the riders its route
            # serves, so the two are alternatives rather than a sum: the
            # origin is worth whichever use of the hold pays more.
            plan_value = (
                plan.expected_profit
                + self._plan_rider_value(plan, self.ship.cargo_capacity)
                if plan is not None
                else 0
            )
            best_there = max(plan_value, contract_profit)
            if best_there <= 0:
                continue
            net_profit = best_there - fuel_to_buy_units * fuel_price
            if net_profit > best_profit:
                best_profit = net_profit
                best_origin = origin

        return best_origin if best_origin is not None else queue_origin

    def _reposition_destination(
        self, target: Planet, fuel_available: int
    ) -> Optional[Planet]:
        """Fly the empty reposition now, or stay a turn and fund its fuel.

        :meth:`_find_reposition_target` counts fuel the ship could buy here,
        so the chosen origin is often out of reach until that purchase
        settles. Recording the intent makes the next docked turn commit
        ``_committed_fuel_need`` to this trip, which is the mechanism
        :meth:`_opportunistic_fuel_topup` honours even at a scarcity price.
        """
        current_planet = self.ship.planet
        if current_planet is None:
            return None
        leg = self.ship.fuel_required(self._nav.distance(current_planet, target))
        if fuel_available >= leg and self._fuel_safe_destination(
            target, current_planet, fuel_available - leg
        ):
            self._clear_reposition_intent()
            return target
        self._reposition_intent = _RepositionIntent(
            origin=current_planet, target=target
        )
        return None

    def _reposition_intent_target(self, fuel_available: int) -> Optional[Planet]:
        """The standing reposition target, if it is still worth waiting for.

        The intent is sticky. Re-surveying every turn let a ship waiting for
        its fuel order to fill re-aim at a different origin each turn, so the
        fuel it had committed to was never the fuel the next turn's trip
        needed and it never left. The survey only runs again once the
        standing target stops being reachable and fundable from here, which
        is the same pair of tests :meth:`_find_reposition_target` applies.
        """
        intent = self._reposition_intent
        current_planet = self.ship.planet
        if intent is None or current_planet is None:
            return None
        if intent.origin is not current_planet:
            return None
        reach = fuel_available + units_to_milli(
            self._affordable_local_fuel(current_planet)
        )
        departure_need = self._departure_fuel_requirement(intent.target)
        if reach < departure_need:
            return None
        leg = self.ship.fuel_required(self._nav.distance(current_planet, intent.target))
        fuel_to_buy = max(0, departure_need - fuel_available)
        if not self._fuel_safe_destination(
            intent.target, current_planet, fuel_available + fuel_to_buy - leg
        ):
            return None
        return intent.target

    def _clear_reposition_intent(self) -> None:
        """Forget any pending empty reposition."""
        self._reposition_intent = None


class Ship:
    """A trading ship that travels between planets."""

    def __init__(
        self,
        name: str,
        simulation: "Simulation",
        planet: Optional[Planet] = None,
        cargo_capacity: int = 100,
        fuel_capacity: int = BASE_FUEL_CAPACITY,
        fuel_efficiency: float = 1.0,
        initial_money: int = 1000,
    ) -> None:
        self.name = name
        self.money = initial_money
        self.reserved_money = 0
        self.planet = planet
        self.destination: Optional[Planet] = None  # None while docked
        # Lane route being flown: origin first, destination last, every
        # consecutive pair joined by a star lane. Empty while docked.
        self.route: List[Planet] = []
        # Fuel charged for the current journey's whole route, and the lane
        # distance from the origin to each planet in ``route``. Both are set
        # at departure and are what an en-route refuel stop refunds and
        # measures itself against.
        self.route_fuel_charged = 0
        self.route_cumulative_distance: List[float] = []
        # En-route stop: the destination to fly on to, the turn the stop
        # began, and what the stop is for. One stop may serve both reasons.
        # ``stop_resume`` is None outside a stop. The two turn lists count the
        # stops of each kind this ship has made.
        self.stop_resume: Optional[Planet] = None
        self.stop_turn = 0
        self.stop_for_fuel = False
        self.stop_for_pickup = False
        self.refuel_stop_turns: List[int] = []
        self.pickup_stop_turns: List[int] = []
        self.cargo = Inventory()
        self.inventory = self.cargo  # Alias for compatibility with market code
        self.cargo_capacity = cargo_capacity
        # Transport jobs this ship has taken on, accepted or loaded; see
        # core/contracts.py. Their hold units are committed space, so
        # free_hold, not the raw cargo total, is what a buy decision has.
        self.contracts: List[Contract] = []
        # Consecutive docked turns spent somewhere that is not the
        # destination of a loaded contract. Past CONTRACT_STRAND_PATIENCE the
        # payload is put down where the ship is rather than ridden around.
        self.docked_turns_with_undelivered_contracts = 0
        # The tank is separate from the hold. ``fuel`` is what the ship
        # burns; nova_fuel in ``cargo`` is trade goods like anything else.
        # Market fills land in the hold, and ``pump_fuel`` moves them into
        # the tank at the start of each docked turn.
        # Both the tank and its capacity are millifuel; see FUEL_MILLI.
        self.fuel = 0
        self.fuel_capacity = fuel_capacity
        self.fuel_efficiency = fuel_efficiency  # Fuel burn is divided by this
        self.travel_progress = 0.0  # 0.0 to 1.0
        self.travel_time = 0  # Turns for the current journey
        self.active_orders: Dict[
            str, str
        ] = {}  # Track active order IDs and their types
        self.last_action = "None"
        self.status = ShipStatus.DOCKED
        self.simulation = simulation
        # Turn this ship last began a journey; 0 until its first departure.
        # Activity KPIs use this to spot ships that sit docked indefinitely
        # even when they are not "stranded" by the fuel-ask definition.
        self.last_departure_turn = 0
        # Every turn a journey actually started, oldest first. Uncapped: the
        # summary KPI only ever looks at a short recent window, and the list
        # is small relative to a run's other per-actor bookkeeping.
        self.departure_turns: List[int] = []
        self.food_consumed_this_turn = (
            True  # Ships don't eat, but needed for compatibility
        )
        self.drives: list[
            "ActorDrive"
        ] = []  # Ships don't have drives, but keep empty list for interface compatibility

        self.brain = TraderBrain(self)

    @property
    def refuel_stops(self) -> int:
        """En-route refuel stops this ship has made."""
        return len(self.refuel_stop_turns)

    @property
    def fuel_units(self) -> float:
        """Tank contents in market units, for display only (0.001 resolution)."""
        return self.fuel / FUEL_MILLI

    @property
    def fuel_capacity_units(self) -> float:
        """Tank capacity in market units, for display only."""
        return self.fuel_capacity / FUEL_MILLI

    @property
    def pickup_stops(self) -> int:
        """En-route stops this ship has made to take contracts aboard."""
        return len(self.pickup_stop_turns)

    def clear_stop(self) -> None:
        """Leave the en-route stop mode, whatever the stop was for."""
        self.stop_resume = None
        self.stop_for_fuel = False
        self.stop_for_pickup = False

    def contract_hold_units(self) -> int:
        """Hold space committed to contracts.

        Accepted contracts count as well as loaded ones: the space is spoken
        for from acceptance, so a ship cannot take a job and then fill the
        hold the job needs.
        """
        return sum(
            contract.payload.hold_units
            for contract in self.contracts
            if contract.status in (ContractStatus.ACCEPTED, ContractStatus.LOADED)
        )

    def free_hold(self) -> int:
        """Hold units available for cargo, after contracts."""
        return (
            self.cargo_capacity
            - self.cargo.get_total_quantity()
            - self.contract_hold_units()
        )

    def pump_fuel(self, keep_in_hold: int = 0) -> int:
        """Move nova_fuel from the hold into the tank, up to capacity.

        ``keep_in_hold`` units stay in the hold as trade cargo; a fuel
        delivery plan passes its load so the pump does not eat the cargo.
        Returns the market units moved. The hold trades whole units, so the
        tank only ever takes on whole units of room.
        """
        fuel_commodity = self.simulation.commodity_registry.get_commodity("nova_fuel")
        if fuel_commodity is None:
            return 0
        in_hold = self.cargo.get_quantity(fuel_commodity)
        movable_units = max(0, in_hold - max(0, keep_in_hold))
        room_milli = max(0, self.fuel_capacity - self.fuel)
        moved_units = min(movable_units, milli_to_units_floor(room_milli))
        if moved_units > 0:
            self.cargo.remove_commodity(fuel_commodity, moved_units)
            self.fuel += units_to_milli(moved_units)
        return moved_units

    def route_distance(self, origin: Planet, destination: Planet) -> float:
        """Length of the shortest star-lane route between two planets.

        Ships fly along lanes, so fuel and travel time are charged on this,
        not the straight-line distance.
        """
        return get_navigator(self.simulation).distance(origin, destination)

    @staticmethod
    def calculate_fuel_needed(distance: float) -> int:
        """Baseline millifuel for a journey of ``distance``, ignoring efficiency.

        Planning for a specific ship must use :meth:`fuel_required`, which
        applies the ship's fuel efficiency the same way departure does.
        """
        return fuel_needed_for(distance, 1.0)

    def fuel_required(self, distance: float) -> int:
        """Efficiency-adjusted millifuel this ship burns for ``distance``.

        One ceiling to the thousandth, applied identically here and at
        departure so planning and consumption never disagree; otherwise
        ships with efficiency < 1.0 under-reserve fuel and strand.
        """
        return fuel_needed_for(distance, self.fuel_efficiency)

    def check_maintenance(self) -> bool:
        """Roll whether the ship needs maintenance before this departure."""
        return random.random() < MAINTENANCE_CHANCE

    def perform_maintenance(self) -> bool:
        """Attempt to perform maintenance on the ship.

        Tries tiered maintenance goods from best to worst:
        - ship_components, 1 unit: best quality
        - ship_parts, 2 units: medium quality
        - ship_supplies, 3 units: basic quality
        - nova_fuel, 5 units: legacy fallback

        Returns True if maintenance succeeded, False if supplies are lacking.
        """
        registry = self.simulation.commodity_registry

        for commodity_id, qty, label in MAINTENANCE_TIERS:
            commodity = registry.get_commodity(commodity_id)
            if commodity is None:
                continue
            if commodity_id == "nova_fuel":
                # The fuel tier burns tank fuel, not hold cargo. The tier
                # quantity is market units; the tank is millifuel.
                qty_milli = units_to_milli(qty)
                if self.fuel < qty_milli:
                    continue
                self.fuel -= qty_milli
            elif self.cargo.has_quantity(commodity, qty):
                self.cargo.remove_commodity(commodity, qty)
            else:
                continue
            self.status = ShipStatus.DOCKED
            self.last_action = f"Performed maintenance using {qty} {label}"
            return True

        self.last_action = "Cannot perform maintenance - insufficient supplies"
        return False

    def _maintenance_stock(self, commodity: "CommodityDefinition") -> int:
        """Units of a maintenance good on hand: tank plus hold for fuel, else the hold."""
        if commodity.id == "nova_fuel":
            return milli_to_units_floor(self.fuel) + self.cargo.get_quantity(commodity)
        return self.cargo.get_quantity(commodity)

    def _buy_maintenance_supplies(self) -> None:
        """Buy maintenance goods from the local market when stranded.

        A ship that rolls a maintenance need on departure is locked out of
        the trading branch until repaired, so without this it could never
        acquire the supplies to repair itself. Buys the cheapest available
        maintenance tier; the order fills at end of turn and
        perform_maintenance succeeds next turn.
        """
        if self.planet is None:
            return
        market = self.planet.market
        registry = self.simulation.commodity_registry

        # Release any money reserved by stale orders so it can fund supplies.
        existing = market.get_actor_orders(self)
        for order in existing["buy"] + existing["sell"]:
            market.cancel_order(order.order_id)

        tiers = [(tier[0], tier[1]) for tier in MAINTENANCE_TIERS]
        for commodity_id, qty_needed in tiers:
            commodity = registry.get_commodity(commodity_id)
            if commodity is None:
                continue
            shortfall = qty_needed - self._maintenance_stock(commodity)
            if shortfall <= 0:
                continue
            _, ask = market.get_bid_ask_spread(commodity)
            if ask is None or ask <= 0:
                continue
            affordable = min(shortfall, self.money // ask)
            if affordable > 0:
                order_id = market.place_buy_order(self, commodity, affordable, ask)
                if order_id:
                    self.active_orders[order_id] = f"buy {commodity_id} (maintenance)"
                    self.last_action = (
                        f"Buying {affordable} {commodity_id} to enable maintenance"
                    )
                    return

        # No tier has an ask to lift: post standing, scarcity-escalated bids
        # so producers see the demand, the maintenance analogue of the
        # standing fuel rescue bid. Without them a ship needing repairs where
        # no supplies are for sale is deadlocked forever, unable to trade its
        # way out. Bid on every completable tier money allows, preferring
        # tiers whose commodity has traded somewhere: a bid on a good nobody
        # in the galaxy produces, such as ship_components early on, can rest
        # unfilled indefinitely while a fillable tier would have freed the
        # ship. Re-posted each turn; prices grow with scarcity pressure while
        # unfilled.
        candidates: list[tuple[int, int, CommodityDefinition, int, int]] = []
        for commodity_id, qty_needed in tiers:
            commodity = registry.get_commodity(commodity_id)
            if commodity is None:
                continue
            shortfall = qty_needed - self._maintenance_stock(commodity)
            if shortfall <= 0:
                continue
            price = max(
                1,
                math.ceil(
                    max(10, market.get_avg_price(commodity))
                    * (1.0 + market.scarcity_pressure_for(commodity))
                ),
            )
            produced_somewhere = any(
                planet.market.has_price_signal(commodity)
                for planet in self.simulation.planets
            )
            candidates.append(
                (
                    0 if produced_somewhere else 1,
                    price * shortfall,
                    commodity,
                    shortfall,
                    price,
                )
            )
        placed: list[str] = []
        for _, _, commodity, shortfall, price in sorted(
            candidates, key=lambda c: (c[0], c[1])
        ):
            if self.money // price < shortfall:
                continue  # can only repair if the whole shortfall is fillable
            order_id = market.place_buy_order(self, commodity, shortfall, price)
            if order_id:
                self.active_orders[order_id] = f"buy {commodity.id} (maintenance bid)"
                placed.append(f"{shortfall} {commodity.id} at {price}")
        if placed:
            self.last_action = "Standing maintenance bid(s): " + ", ".join(placed)

    def start_journey(self, destination: Planet, resuming: bool = False) -> bool:
        """Begin a journey to ``destination``.

        ``resuming`` marks the second half of a journey broken by an en-route
        refuel stop. It skips the maintenance roll, which the original
        departure already made, and clears the stop. The departure is still
        recorded in ``departure_turns``.

        Returns True if the journey started, False otherwise.
        """
        if self.status != ShipStatus.DOCKED:
            self.last_action = (
                f"Cannot start journey - ship status: {self.status.value}"
            )
            return False

        if self.planet is None:
            self.last_action = "Cannot start journey - ship is not at a planet"
            return False

        if self.planet == destination:
            self.last_action = "Already at destination"
            return False

        # Make sure the origin planet lists this ship.
        if self.planet and self not in self.planet.ships:
            self.planet.ships.append(self)

        if not resuming and self.check_maintenance():
            self.status = ShipStatus.NEEDS_MAINTENANCE
            self.last_action = "Maintenance required before departure"
            return False

        # Cancel resting orders before departing. Cancellation is
        # local-market-only, so a ship that leaves with orders in the book
        # can never reclaim their reserved money or cargo unless it returns;
        # otherwise ships strand in needs_maintenance because the fuel they
        # need is reserved by a stale sell order elsewhere. This runs before
        # the fuel check so fuel held by such an order counts as available.
        origin_market = self.planet.market
        resting = origin_market.get_actor_orders(self)
        for order in resting["buy"] + resting["sell"]:
            origin_market.cancel_order(order.order_id)

        fuel_commodity = self.simulation.commodity_registry.get_commodity("nova_fuel")

        if not fuel_commodity:
            self.last_action = "Cannot start journey - fuel commodity not defined"
            return False

        # The whole route is fuelled up front; intermediate planets are flown
        # past, not visited.
        navigator = get_navigator(self.simulation)
        route = navigator.route(self.planet, destination)
        distance = navigator.distance(self.planet, destination)
        adjusted_fuel_needed = self.fuel_required(distance)

        if self.fuel < adjusted_fuel_needed:
            self.last_action = (
                "Insufficient fuel for journey "
                f"(need {adjusted_fuel_needed / FUEL_MILLI:.3f})"
            )
            return False

        self.fuel -= adjusted_fuel_needed

        # One turn per DISTANCE_PER_TURN of route, minimum 1.
        self.travel_time = max(1, math.ceil(distance / DISTANCE_PER_TURN))
        self.travel_progress = 0.0
        self.status = ShipStatus.TRAVELING
        self.destination = destination
        self.route = route
        self.route_fuel_charged = adjusted_fuel_needed
        self.route_cumulative_distance = _cumulative_lane_distances(navigator, route)
        self.clear_stop()
        self.last_departure_turn = self.simulation.current_turn
        self.departure_turns.append(self.simulation.current_turn)

        # Load here, once the departure is certain: a payload must never be
        # taken aboard for a journey that then fails a check above. A payload
        # bound for any planet the route passes loads, since the ship gets
        # there; contracts for anywhere else go back on their boards.
        self.docked_turns_with_undelivered_contracts = 0
        served = set(route)
        served.discard(self.planet)
        for contract in list(self.contracts):
            if contract.status is not ContractStatus.ACCEPTED:
                continue
            if contract.destination in served:
                load_contract(self, contract)
            else:
                contract.origin.contracts.release(contract)

        hops = len(route) - 1
        self.last_action = (
            f"Departed for {destination.name} "
            f"({self.travel_time} turns, {hops} lane{'s' if hops != 1 else ''})"
        )
        return True

    def _route_nodes_passed(self, previous_progress: float) -> List[Tuple[int, float]]:
        """Intermediate route nodes whose distance falls in this turn's span.

        Index into ``route`` and distance along it, in flight order. The
        endpoints are excluded: the origin is behind the ship and the
        destination is an arrival, not a pass. Both the en-route deliveries
        and the refuel stop work off this one span test.
        """
        route = self.route
        cumulative = self.route_cumulative_distance
        if len(route) < 3 or len(cumulative) != len(route):
            return []
        total = cumulative[-1]
        covered = self.travel_progress * total
        previously_covered = previous_progress * total
        return [
            (index, cumulative[index])
            for index in range(1, len(route) - 1)
            if previously_covered < cumulative[index] <= covered
        ]

    def _deliver_passed_contracts(self, previous_progress: float) -> None:
        """Drop off every loaded contract bound for a node passed this turn.

        The ship flies past intermediate planets without docking, and a
        payload bound for one gets off as it passes: a passenger disembarks,
        a consignment is handed down. Nothing about the journey changes, so a
        rider to a planet on the way costs the trip only its hold space.
        """
        for index, _reached in self._route_nodes_passed(previous_progress):
            node = self.route[index]
            for contract in list(self.contracts):
                if (
                    contract.status is ContractStatus.LOADED
                    and contract.destination is node
                ):
                    deliver_contract(self, contract)

    def _take_route_stop(self, previous_progress: float) -> bool:
        """Dock at an intermediate planet passed this turn if the brain wants to.

        A node counts as passed when its distance along the route falls in the
        span covered this turn. The first such node the brain accepts becomes
        a stop: the ship docks there, the tank is credited with the route fuel
        it has not burned, and ``stop_resume`` holds the destination to fly on
        to.

        There are two reasons to stop and one stop may serve both: cheap fuel
        (:meth:`ShipBrain.wants_refuel_stop`) and contracts on the local board
        bound farther along the route
        (:meth:`ShipBrain.wants_pickup_stop`). The accounting is the same
        either way, so both are asked about the same node with the same
        numbers and ``stop_for_fuel`` / ``stop_for_pickup`` record the answers.

        Route fuel is charged with a ceiling per leg, so splitting a route in
        two costs fuel: the shortfall. Up to REFUEL_STOP_MAX_SHORTFALL
        millifuel of it are passed to the brain, which stops only where it can buy them
        back at the local ask; more than that is refused here.

        Returns True when the ship stopped.
        """
        destination = self.destination
        route = self.route
        cumulative = self.route_cumulative_distance
        if destination is None or len(route) < 3 or len(cumulative) != len(route):
            return False

        total = cumulative[-1]

        for index, reached in self._route_nodes_passed(previous_progress):
            node = route[index]
            if node is destination or node is self.planet:
                continue
            refund = self.route_fuel_charged - self.fuel_required(reached)
            shortfall = max(0, self.fuel_required(total - reached) - refund)
            if shortfall > REFUEL_STOP_MAX_SHORTFALL:
                continue
            # The docked turn plus whatever splitting the route adds to the
            # travel time, which is charged per leg at DISTANCE_PER_TURN.
            stop_turns = (
                1
                + math.ceil(reached / DISTANCE_PER_TURN)
                + math.ceil((total - reached) / DISTANCE_PER_TURN)
                - math.ceil(total / DISTANCE_PER_TURN)
            )
            for_fuel = self.brain.wants_refuel_stop(
                node, destination, refund, shortfall, stop_turns
            )
            for_pickup = self.brain.wants_pickup_stop(
                node, destination, refund, shortfall, stop_turns
            )
            if not (for_fuel or for_pickup):
                continue

            old_planet = self.planet
            self.fuel += refund
            self.planet = node
            self.destination = None
            self.route = []
            self.route_cumulative_distance = []
            self.status = ShipStatus.DOCKED
            self.travel_progress = 0.0
            if old_planet and self in old_planet.ships:
                old_planet.ships.remove(self)
            if self not in node.ships:
                node.ships.append(self)
            self.stop_resume = destination
            self.stop_turn = self.simulation.current_turn
            self.stop_for_fuel = for_fuel
            self.stop_for_pickup = for_pickup
            if for_fuel:
                self.refuel_stop_turns.append(self.simulation.current_turn)
            if for_pickup:
                self.pickup_stop_turns.append(self.simulation.current_turn)
            reasons = []
            if for_fuel:
                reasons.append("cheap fuel")
            if for_pickup:
                reasons.append("contracts")
            self.last_action = (
                f"Stopped at {node.name} for {' and '.join(reasons)}, "
                f"resuming to {destination.name}"
            )
            return True
        return False

    def update_journey(self) -> bool:
        """Advance the journey by one turn.

        Returns True on arrival, False otherwise.
        """
        if self.status != ShipStatus.TRAVELING:
            return False

        if not self.destination:
            self.status = ShipStatus.DOCKED
            return False

        progress_increment = 1.0 / self.travel_time
        previous_progress = self.travel_progress
        self.travel_progress += progress_increment

        # Payloads bound for a planet the route passes get off here, before a
        # refuel stop can end the turn's flight: the ship has passed the node
        # either way, and a stop at that node must not leave its own
        # deliveries aboard.
        self._deliver_passed_contracts(previous_progress)

        if self.travel_progress < 1.0 and self._take_route_stop(previous_progress):
            return False

        if self.travel_progress >= 1.0:
            old_planet = self.planet
            self.planet = self.destination
            self.destination = None
            self.route = []
            self.status = ShipStatus.DOCKED
            self.travel_progress = 0

            # Move the ship between the planets' ship lists.
            if old_planet and self in old_planet.ships:
                old_planet.ships.remove(self)

            if self.planet:
                if self not in self.planet.ships:
                    self.planet.ships.append(self)

            arrival = self.planet
            for contract in list(self.contracts):
                if (
                    contract.status is ContractStatus.LOADED
                    and contract.destination is arrival
                ):
                    deliver_contract(self, contract)

            self.last_action = f"Arrived at {arrival.name}"
            return True
        else:
            remaining_turns = math.ceil((1.0 - self.travel_progress) * self.travel_time)
            self.last_action = f"En route to {self.destination.name} ({remaining_turns} turns remaining)"
            return False

    def _age_undelivered_contracts(self) -> None:
        """Count a docked turn against loaded contracts, and strand the stale.

        A loaded contract pins the ship to its destination, so sitting
        anywhere else means the departure gate keeps refusing. Past
        CONTRACT_STRAND_PATIENCE the payload is better off put down here than
        carried around indefinitely; a passenger stranding also needs a free
        land here, so it can fail and be retried at the next planet.
        """
        planet = self.planet
        if planet is None:
            return
        undelivered = [
            contract
            for contract in self.contracts
            if contract.status is ContractStatus.LOADED
            and contract.destination is not planet
        ]
        if not undelivered:
            self.docked_turns_with_undelivered_contracts = 0
            return
        self.docked_turns_with_undelivered_contracts += 1
        if self.docked_turns_with_undelivered_contracts < CONTRACT_STRAND_PATIENCE:
            return
        for contract in undelivered:
            strand_contract(self, contract, planet)

    def take_turn(self) -> None:
        """Perform actions for this turn.

        Each turn consists of:
        1. If traveling, update journey
        2. If docked, take trade actions
        3. If docked, consider traveling
        """
        if self.status == ShipStatus.TRAVELING:
            self.update_journey()
            if self.stop_resume is not None:
                # The journey broke for an en-route stop this turn. Act now, in
                # the same turn, so a fuel bid rests in this turn's auction and
                # the contracts the stop was made for are taken off the board
                # before another ship can take them; the resume decision waits
                # until the fill has been pumped.
                self.brain.decide_trade_actions()
            return
        # Last turn's fuel fills landed in the hold; tank them before any
        # decision reads the fuel level.
        self.pump_fuel(keep_in_hold=self.brain.fuel_cargo_to_keep())
        # Give up on a stop that is going nowhere, or on one whose resume
        # destination is where the ship already sits, and let normal docked
        # logic take over. Contracts accepted for a pickup stop go back on the
        # board with it: the release at the top of the next docked turn is
        # reached only once the stop mode is off.
        if self.stop_resume is not None and (
            self.stop_resume is self.planet
            or self.simulation.current_turn - self.stop_turn >= REFUEL_STOP_MAX_TURNS
        ):
            self.clear_stop()
        if self.status == ShipStatus.NEEDS_MAINTENANCE:
            # Buy supplies locally when repair fails so the ship can repair
            # next turn instead of stranding. Repairing is the whole turn, so
            # any pending stop is abandoned.
            self.clear_stop()
            if not self.perform_maintenance():
                self._buy_maintenance_supplies()
        elif self.status == ShipStatus.DOCKED:
            self._age_undelivered_contracts()
            self.brain.decide_trade_actions()

            destination = self.brain.decide_travel()
            if destination:
                resuming = destination is self.stop_resume
                self.start_journey(destination, resuming=resuming)


def _cumulative_lane_distances(
    navigator: Navigator, route: Sequence[Planet]
) -> List[float]:
    """Lane distance from the first planet of ``route`` to each planet in it.

    The first entry is 0.0 and the last is the whole route's length. Summing
    the individual lanes rather than asking for the origin-to-node distance
    keeps the list consistent with the route actually being flown.
    """
    cumulative = [0.0]
    for previous, planet in zip(route, route[1:]):
        cumulative.append(cumulative[-1] + navigator.distance(previous, planet))
    return cumulative


def fuel_needed_for(distance: float, fuel_efficiency: float) -> int:
    """Millifuel a ship of ``fuel_efficiency`` burns over ``distance``.

    One ceiling, to the thousandth of a market unit. Every planning and
    consumption site routes through this so they never disagree.
    """
    return math.ceil(distance * FUEL_MILLI / (DISTANCE_PER_FUEL_UNIT * fuel_efficiency))


def units_to_milli(units: int) -> int:
    """Market units of fuel as tank millifuel."""
    return units * FUEL_MILLI


def milli_to_units_ceil(milli: int) -> int:
    """Whole market units that cover ``milli`` millifuel; never negative."""
    return max(0, math.ceil(milli / FUEL_MILLI))


def milli_to_units_floor(milli: int) -> int:
    """Whole market units fully contained in ``milli`` millifuel; never negative."""
    return max(0, milli // FUEL_MILLI)


def mean_round_trip_fuel(mean_pair_distance: float, fuel_efficiency: float) -> int:
    """Millifuel a ship of ``fuel_efficiency`` burns on an average round trip.

    ``mean_pair_distance`` is the mean shortest lane route between planets,
    from :meth:`Navigator.mean_pair_distance`.
    """
    return 2 * fuel_needed_for(mean_pair_distance, fuel_efficiency)


def starting_capital(mean_pair_distance: float, fuel_efficiency: float) -> int:
    """Starting money for a ship launched into a galaxy of this size.

    Sized as SHIP_CAPITAL_ROUND_TRIPS average round trips of fuel plus the
    maintenance those trips expect, valued at
    SHIP_CAPITAL_FUEL_PRICE_REFERENCE, marked up by a working-capital
    fraction for cargo, and floored at the small-galaxy baseline.
    """
    fuel_units = (
        mean_round_trip_fuel(mean_pair_distance, fuel_efficiency) / FUEL_MILLI
    ) + (2 * MAINTENANCE_CHANCE * MAINTENANCE_FUEL_UNITS)
    budget = (
        SHIP_CAPITAL_ROUND_TRIPS
        * fuel_units
        * SHIP_CAPITAL_FUEL_PRICE_REFERENCE
        * (1.0 + SHIP_CAPITAL_RESERVE_FRACTION)
    )
    return max(SHIP_CAPITAL_FLOOR, int(budget))


def fuel_capacity_for(mean_pair_distance: float, fuel_efficiency: float) -> int:
    """Tank size in millifuel for a galaxy of this size, never below the baseline."""
    return max(
        BASE_FUEL_CAPACITY,
        math.ceil(
            FUEL_CAPACITY_ROUND_TRIP_HEADROOM
            * mean_round_trip_fuel(mean_pair_distance, fuel_efficiency)
        ),
    )
