import heapq
import itertools
import math
import random
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AbstractSet, Deque, Dict, List, Optional, Tuple, Union

from spacesim2.core.actor import Actor

if TYPE_CHECKING:
    from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
    from spacesim2.core.process import ProcessDefinition
    from spacesim2.core.ship import Ship

# Anything that can place orders and trade. Ships use the same duck-typed
# interface as actors: name, money, inventory, active_orders, reserved_money.
MarketParticipant = Union[Actor, "Ship"]


# Scarcity pressure is a per-commodity signal that rises each turn local buy
# demand goes unfilled and decays when it is met. Buyers raise bids toward
# their ceiling as it grows, which pulls ships toward starved planets.
# Update: p' = p*DECAY + STEP*unmet. Fully starved steady state is
# STEP/(1-DECAY), clamped to MAX.
SCARCITY_PRESSURE_STEP = 0.5
SCARCITY_PRESSURE_DECAY = 0.9
SCARCITY_PRESSURE_MAX = 3.0

# Price/volume history bounds. Readers use at most a 30-turn window
# (get_30_day_*), a 10-turn flow window (FLOW_RECENCY_TURNS in ship planning),
# or the last entry, so HISTORY_KEEP entries suffice. A series is trimmed only
# past HISTORY_TRIM_THRESHOLD, keeping appends amortized O(1).
HISTORY_KEEP = 120
HISTORY_TRIM_THRESHOLD = 240

# Order events kept per actor. Only the data logger reads them, through
# Actor.get_market_activity_this_turn, and it needs only the current turn.
# The bound stops growth over long runs.
ORDER_EVENTS_PER_ACTOR = 100

# Transaction retention: last 1000 market-wide, last 100 per actor name.
TRANSACTIONS_KEEP_GLOBAL = 1000
TRANSACTIONS_KEEP_PER_ACTOR = 100


# Monotonic order-id source, module-level so ids are unique across every
# market in the process. Locked because the threaded actor phase
# (core/parallel.py) places orders from several threads; itertools.count
# alone is only atomic under the GIL.
class _LockedCounter:
    def __init__(self, start: int) -> None:
        self._it = itertools.count(start)
        self._lock = threading.Lock()

    def __next__(self) -> int:
        with self._lock:
            return next(self._it)


_ORDER_ID_COUNTER = _LockedCounter(1)

# Monotonic transaction-id source, shared across every market in the process
# for the same reason and with the same locking as the order-id counter. Ids
# are strictly increasing in creation order, which is what lets a reader hold
# a cursor into a per-actor history that gets trimmed underneath it.
_TRANSACTION_ID_COUNTER = _LockedCounter(1)


@dataclass
class Order:
    """Represents a buy or sell order in the market."""

    actor: MarketParticipant
    commodity_type: "CommodityDefinition"
    quantity: int
    price: int
    is_buy: bool
    timestamp: int = 0  # Tiebreak among equal prices; oldest first
    order_id: str = ""
    created_turn: int = 0
    # Lazy-delete marker: cancel_order marks the order instead of rebuilding
    # the per-commodity book. Every book reader must skip cancelled orders.
    # Compaction, threshold-based in cancel_order and per commodity in each
    # match_orders pass, bounds how long dead orders rest in the books.
    cancelled: bool = False

    def __post_init__(self) -> None:
        """Generate a unique order ID if not provided."""
        if not self.order_id:
            self.order_id = str(next(_ORDER_ID_COUNTER))


def _order_price(order: "Order") -> int:
    """Sort key for order price; a module function beats a lambda on hot paths."""
    return order.price


@dataclass
class OrderEvent:
    """Represents an order lifecycle event."""

    order_id: str
    actor_name: str
    event_type: str  # "created", "filled", "cancelled"
    turn: int
    order: Order


@dataclass
class Transaction:
    """Represents a completed transaction in the market."""

    buyer: MarketParticipant
    seller: MarketParticipant
    commodity_type: "CommodityDefinition"
    quantity: int
    price: int
    total_amount: int
    turn: int = 0
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    # Strictly increasing across the process, assigned at creation. Readers
    # that follow their own fills (see core/brains/dealer.ingest_fills) key
    # their cursor on this rather than on a list index, because
    # _trim_transaction_history drops the front of a per-actor history and an
    # index cursor would then either skip fills or replay them.
    transaction_id: int = field(default_factory=lambda: next(_TRANSACTION_ID_COUNTER))


