from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Literal, Optional

from spacesim2.core.actor import Actor
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.brains import dealer
from spacesim2.core.commands import (
    CancelOrderCommand,
    EconomicCommand,
    GovernmentWorkCommand,
    MarketCommand,
    PlaceBuyOrderCommand,
    PlaceSellOrderCommand,
)
from spacesim2.core.commodity import CommodityDefinition

if TYPE_CHECKING:
    from spacesim2.core.market import Market

# ---- Maker internal state ----------------------------------------------------

Phase = Literal["DISCOVERY", "MAKER"]


@dataclass
class MarketMakerState:
    """Per-commodity state for the market maker.

    Discovery tracks a conservative price bracket [lower_bound, upper_bound].
    A filled bid means sellers exist at or below its price, which tightens
    upper_bound. A filled ask means buyers exist at or above its price, which
    raises lower_bound. Once the bracket is tight enough or enough trades are
    seen, the midpoint is recorded and the phase switches to MAKER.
    """

    phase: Phase = "DISCOVERY"

    # Conservative price bracket from observed fills
    lower_bound: int = 1  # buyers exist at ≥ lower_bound
    upper_bound: Optional[int] = None  # sellers exist at ≤ upper_bound

    # Last discovery probes placed, 1 unit each
    last_bid_quote: Optional[int] = None
    last_ask_quote: Optional[int] = None

    # Last fill price on each side; used for phase transitions
    last_bid_filled_price: Optional[int] = None
    last_ask_filled_price: Optional[int] = None

    trades_seen: int = 0
    quiet_ticks: int = 0
    last_sigma: Optional[float] = None

    # Midpoint found in discovery; seeds maker mode
    discovered_midpoint: Optional[int] = None


# ---- Market maker brain ------------------------------------------------------