class Market:
    """Represents a commodity market on a planet."""

    def __init__(self) -> None:
        self.buy_orders: Dict["CommodityDefinition", List[Order]] = defaultdict(list)
        self.sell_orders: Dict["CommodityDefinition", List[Order]] = defaultdict(list)

        # Open orders only; filled and cancelled orders are removed.
        self.orders_by_id: Dict[str, Order] = {}

        self.actor_orders: Dict[MarketParticipant, Dict[str, List[str]]] = defaultdict(
            lambda: {"buy": [], "sell": []}
        )

        self.transaction_history: List[Transaction] = []
        self.actor_transaction_history: Dict[str, List[Transaction]] = defaultdict(list)

        # Recent order lifecycle events per actor. Bounded because each event
        # pins its Order. The only reader is get_actor_order_events for the
        # current turn, which ORDER_EVENTS_PER_ACTOR covers.
        self.order_events_by_actor: Dict[str, Deque[OrderEvent]] = defaultdict(
            lambda: deque(maxlen=ORDER_EVENTS_PER_ACTOR)
        )

        # Actor names whose order events are recorded. None means everyone,
        # the default for directly-constructed markets such as in tests. The
        # simulation wires this to the data logger's live logged-actor set:
        # events are only read back for logged actors, so recording them for
        # everyone else is overhead on every place, cancel, and fill.
        self.order_event_filter: Optional[AbstractSet[str]] = None

        self.current_turn = 0

        # Last 10 fill prices per commodity.
        self.last_traded_prices: Dict["CommodityDefinition", List[int]] = defaultdict(
            list
        )

        # Per-turn series, one entry per turn once a commodity has traded.
        self.price_history: Dict["CommodityDefinition", List[int]] = defaultdict(
            list
        )  # Volume-weighted average fill price per turn
        self.volume_history: Dict["CommodityDefinition", List[int]] = defaultdict(
            list
        )  # Units traded per turn

        # Lifetime count of turns with nonzero volume per commodity. Backs
        # has_history() in O(1); the series above are trimmed to a recent
        # window, so this counter is the durable record.
        self._active_volume_days: Dict["CommodityDefinition", int] = defaultdict(int)

        # Per-turn memos for trade-history reads: get_avg_price,
        # has_price_signal, get_30_day_average_price,
        # get_30_day_average_volume, get_30_day_standard_deviation. Their
        # inputs (last_traded_prices, price_history, volume_history) are
        # written only inside match_orders at the end of the turn, so cached
        # values are exact from the start of a turn until matching. Cleared
        # in set_current_turn and again at the top of match_orders so
        # post-match readers (export, logging) see fresh values.
        self._avg_price_cache: Dict["CommodityDefinition", int] = {}
        self._price_signal_cache: Dict["CommodityDefinition", bool] = {}
        self._avg30_price_cache: Dict["CommodityDefinition", float] = {}
        self._avg30_volume_cache: Dict["CommodityDefinition", float] = {}
        self._stdev30_cache: Dict["CommodityDefinition", float] = {}

        # Per-commodity scarcity pressure (see SCARCITY_PRESSURE_* constants).
        self.scarcity_pressure: Dict["CommodityDefinition", float] = defaultdict(float)

        # Per-turn cache of drive-bid anchors for never-traded goods, keyed
        # commodity_id -> (turn, anchor). Written and read by
        # ActorBrain._drive_bid_reference. Imputing replacement cost recurses
        # over the process graph, too costly to redo per actor per turn.
        self.drive_anchor_cache: Dict[str, Tuple[int, float]] = {}

        # (highest_bid, lowest_ask) per commodity for get_bid_ask_spread, the
        # hottest market read. Maintained incrementally where cheap and exact:
        # placing an order can only improve the cached best, an O(1) max/min
        # update; cancelling or filling invalidates only when the removed
        # order sat at the cached best, and the entry is then recomputed by
        # one O(book) rescan on the next read. Mutation-tracked, not
        # turn-scoped: orders placed mid-turn are visible to later actors
        # because matching is deferred to end of turn, so a per-turn cache
        # would change behavior. A present entry always equals the true best.
        self._quote_cache: Dict[
            "CommodityDefinition", Tuple[Optional[int], Optional[int]]
        ] = {}

        # Bumped at every site where a best bid/ask could change, mirroring
        # the _quote_cache maintenance above: a placement that improves or
        # could first populate a side's best, a cancel at the cached best or
        # with no cached quote, and matching. A bump when the best did not
        # move only loses cache sharing; a best-quote change without a bump
        # is a bug. Consumers key shared quote-derived tables on (sim turn,
        # quote_version): two readers at the same key see identical bid/ask
        # for every commodity, and trade-history reads such as avg_price are
        # constant between matchings, so such tables are identical and safe
        # to share across actors on this market.
        self.quote_version: int = 0

        # Shared actor-independent process quote table, stored as
        # ((sim turn, quote_version), table). Written and read by
        # ColonistBrain._best_process_and_raw_profit. The list is shared
        # across actors and must be treated as read-only. The turn is part of
        # the key so rolling windows advancing across turns cannot leave it
        # stale even if quote_version stands still. No lock: threading shards
        # whole planets, so one thread owns a market.
        self.shared_quote_table: Optional[
            Tuple[Tuple[int, int], List[Tuple[float, float, "ProcessDefinition"]]]
        ] = None

        # Cancelled (dead) orders still resting in each per-commodity book.
        # cancel_order marks orders dead instead of rebuilding the list. A
        # book is compacted when its dead count exceeds half its length, and
        # per commodity in match_orders so staleness never crosses a turn.
        # Compaction keeps live orders in relative order, so it needs no
        # cache invalidation.
        self._dead_buy_counts: Dict["CommodityDefinition", int] = defaultdict(int)
        self._dead_sell_counts: Dict["CommodityDefinition", int] = defaultdict(int)

        # Sorted (price, quantity) bid levels per commodity, dropped on any
        # buy-book mutation. Backs get_bid_levels, which ship planners call
        # repeatedly between mutations.
        self._bid_levels_cache: Dict["CommodityDefinition", List[Tuple[int, int]]] = {}

        # Set by the simulation.
        self.commodity_registry: Optional["CommodityRegistry"] = None

    def _trim_transaction_history(self) -> None:
        """Trim transaction histories in place to TRANSACTIONS_KEEP_*.

        ``del list[:-keep]`` rather than slice-and-copy: an untrimmed list
        costs a length check and a trimmed one a single memmove.
        """
        if len(self.transaction_history) > TRANSACTIONS_KEEP_GLOBAL:
            del self.transaction_history[:-TRANSACTIONS_KEEP_GLOBAL]

        for actor_transactions in self.actor_transaction_history.values():
            if len(actor_transactions) > TRANSACTIONS_KEEP_PER_ACTOR:
                del actor_transactions[:-TRANSACTIONS_KEEP_PER_ACTOR]

    def get_actor_transaction_history(
        self, actor: MarketParticipant
    ) -> List[Transaction]:
        """Get the transaction history for a specific actor."""
        return self.actor_transaction_history.get(actor.name, [])

    def _record_order_event(self, event_type: str, order: Order) -> None:
        """Record an order lifecycle event for the order's actor.

        Skipped for actors outside ``order_event_filter``; nothing reads
        their events. See the filter's comment in ``__init__``.
        """
        recorded = self.order_event_filter
        if recorded is not None and order.actor.name not in recorded:
            return
        event = OrderEvent(
            order_id=order.order_id,
            actor_name=order.actor.name,
            event_type=event_type,
            turn=self.current_turn,
            order=order,
        )
        self.order_events_by_actor[order.actor.name].append(event)

    def get_actor_current_orders(self, actor: MarketParticipant) -> Dict[str, Dict]:
        """Open orders for an actor, keyed by order id."""
        result = {}

        for order_id in self.actor_orders[actor]["buy"]:
            if order_id in self.orders_by_id:
                order = self.orders_by_id[order_id]
                result[order_id] = {
                    "type": "buy",
                    "commodity": order.commodity_type.id,
                    "quantity": order.quantity,
                    "price": order.price,
                    "created_turn": order.created_turn,
                }

        for order_id in self.actor_orders[actor]["sell"]:
            if order_id in self.orders_by_id:
                order = self.orders_by_id[order_id]
                result[order_id] = {
                    "type": "sell",
                    "commodity": order.commodity_type.id,
                    "quantity": order.quantity,
                    "price": order.price,
                    "created_turn": order.created_turn,
                }

        return result

    def get_actor_order_events(
        self,
        actor: MarketParticipant,
        since_turn: int = 0,
        until_turn: Optional[int] = None,
    ) -> List[OrderEvent]:
        """An actor's order events in [since_turn, until_turn], oldest first.

        Only the last ORDER_EVENTS_PER_ACTOR events per actor are kept, so a
        query far into the past may be truncated. The current-turn window the
        data logger uses is always complete.
        """
        actor_events = self.order_events_by_actor[actor.name]

        if until_turn is None:
            until_turn = self.current_turn

        result = []
        # Events are chronological, so scan from the newest and stop early.
        for event in reversed(actor_events):
            if event.turn < since_turn:
                break
            if event.turn <= until_turn:
                result.append(event)

        return list(reversed(result))

    def get_actor_transactions_range(
        self,
        actor: MarketParticipant,
        since_turn: int = 0,
        until_turn: Optional[int] = None,
    ) -> List[Transaction]:
        """An actor's transactions in [since_turn, until_turn], oldest first."""
        actor_transactions = self.actor_transaction_history[actor.name]

        if until_turn is None:
            until_turn = self.current_turn

        result = []
        # Transactions are chronological, so scan from the newest and stop early.
        for transaction in reversed(actor_transactions):
            if transaction.turn < since_turn:
                break
            if transaction.turn <= until_turn:
                result.append(transaction)

        return list(reversed(result))

    def place_buy_order(
        self,
        actor: MarketParticipant,
        commodity_type: "CommodityDefinition",
        quantity: int,
        price: int,
    ) -> str:
        """Place a bid, reserving the buyer's money.

        Quantity is clamped to what the actor can afford. Returns the order
        id, or an empty string if nothing could be placed.
        """
        total_cost = quantity * price
        if actor.money < total_cost:
            # Recompute the cost for the clamped quantity. Reserving the
            # original cost would drive money negative and orphan the
            # difference in reserved_money forever.
            quantity = int(actor.money / price) if price > 0 else 0
            total_cost = quantity * price

        if quantity <= 0:
            return ""

        actor.money -= total_cost
        actor.reserved_money += total_cost

        order = Order(
            actor=actor,
            commodity_type=commodity_type,
            quantity=quantity,
            price=price,
            is_buy=True,
            timestamp=self.current_turn,
            created_turn=self.current_turn,
        )

        self.buy_orders[commodity_type].append(order)
        self._on_buy_book_changed(commodity_type)
        cached = self._quote_cache.get(commodity_type)
        if cached is not None:
            best_bid, best_ask = cached
            if best_bid is None or price > best_bid:
                self._quote_cache[commodity_type] = (price, best_ask)
                self.quote_version += 1
        else:
            # No cached quote, so whether the bid is the new best is unknown.
            # Conservative bump; see __init__.
            self.quote_version += 1
        self.orders_by_id[order.order_id] = order
        self.actor_orders[actor]["buy"].append(order.order_id)

        actor.active_orders[order.order_id] = f"buy {commodity_type.id}"

        self._record_order_event("created", order)

        return order.order_id

    def place_sell_order(
        self,
        actor: MarketParticipant,
        commodity_type: "CommodityDefinition",
        quantity: int,
        price: int,
    ) -> str:
        """Place an ask, reserving the seller's goods.

        Quantity is clamped to the available inventory. Returns the order id,
        or an empty string if nothing could be placed.
        """
        available_quantity = actor.inventory.get_available_quantity(commodity_type)
        if available_quantity < quantity:
            quantity = available_quantity

        if quantity <= 0:
            return ""

        actor.inventory.reserve_commodity(commodity_type, quantity)

        order = Order(
            actor=actor,
            commodity_type=commodity_type,
            quantity=quantity,
            price=price,
            is_buy=False,
            timestamp=self.current_turn,
            created_turn=self.current_turn,
        )

        self.sell_orders[commodity_type].append(order)
        cached = self._quote_cache.get(commodity_type)
        if cached is not None:
            best_bid, best_ask = cached
            if best_ask is None or price < best_ask:
                self._quote_cache[commodity_type] = (best_bid, price)
                self.quote_version += 1
        else:
            # No cached quote. Conservative bump; see __init__.
            self.quote_version += 1
        self.orders_by_id[order.order_id] = order
        self.actor_orders[actor]["sell"].append(order.order_id)

        actor.active_orders[order.order_id] = f"sell {commodity_type.id}"

        self._record_order_event("created", order)

        return order.order_id

    def _clear_history_read_caches(self) -> None:
        """Drop the per-turn memos of trade-history reads.

        Called when current_turn advances and again before match_orders
        mutates the histories, so every read equals an uncached computation.
        """
        self._avg_price_cache.clear()
        self._price_signal_cache.clear()
        self._avg30_price_cache.clear()
        self._avg30_volume_cache.clear()
        self._stdev30_cache.clear()

    def match_orders(self) -> None:
        """Match orders for every commodity and append the turn's history."""
        # Matching rewrites last_traded_prices, price_history, and
        # volume_history; anything read after this point must be recomputed.
        self._clear_history_read_caches()
        self._trim_transaction_history()

        all_commodities = (
            set(list(self.buy_orders.keys()))
            .union(self.sell_orders.keys())
            .union(self.volume_history.keys())
        )

        for commodity_type in all_commodities:
            # Sweep out cancelled orders first so book staleness stays within
            # one turn and the truthiness checks below see only live orders.
            self._compact_books(commodity_type)

            buy_book = self.buy_orders.get(commodity_type)
            sell_book = self.sell_orders.get(commodity_type)

            # Capture buy demand standing before matching consumes it.
            requested_buy_qty = sum(o.quantity for o in buy_book) if buy_book else 0

            daily_volume = 0
            average_price_numerator = 0

            # Fills need both sides with the best bid at or above the best
            # ask. Skipping the sort-and-scan otherwise is behavior-neutral:
            # an uncrossed book yields no transactions and is left untouched.
            if buy_book and sell_book:
                best_bid, best_ask = self.get_bid_ask_spread(commodity_type)
                if (
                    best_bid is not None
                    and best_ask is not None
                    and (best_bid >= best_ask)
                ):
                    before_count = len(self.transaction_history)
                    self._match_orders_for_commodity(commodity_type)

                    for tx in self.transaction_history[before_count:]:
                        if tx.commodity_type == commodity_type:
                            daily_volume += tx.quantity
                            average_price_numerator += tx.total_amount

            self._update_scarcity_pressure(
                commodity_type, requested_buy_qty, daily_volume
            )

            price_series = self.price_history[commodity_type]
            volume_series = self.volume_history[commodity_type]
            if daily_volume > 0:
                volume_series.append(daily_volume)
                price_series.append(average_price_numerator // daily_volume)
                self._active_volume_days[commodity_type] += 1
            elif volume_series:
                volume_series.append(0)
                price_series.append(price_series[-1])

            # Bound the series; see HISTORY_KEEP.
            if len(volume_series) > HISTORY_TRIM_THRESHOLD:
                del volume_series[:-HISTORY_KEEP]
                del price_series[:-HISTORY_KEEP]

    def _match_orders_for_commodity(
        self, commodity_type: "CommodityDefinition"
    ) -> None:
        """Match buy and sell orders for one commodity."""
        # Best price first, then oldest. Exact (price, timestamp) ties are
        # broken randomly each pass: kept orders sit at a fixed book position
        # since churn pruning, so without the random key the same actor would
        # win a contested price level every turn and starve thin markets.
        buy_orders = sorted(
            (o for o in self.buy_orders.get(commodity_type, []) if not o.cancelled),
            key=lambda o: (-o.price, o.timestamp, random.random()),
        )

        sell_orders = sorted(
            (o for o in self.sell_orders.get(commodity_type, []) if not o.cancelled),
            key=lambda o: (o.price, o.timestamp, random.random()),
        )

        # Cursors into the sorted books; advancing one is an O(1) pop.
        buy_index = 0
        sell_index = 0

        # Orders stepped over because they would have crossed against their
        # own actor. An order is only ever set aside for the duration of the
        # counterparty it collided with, then put back at the front of its
        # book: deferred entries were popped in price-time order and are
        # therefore at least as aggressive as everything still resting, so
        # prepending restores the exact sort order. Nothing a third party
        # could have filled is skipped -- the deferred order is available
        # again to the very next order on the other side.
        deferred_buys: List[Order] = []
        deferred_sells: List[Order] = []

        while buy_index < len(buy_orders) and sell_index < len(sell_orders):
            buy_order = buy_orders[buy_index]
            sell_order = sell_orders[sell_index]

            if buy_order.price < sell_order.price:
                if not deferred_sells:
                    break
                # Asks set aside for this bid are cheaper than the one that
                # stopped us, so a later, lower bid may still cross them.
                # Retire this bid and reinstate them for the next one.
                deferred_buys.append(buy_order)
                buy_index += 1
                sell_orders = deferred_sells + sell_orders[sell_index:]
                sell_index = 0
                deferred_sells = []
                continue

            if buy_order.actor is sell_order.actor:
                # An actor must never trade with itself: it churns its own
                # money and pollutes the flow price and volume that ship
                # planners read. Step over this ask and try the next one at
                # the same or a worse price for this bid.
                deferred_sells.append(sell_order)
                sell_index += 1
                if sell_index >= len(sell_orders):
                    # Every crossing ask belonged to this bidder. Retire the
                    # bid and restore the asks for the next bidder.
                    deferred_buys.append(buy_order)
                    buy_index += 1
                    sell_orders = list(deferred_sells)
                    sell_index = 0
                    deferred_sells = []
                continue

            quantity = min(buy_order.quantity, sell_order.quantity)

            # Trades clear at the ask.
            transaction_price = sell_order.price

            self._execute_transaction(
                buyer=buy_order.actor,
                seller=sell_order.actor,
                commodity_type=commodity_type,
                quantity=quantity,
                price=transaction_price,
                buy_order=buy_order,
                sell_order=sell_order,
            )

            buy_order.quantity -= quantity
            sell_order.quantity -= quantity

            self.last_traded_prices[commodity_type].append(transaction_price)

            if len(self.last_traded_prices[commodity_type]) > 10:
                self.last_traded_prices[commodity_type] = self.last_traded_prices[
                    commodity_type
                ][-10:]

            if buy_order.quantity <= 0:
                self._record_order_event("filled", buy_order)

                if buy_order.order_id in self.orders_by_id:
                    del self.orders_by_id[buy_order.order_id]

                buyer = buy_order.actor
                if (
                    buyer in self.actor_orders
                    and buy_order.order_id in self.actor_orders[buyer]["buy"]
                ):
                    self.actor_orders[buyer]["buy"].remove(buy_order.order_id)

                if buy_order.order_id in buyer.active_orders:
                    del buyer.active_orders[buy_order.order_id]

                buy_index += 1

            if sell_order.quantity <= 0:
                self._record_order_event("filled", sell_order)

                if sell_order.order_id in self.orders_by_id:
                    del self.orders_by_id[sell_order.order_id]

                seller = sell_order.actor
                if (
                    seller in self.actor_orders
                    and sell_order.order_id in self.actor_orders[seller]["sell"]
                ):
                    self.actor_orders[seller]["sell"].remove(sell_order.order_id)

                if sell_order.order_id in seller.active_orders:
                    del seller.active_orders[sell_order.order_id]

                sell_index += 1

            if buy_order.quantity <= 0 and deferred_sells:
                # This bid is done; its own asks rejoin the book.
                sell_orders = deferred_sells + sell_orders[sell_index:]
                sell_index = 0
                deferred_sells = []

        # The rebuilt books come from the cancel-filtered sorted lists, so
        # they hold no dead orders and the lazy-delete counters reset.
        self.buy_orders[commodity_type] = deferred_buys + buy_orders[buy_index:]
        self.sell_orders[commodity_type] = deferred_sells + sell_orders[sell_index:]
        self._dead_buy_counts[commodity_type] = 0
        self._dead_sell_counts[commodity_type] = 0
        self._quote_cache.pop(commodity_type, None)
        self.quote_version += 1
        self._on_buy_book_changed(commodity_type)

    def _update_scarcity_pressure(
        self,
        commodity_type: "CommodityDefinition",
        requested_buy_qty: int,
        filled_volume: int,
    ) -> None:
        """Update a commodity's scarcity pressure after matching.

        Rises when buy demand goes unfilled, decays toward zero when demand is
        met or absent. See the SCARCITY_PRESSURE_* constants.
        """
        pressure = self.scarcity_pressure[commodity_type]
        if requested_buy_qty <= 0:
            pressure *= SCARCITY_PRESSURE_DECAY
        else:
            unmet = 1.0 - min(filled_volume, requested_buy_qty) / requested_buy_qty
            pressure = (
                pressure * SCARCITY_PRESSURE_DECAY + SCARCITY_PRESSURE_STEP * unmet
            )
        self.scarcity_pressure[commodity_type] = max(
            0.0, min(SCARCITY_PRESSURE_MAX, pressure)
        )

    def scarcity_pressure_for(self, commodity_type: "CommodityDefinition") -> float:
        """Current scarcity pressure for a commodity; 0 if never under-served."""
        return self.scarcity_pressure[commodity_type]

    def _execute_transaction(
        self,
        buyer: MarketParticipant,
        seller: MarketParticipant,
        commodity_type: "CommodityDefinition",
        quantity: int,
        price: int,
        buy_order: Order,
        sell_order: Order,
    ) -> None:
        """Move goods and money for one fill and record the Transaction.

        Goods move first; money follows for the quantity that moved.
        """
        actual_quantity = quantity

        if sell_order:
            # Move the goods out of the seller's reservation, then out of
            # inventory. A reservation shortfall should not happen; it is
            # logged and the fill shrinks to match.
            reserved_quantity = min(
                quantity, seller.inventory.get_reserved_quantity(commodity_type)
            )
            if reserved_quantity < quantity:
                actual_quantity = reserved_quantity
                print(
                    f"WARNING: Reserved quantity ({reserved_quantity}) less than transfer quantity ({quantity})"
                )

            seller.inventory.unreserve_commodity(commodity_type, actual_quantity)

            if not seller.inventory.remove_commodity(commodity_type, actual_quantity):
                print(
                    f"ERROR: Failed to remove {actual_quantity} of {commodity_type.id} from seller inventory"
                )
                actual_quantity = 0
        else:
            # No order: take directly from available inventory.
            if not seller.inventory.remove_commodity(commodity_type, quantity):
                print(
                    f"ERROR: Failed to remove {quantity} of {commodity_type.id} from seller inventory"
                )
                actual_quantity = 0

        total_amount = actual_quantity * price

        if buy_order:
            # Money was reserved at the bid price; release the moved
            # quantity's share and refund the gap down to the clearing price.
            reserved_amount = min(
                actual_quantity * buy_order.price, buyer.reserved_money
            )
            buyer.reserved_money -= reserved_amount

            price_diff = buy_order.price - price
            if price_diff > 0 and actual_quantity > 0:
                refund = actual_quantity * price_diff
                buyer.money += refund
        else:
            buyer.money -= total_amount

        seller.money += total_amount

        buyer.inventory.add_commodity(commodity_type, actual_quantity)

        transaction = Transaction(
            buyer=buyer,
            seller=seller,
            commodity_type=commodity_type,
            quantity=actual_quantity,
            price=price,
            total_amount=total_amount,
            turn=self.current_turn,
            buy_order_id=buy_order.order_id,
            sell_order_id=sell_order.order_id,
        )
        self.transaction_history.append(transaction)
        self.actor_transaction_history[buyer.name].append(transaction)
        self.actor_transaction_history[seller.name].append(transaction)

    def get_avg_price(self, commodity_type: "CommodityDefinition") -> int:
        """Mean of the last 10 fill prices.

        Falls back to the last per-turn price, then to a default of 10. See
        ``has_price_signal`` to tell the default from a real price.
        """
        cached = self._avg_price_cache.get(commodity_type)
        if cached is not None:
            return cached

        prices = self.last_traded_prices.get(commodity_type, [])
        if not prices:
            if (
                commodity_type in self.price_history
                and self.price_history[commodity_type]
            ):
                result = self.price_history[commodity_type][-1]
            else:
                result = 10
        else:
            # Equals int(statistics.mean(prices)) for these non-negative ints
            # without statistics' Fraction arithmetic, which is slow here.
            result = sum(prices) // len(prices)

        self._avg_price_cache[commodity_type] = result
        return result

    def has_price_signal(self, commodity_type: "CommodityDefinition") -> bool:
        """Whether a real trade has ever set a price for ``commodity_type``.

        ``get_avg_price`` returns a default of 10 for never-traded goods,
        indistinguishable from a real price of 10. Callers that must not
        trust the default, such as imputed replacement-cost procurement, use
        this to tell them apart. True only when ``last_traded_prices`` or
        ``price_history`` holds data, and both are written only when a
        transaction clears.
        """
        cached = self._price_signal_cache.get(commodity_type)
        if cached is not None:
            return cached

        result = bool(self.last_traded_prices.get(commodity_type)) or bool(
            self.price_history.get(commodity_type)
        )
        self._price_signal_cache[commodity_type] = result
        return result

    def get_bid_ask_spread(
        self, commodity_type: "CommodityDefinition"
    ) -> Tuple[Optional[int], Optional[int]]:
        """Current (highest bid, lowest ask); either is None if that side is empty."""
        cached = self._quote_cache.get(commodity_type)
        if cached is not None:
            return cached

        buy_orders = self.buy_orders.get(commodity_type, [])
        sell_orders = self.sell_orders.get(commodity_type, [])

        highest_bid = max(
            (o.price for o in buy_orders if not o.cancelled), default=None
        )
        lowest_ask = min(
            (o.price for o in sell_orders if not o.cancelled), default=None
        )

        result = (highest_bid, lowest_ask)
        self._quote_cache[commodity_type] = result
        return result

    def _on_buy_book_changed(self, commodity_type: "CommodityDefinition") -> None:
        """Note a buy-book mutation; drops the bid-levels cache."""
        self._bid_levels_cache.pop(commodity_type, None)

    def get_bid_levels(
        self, commodity_type: "CommodityDefinition"
    ) -> List[Tuple[int, int]]:
        """Resting buy orders as (price, quantity) pairs, best price first.

        Planners need bid depth, not just the top of book: selling more units
        than the book holds at acceptable prices walks down the levels or
        does not fill, so revenue projected from the top bid overestimates.

        The sorted levels are cached until the next buy-book mutation, so
        repeat calls cost one list copy instead of a re-sort.
        """
        cached = self._bid_levels_cache.get(commodity_type)
        if cached is None:
            cached = [
                (o.price, o.quantity)
                for o in self.buy_orders.get(commodity_type, [])
                if not o.cancelled
            ]
            cached.sort(key=lambda level: -level[0])
            self._bid_levels_cache[commodity_type] = cached
        return list(cached)

    def get_ask_levels(
        self,
        commodity_type: "CommodityDefinition",
        exclude_actor: Optional[MarketParticipant] = None,
    ) -> List[Tuple[int, int]]:
        """Resting sell orders as (price, quantity) pairs, cheapest first.

        The mirror of :meth:`get_bid_levels`, for buyers. Top of ask says only
        that *something* is for sale: a one-unit lowball ask is not a cost
        basis for a hold-sized purchase, so a planner sizing a load has to
        walk the levels the way a seller walks the bids.

        ``exclude_actor`` drops that participant's own asks. Matching has no
        buyer-is-seller guard, so a planner that prices a purchase off a book
        containing its own asks bids at or above them and buys its own cargo
        back. Buyers must pass themselves.

        Uncached, unlike the bid side, because it is read once per origin
        commodity per planning ship-turn rather than once per candidate pair.
        """
        levels = [
            (o.price, o.quantity)
            for o in self.sell_orders.get(commodity_type, [])
            if not o.cancelled and o.actor is not exclude_actor
        ]
        levels.sort(key=lambda level: level[0])
        return levels

    def get_bid_price_at_depth(
        self, commodity_type: "CommodityDefinition", quantity: int
    ) -> Optional[int]:
        """Price of the bid level at which resting depth first covers ``quantity``.

        The conservative price for selling ``quantity`` units into the book
        right now: a seller sweeping that many units fills its last unit at
        this level, so no unit is worth more than it. Returns ``None`` when
        the resting bids cannot absorb ``quantity`` at all.

        This is the depth-aware answer to ``get_bid_ask_spread``'s top of
        book. A single one-unit probe far above fair value moves the top bid
        but not this price, so planners that size their output against real
        demand should ask here.

        Brains call this every turn while the buy book is being rebuilt
        around them, so it never sorts the whole book. Every order carries at
        least one unit, so at most ``quantity`` orders can be needed and the
        answer is a bounded partial selection, linear in book size. The
        sorted levels are reused when ``get_bid_levels`` has already built
        them for this book.
        """
        if quantity <= 0:
            return None

        levels = self._bid_levels_cache.get(commodity_type)
        if levels is None:
            levels = [
                (o.price, o.quantity)
                for o in heapq.nlargest(
                    quantity,
                    (
                        o
                        for o in self.buy_orders.get(commodity_type, ())
                        if not o.cancelled
                    ),
                    key=_order_price,
                )
            ]

        cumulative = 0
        for price, level_quantity in levels:
            cumulative += level_quantity
            if cumulative >= quantity:
                return price
        return None

    def get_bid_sweep_average(
        self,
        commodity_type: "CommodityDefinition",
        quantity: int,
        price_clip: float = math.inf,
    ) -> Optional[float]:
        """Mean fill price for selling ``quantity`` units into the resting bids.

        The revenue a seller sweeping the top of the book actually gets,
        per unit, with each level's price clipped at ``price_clip`` so a
        caller can discount a discovery probe without discarding the levels
        under it. ``get_bid_price_at_depth`` returns the last level hit,
        which is the floor of that revenue: two real bids at 300 over a
        1-credit discovery bid value a three-unit run at 1 there and at 200
        here. Returns ``None`` when the resting bids cannot absorb
        ``quantity`` at all. Same bounded partial selection as the depth
        price.
        """
        if quantity <= 0:
            return None

        levels = self._bid_levels_cache.get(commodity_type)
        if levels is None:
            levels = [
                (o.price, o.quantity)
                for o in heapq.nlargest(
                    quantity,
                    (
                        o
                        for o in self.buy_orders.get(commodity_type, ())
                        if not o.cancelled
                    ),
                    key=_order_price,
                )
            ]

        remaining = quantity
        revenue = 0.0
        for price, level_quantity in levels:
            take = min(level_quantity, remaining)
            revenue += min(float(price), price_clip) * take
            remaining -= take
            if remaining <= 0:
                return revenue / quantity
        return None

    def get_30_day_average_price(self, commodity_type: "CommodityDefinition") -> float:
        """Mean per-turn price over the last 30 turns; 10.0 with no history."""
        cached = self._avg30_price_cache.get(commodity_type)
        if cached is not None:
            return cached

        prices = self.price_history.get(commodity_type, [])
        if not prices:
            result = 10.0
        else:
            recent_prices = prices[-30:] if len(prices) >= 30 else prices
            result = sum(recent_prices) / len(recent_prices)

        self._avg30_price_cache[commodity_type] = result
        return result

    def get_30_day_average_volume(self, commodity_type: "CommodityDefinition") -> float:
        """Mean per-turn volume over the last 30 turns; 1.0 with no history."""
        cached = self._avg30_volume_cache.get(commodity_type)
        if cached is not None:
            return cached

        volumes = self.volume_history.get(commodity_type, [])
        if not volumes:
            result = 1.0
        else:
            recent_volumes = volumes[-30:] if len(volumes) >= 30 else volumes
            result = sum(recent_volumes) / len(recent_volumes)

        self._avg30_volume_cache[commodity_type] = result
        return result

    def get_30_day_standard_deviation(
        self, commodity_type: "CommodityDefinition"
    ) -> float:
        """Sample standard deviation of per-turn prices over the last 30 turns.

        With fewer than 2 prices, returns 10% of the 30-turn average price,
        at least 1.0.
        """
        cached = self._stdev30_cache.get(commodity_type)
        if cached is not None:
            return cached

        prices = self.price_history.get(commodity_type, [])
        if not prices or len(prices) < 2:
            result = max(1.0, self.get_30_day_average_price(commodity_type) * 0.1)
            self._stdev30_cache[commodity_type] = result
            return result

        recent_prices = prices[-30:] if len(prices) >= 30 else prices
        # Sample standard deviation with n-1 denominator, matching
        # statistics.stdev in plain float arithmetic; see get_avg_price.
        n = len(recent_prices)
        mean = sum(recent_prices) / n
        sum_sq_dev = sum((p - mean) ** 2 for p in recent_prices)
        result = math.sqrt(sum_sq_dev / (n - 1))
        self._stdev30_cache[commodity_type] = result
        return result

    def has_history(self, commodity_type: "CommodityDefinition") -> bool:
        """Whether the commodity has traded on at least 5 turns, ever.

        Answered in O(1) from ``_active_volume_days``; the raw series is
        trimmed, so it cannot be recounted.
        """
        return self._active_volume_days.get(commodity_type, 0) >= 5

    def set_current_turn(self, turn: int) -> None:
        """Set the turn used to stamp new orders; drops per-turn memos."""
        self.current_turn = turn
        self._clear_history_read_caches()

    def _note_dead_order(
        self,
        book: Dict["CommodityDefinition", List[Order]],
        dead_counts: Dict["CommodityDefinition", int],
        commodity_type: "CommodityDefinition",
    ) -> None:
        """Count one newly cancelled order; compact past the threshold.

        Compaction keeps live orders in relative order, so every
        cancel-skipping reader sees no change.
        """
        dead = dead_counts[commodity_type] + 1
        orders = book[commodity_type]
        if dead > len(orders) // 2:
            book[commodity_type] = [o for o in orders if not o.cancelled]
            dead_counts[commodity_type] = 0
        else:
            dead_counts[commodity_type] = dead

    def _compact_books(self, commodity_type: "CommodityDefinition") -> None:
        """Drop any dead orders resting in this commodity's books."""
        if self._dead_buy_counts.get(commodity_type):
            orders = self.buy_orders[commodity_type]
            self.buy_orders[commodity_type] = [o for o in orders if not o.cancelled]
            self._dead_buy_counts[commodity_type] = 0
        if self._dead_sell_counts.get(commodity_type):
            orders = self.sell_orders[commodity_type]
            self.sell_orders[commodity_type] = [o for o in orders if not o.cancelled]
            self._dead_sell_counts[commodity_type] = 0

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order and release its reserved money or goods.

        Returns False if the order is not open.
        """
        if order_id not in self.orders_by_id:
            return False

        order = self.orders_by_id[order_id]
        actor = order.actor
        commodity_type = order.commodity_type

        self._record_order_event("cancelled", order)

        # Removing an order disturbs the cached best bid/ask only if it sat
        # at the cached best price. Any other removal leaves the cached quote
        # exact, so the cache survives cancel-and-repost churn of non-best
        # orders.
        cached = self._quote_cache.get(commodity_type)
        if cached is not None:
            cached_bid, cached_ask = cached
            if order.price == (cached_bid if order.is_buy else cached_ask):
                self._quote_cache.pop(commodity_type, None)
                self.quote_version += 1
        else:
            # No cached quote, so this order may have been the best on its
            # side. Conservative bump; see __init__.
            self.quote_version += 1

        del self.orders_by_id[order_id]

        # Mark dead instead of rebuilding the book; compact once dead orders
        # outnumber live ones. All book readers skip cancelled orders, so
        # live-order content and relative order match an eager delete.
        order.cancelled = True
        if order.is_buy:
            self._note_dead_order(
                self.buy_orders, self._dead_buy_counts, commodity_type
            )
            self._on_buy_book_changed(commodity_type)

            actor.reserved_money -= order.quantity * order.price
            actor.money += order.quantity * order.price

            if actor in self.actor_orders:
                self.actor_orders[actor]["buy"].remove(order_id)

        else:
            self._note_dead_order(
                self.sell_orders, self._dead_sell_counts, commodity_type
            )

            actor.inventory.unreserve_commodity(commodity_type, order.quantity)

            if actor in self.actor_orders:
                self.actor_orders[actor]["sell"].remove(order_id)

        if order_id in actor.active_orders:
            del actor.active_orders[order_id]

        return True

    def get_actor_orders(self, actor: MarketParticipant) -> Dict[str, List[Order]]:
        """Open orders for an actor, as {"buy": [...], "sell": [...]}."""
        result: dict[str, list[Order]] = {"buy": [], "sell": []}

        if actor not in self.actor_orders:
            return result

        for order_id in self.actor_orders[actor]["buy"]:
            if order_id in self.orders_by_id:
                result["buy"].append(self.orders_by_id[order_id])

        for order_id in self.actor_orders[actor]["sell"]:
            if order_id in self.orders_by_id:
                result["sell"].append(self.orders_by_id[order_id])

        return result