class MarketMakerBrain(ActorBrain):
    """Liquidity-first market maker.

    Discovery mode posts 1-unit geometric probes, with no price defaults, to
    establish a bracket. Maker mode quotes a ladder around mid plus or minus
    spread with light inventory skew. Fills are read from
    market.get_actor_transaction_history(actor) through a cursor. Goals:
    simplicity, solvency, and stable liquidity.
    """

    # Tunables
    LADDER_LEVELS: int = 5  # depth per side in MAKER
    # Fraction of the wallet committed to the buy side in aggregate. The pool
    # is split evenly across every market served (see decide_market_actions),
    # so illiquid commodities get a real, bounded allocation instead of being
    # starved by whichever markets are iterated first.
    BUY_CAPITAL_FRACTION: float = dealer.DEFAULT_BUY_CAPITAL_FRACTION
    VOLATILITY_WIDENER: float = 0.5  # widen spread by (1 + VOL * sigma/mid)
    INVENTORY_SKEW_CAP: float = 0.50  # cap skew at ±50% of mid
    MAX_NOTIONAL_FRACTION: float = 0.60  # cap exposure vs. cash-only net worth
    MIN_ORDER_QUANTITY: int = 1

    # Discovery behavior
    # Highest price probed while seeding a market with no trade history. Keeps
    # the maker from bidding illiquid goods to arbitrary highs; the first fill
    # then tightens the bracket.
    DISCOVERY_PRICE_CEILING: int = 15
    DISCOVERY_TIGHTNESS_EPSILON: int = 2  # switch to maker when U-L <= epsilon
    DISCOVERY_MIN_TRADES: int = 6  # or after N total fills (any side)
    REVERT_IF_QUIET_TICKS: int = 50  # revert to discovery if no fills for N ticks
    VOLATILITY_SPIKE_FACTOR: float = 3.0  # revert if sigma jumps ≥ factor×

    # Safety floors
    MIN_PRICE: int = 1
    MIN_ASK_PRICE: int = 2

    def __init__(self) -> None:
        super().__init__()
        # Base spread percentage, randomized per maker.
        self.spread_percentage: float = dealer.draw_spread()

        # Per-commodity state, keyed by commodity name.
        self._state: Dict[str, MarketMakerState] = {}

        # Cursor into the actor's transaction history.
        self._fill_cursor: int = 0

    # -------- Required interface ---------------------------------------------

    def decide_economic_action(self, actor: Actor) -> Optional["EconomicCommand"]:
        """Market makers only do government work."""
        return GovernmentWorkCommand()

    def decide_market_actions(self, actor: Actor) -> List["MarketCommand"]:
        """Primary decision loop: cancel old orders, ingest new fills, then quote."""
        if not actor.planet:
            return []

        market = actor.planet.market
        commands: List["MarketCommand"] = []

        # Cancel and replace every turn. There are no fees and one maker.
        existing_orders = market.get_actor_orders(actor)
        for order in existing_orders["buy"] + existing_orders["sell"]:
            commands.append(CancelOrderCommand(order.order_id))

        new_fills_by_commodity = self._consume_new_transactions(actor, market)

        all_commodities = [
            c for c in actor.sim.commodity_registry.all_commodities() if c.transportable
        ]

        # Split the buy-side pool evenly across every market so each gets a
        # bounded share. The total requested never exceeds BUY_CAPITAL_FRACTION
        # of the wallet, so sequential order execution, which reserves cash per
        # order, cannot starve later markets.
        num_markets = max(1, len(all_commodities))
        per_market_budget = (actor.money * self.BUY_CAPITAL_FRACTION) / num_markets

        for commodity in all_commodities:
            state = self._ensure_state_for(commodity)

            self._apply_fills_to_state(actor, commodity, state, new_fills_by_commodity)

            if state.phase == "DISCOVERY":
                commands.extend(
                    self._discovery_quotes(actor, commodity, state, per_market_budget)
                )
            else:  # MAKER
                commands.extend(
                    self._maker_quotes(
                        actor, market, commodity, state, per_market_budget
                    )
                )

        return commands

    # -------- Transaction ingestion ------------------------------------------

    def _consume_new_transactions(
        self, actor: Actor, market: "Market"
    ) -> Dict[str, dealer.CommodityFills]:
        """Group the actor's new fills since the last tick by commodity and side."""
        self._fill_cursor, grouped = dealer.ingest_fills(
            actor, market, self._fill_cursor
        )
        return grouped

    def _apply_fills_to_state(
        self,
        actor: Actor,
        commodity: "CommodityDefinition",
        state: MarketMakerState,
        fills_by_commodity: Dict[str, dealer.CommodityFills],
    ) -> None:
        """Tighten discovery bracket and handle mode transitions or reversion."""
        commodity_name = dealer.commodity_key(commodity)
        fills = fills_by_commodity.get(commodity_name)

        if actor.planet is None:
            return
        market = actor.planet.market
        sigma: Optional[float] = None
        if market.has_history(commodity):
            sigma = max(market.get_30_day_standard_deviation(commodity), 1.0)
            state.phase = "MAKER"

        if state.phase == "DISCOVERY":
            if fills:
                # We bought: sellers exist at or below max(buy_fills).
                if fills.buy_prices:
                    max_bid_fill = max(fills.buy_prices)
                    state.upper_bound = (
                        max_bid_fill
                        if state.upper_bound is None
                        else min(state.upper_bound, max_bid_fill)
                    )
                    state.last_bid_filled_price = max_bid_fill
                    state.trades_seen += 1

                # We sold: buyers exist at or above min(sell_fills).
                if fills.sell_prices:
                    min_ask_fill = min(fills.sell_prices)
                    state.lower_bound = max(state.lower_bound, min_ask_fill)
                    state.last_ask_filled_price = min_ask_fill
                    state.trades_seen += 1

                # Switch to MAKER when the bracket is tight or enough trades seen.
                if (
                    state.last_bid_filled_price is not None
                    and state.last_ask_filled_price is not None
                ):
                    if (state.upper_bound is not None) and (
                        state.upper_bound - state.lower_bound
                        <= self.DISCOVERY_TIGHTNESS_EPSILON
                    ):
                        state.discovered_midpoint = (
                            state.lower_bound + state.upper_bound
                        ) // 2
                        state.phase = "MAKER"
                    elif state.trades_seen >= self.DISCOVERY_MIN_TRADES:
                        # No upper bound observed: synthesize one from the last
                        # bid quote.
                        synthetic_upper = (
                            state.upper_bound
                            if state.upper_bound is not None
                            else max(
                                state.lower_bound + 1,
                                (state.last_bid_quote or state.lower_bound + 1),
                            )
                        )
                        state.discovered_midpoint = (
                            state.lower_bound + synthetic_upper
                        ) // 2
                        state.phase = "MAKER"

            state.last_sigma = sigma  # for spike detection

        else:  # MAKER
            any_fill = bool(fills)
            state.quiet_ticks = 0 if any_fill else (state.quiet_ticks + 1)

            # Revert to discovery if quiet too long or volatility spikes.
            revert_to_discovery = False
            if state.quiet_ticks >= self.REVERT_IF_QUIET_TICKS:
                revert_to_discovery = True
            if (
                sigma is not None
                and state.last_sigma is not None
                and sigma >= self.VOLATILITY_SPIKE_FACTOR * state.last_sigma
            ):
                revert_to_discovery = True

            state.last_sigma = sigma

            if revert_to_discovery:
                state.phase = "DISCOVERY"
                # Keep the bracket; clear quotes so probing restarts.
                state.last_bid_quote = None
                state.last_ask_quote = None
                state.trades_seen = 0

    # -------- State helpers ---------------------------------------------------

    def _ensure_state_for(self, commodity: "CommodityDefinition") -> MarketMakerState:
        """Return the state for this commodity, creating it if needed."""
        commodity_name = dealer.commodity_key(commodity)
        if commodity_name not in self._state:
            self._state[commodity_name] = MarketMakerState(
                phase="DISCOVERY",
                lower_bound=self.MIN_PRICE,
                upper_bound=None,
                last_bid_quote=None,
                last_ask_quote=None,
                last_bid_filled_price=None,
                last_ask_filled_price=None,
                trades_seen=0,
                quiet_ticks=0,
                last_sigma=None,
                discovered_midpoint=None,
            )
        return self._state[commodity_name]

    # -------- Discovery mode --------------------------------------------------

    def _discovery_quotes(
        self,
        actor: Actor,
        commodity: "CommodityDefinition",
        state: MarketMakerState,
        per_market_budget: float,
    ) -> List["MarketCommand"]:
        """Post 1-unit geometric probes to establish a conservative bracket.

        The bid starts at lower_bound and doubles while unfilled, capped by
        upper_bound. The ask starts at upper_bound, or lower_bound+1, and
        halves while unfilled, floored at lower_bound+1. The posted bid is
        additionally held under our own ask, but the ladder itself is not:
        see the comment on ``last_bid_quote`` below.
        """
        commands: List["MarketCommand"] = []
        # The probe budget is this market's share of the buy pool. It must
        # afford one unit up to DISCOVERY_PRICE_CEILING, or the maker can never
        # bid high enough to attract the first seller and the market stays dead.
        per_tick_probe_budget = max(self.MIN_PRICE, int(per_market_budget))

        # Cap how high to probe so seeding an illiquid market cannot bid it to
        # arbitrary highs. The first real fill tightens the bracket.
        probe_ceiling = max(self.MIN_ASK_PRICE, self.DISCOVERY_PRICE_CEILING)

        # Initialize the upper bound to the affordable share of our ceiling.
        if state.upper_bound is None:
            state.upper_bound = max(
                self.MIN_ASK_PRICE, min(probe_ceiling, per_tick_probe_budget)
            )

        lower_bound = state.lower_bound
        upper_bound = state.upper_bound
        has_inventory = actor.inventory.get_quantity(commodity) > 0

        # Probe ask, 1 unit, only with inventory. Priced before the bid so
        # the bid can be held strictly below it. The two probes walk toward
        # each other -- the bid doubles up from lower_bound, the ask halves
        # down toward lower_bound + 1 -- so without the clamp they meet (at 2
        # from a lower_bound of 1) and the maker fills its own ask.
        next_ask: Optional[int] = None
        if has_inventory:
            next_ask = (
                state.last_ask_quote
                if state.last_ask_quote is not None
                else max(lower_bound + 1, upper_bound)
            )
            if state.last_ask_quote is not None:
                # Previous ask did not fill: halve.
                next_ask = max(
                    lower_bound + 1, min(upper_bound, state.last_ask_quote // 2)
                )
            state.last_ask_quote = next_ask
            commands.append(PlaceSellOrderCommand(commodity, 1, next_ask))

        # Probe bid, 1 unit, always strictly under our own ask.
        if per_tick_probe_budget >= max(self.MIN_PRICE, lower_bound):
            next_bid = (
                state.last_bid_quote
                if state.last_bid_quote is not None
                else lower_bound
            )
            if state.last_bid_quote is not None:
                # Previous bid did not fill: double.
                next_bid = min(upper_bound, max(lower_bound, state.last_bid_quote * 2))

            if next_bid >= self.MIN_PRICE:
                # Remember the ladder rung, not the price actually posted.
                # While the maker held inventory its own halving ask clamped
                # the bid, and persisting the clamped value pinned the ladder
                # at lower_bound: doubling lower_bound was clamped straight
                # back to it, every turn, so the bid could never escalate for
                # exactly the commodities the maker was trying to two-side.
                state.last_bid_quote = next_bid
                placed_bid = next_bid
                if next_ask is not None:
                    # Never cross our own ask: the maker would fill itself.
                    placed_bid = min(placed_bid, next_ask - 1)
                if placed_bid >= self.MIN_PRICE and per_tick_probe_budget >= placed_bid:
                    commands.append(PlaceBuyOrderCommand(commodity, 1, placed_bid))

        return commands

    # -------- Maker mode ------------------------------------------------------

    def _maker_quotes(
        self,
        actor: Actor,
        market: "Market",
        commodity: "CommodityDefinition",
        state: MarketMakerState,
        per_market_budget: float,
    ) -> List["MarketCommand"]:
        """Quote a ladder around a volatility-adjusted, inventory-skewed midpoint."""
        commands: List["MarketCommand"] = []

        # Midpoint blends the discovered midpoint with the rolling average.
        midpoint = (
            state.discovered_midpoint
            if state.discovered_midpoint is not None
            else self.MIN_PRICE
        )
        sigma = 1.0
        if market.has_history(commodity):
            average_price = market.get_30_day_average_price(commodity)
            sigma = max(market.get_30_day_standard_deviation(commodity), 1.0)
            if average_price is not None:
                midpoint = (
                    int(round(0.5 * midpoint + 0.5 * average_price))
                    if midpoint > 1
                    else int(round(average_price))
                )
        midpoint = max(self.MIN_PRICE, midpoint)

        # Spread, widened by volatility.
        half_spread = max(
            1,
            int(
                self.spread_percentage
                * midpoint
                * (1.0 + self.VOLATILITY_WIDENER * (sigma / max(1, midpoint)))
            ),
        )

        current_inventory = actor.inventory.get_quantity(commodity)
        target_inventory = dealer.flow_stock_target(market, commodity)
        quoted_midpoint = dealer.skew_midpoint(
            midpoint,
            current_inventory,
            target_inventory,
            self.INVENTORY_SKEW_CAP,
            self.MIN_PRICE,
        )

        # Evenly spaced ladders.
        levels = self.LADDER_LEVELS
        step = max(1, half_spread // levels)

        bid_prices = dealer.ladder_prices(
            quoted_midpoint, half_spread, levels, step, self.MIN_PRICE, ascending=False
        )
        ask_prices = dealer.ladder_prices(
            quoted_midpoint,
            half_spread,
            levels,
            step,
            self.MIN_ASK_PRICE,
            ascending=True,
        )

        # Exposure cap versus cash-only net worth.
        cash_net_worth = max(1, actor.money)
        max_notional_per_commodity = self.MAX_NOTIONAL_FRACTION * cash_net_worth

        # Sell: spread inventory across asks, front-loaded near the touch.
        if current_inventory > 0:
            remaining_inventory = current_inventory
            weights = dealer.front_loaded_weights(levels)
            weight_sum = sum(weights)

            for level_index, price in enumerate(ask_prices):
                if remaining_inventory <= 0:
                    break
                allocated_quantity = max(
                    self.MIN_ORDER_QUANTITY,
                    (remaining_inventory * weights[level_index]) // weight_sum,
                )
                allocated_quantity = min(allocated_quantity, remaining_inventory)
                if allocated_quantity > 0:
                    commands.append(
                        PlaceSellOrderCommand(commodity, allocated_quantity, price)
                    )
                    remaining_inventory -= allocated_quantity

        # Buy: allocate this market's share of the pool, front-loaded.
        cash_budget = int(per_market_budget)
        if cash_budget > 0 and max_notional_per_commodity > 0:
            remaining_funds = min(cash_budget, int(max_notional_per_commodity))
            weights = dealer.front_loaded_weights(levels)
            weight_sum = sum(weights)

            for level_index, price in enumerate(bid_prices):
                if remaining_funds < price:
                    break
                # Size scales with weight and quoted_midpoint/price.
                base_units = max(
                    self.MIN_ORDER_QUANTITY,
                    (weights[level_index] * quoted_midpoint)
                    // (weight_sum * max(1, price)),
                )
                quantity = min(base_units, remaining_funds // price)
                if quantity > 0:
                    commands.append(PlaceBuyOrderCommand(commodity, quantity, price))
                    remaining_funds -= quantity * price

        return commands
