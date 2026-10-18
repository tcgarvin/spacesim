"""Reusable dealer algorithms shared by service brains.

These are plain functions, not a base class: each service brain (market maker,
spaceport operator) keeps its own class and its own state, and calls in here for
the pieces they genuinely share — reading their own fills off the market's
transaction history, skewing a quote by inventory, sizing a stock target from
observed flow, drawing a per-actor spread, laying out a front-loaded ladder, and
tracking a cost basis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Sequence, Tuple

if TYPE_CHECKING:
    from spacesim2.core.commodity import CommodityDefinition
    from spacesim2.core.market import Market, MarketParticipant, Transaction

# Per-actor spread is drawn once from this range so makers on the same planet do
# not quote identically.
MIN_SPREAD_PERCENTAGE: float = 0.10
MAX_SPREAD_PERCENTAGE: float = 0.30

# Default stock target when a market has no usable trade history. Not a price
# default: it only sizes inventory ambition.
DEFAULT_NEUTRAL_STOCK: int = 25
DEFAULT_STOCK_DAYS: int = 30

# Fraction of a dealer's wallet that may be committed to the buy side in
# aggregate. A dealer whose whole purse rests in bids nobody fills has no cash
# left for anything else, so every service brain caps its buying here.
DEFAULT_BUY_CAPITAL_FRACTION: float = 0.70

# Default randomness source for draw_spread. A module-level instance rather than
# the `random` module so callers can pass their own Random for deterministic
# tests.
DEFAULT_RNG: random.Random = random.Random()


@dataclass(frozen=True)
class FillLot:
    """One of our own fills: how much traded and at what unit price."""

    quantity: int
    price: int


@dataclass
class CommodityFills:
    """Our fills in a single commodity over one ingestion window."""

    buys: List[FillLot] = field(default_factory=list)
    sells: List[FillLot] = field(default_factory=list)

    @property
    def buy_prices(self) -> List[int]:
        """Unit prices we paid, one entry per fill, oldest first."""
        return [lot.price for lot in self.buys]

    @property
    def sell_prices(self) -> List[int]:
        """Unit prices we received, one entry per fill, oldest first."""
        return [lot.price for lot in self.sells]

    def __bool__(self) -> bool:
        """True when this window saw any fill on either side."""
        return bool(self.buys or self.sells)


def commodity_key(commodity: "CommodityDefinition") -> str:
    """Stable dictionary key for a commodity: its name."""
    return getattr(commodity, "name", str(commodity))


def ingest_fills(
    actor: "MarketParticipant", market: "Market", cursor: int
) -> Tuple[int, Dict[str, CommodityFills]]:
    """Read the actor's fills recorded since ``cursor`` and group them.

    The cursor is the highest ``Transaction.transaction_id`` already seen, not
    an index. The market trims each per-actor history to its last
    ``TRANSACTIONS_KEEP_PER_ACTOR`` entries, which shifts every index down: an
    index cursor would silently skip the fills that moved past it, or, if it
    ran off the end, replay the whole retained window and double-count the
    cost basis. Transaction ids are assigned from a monotonic counter, so
    comparing against them is stable under trimming. Fills genuinely dropped
    by trimming are simply lost, which is the retention policy's cost; nothing
    is counted twice.

    Args:
        actor: The participant whose own fills we want.
        market: The market holding the per-actor transaction history.
        cursor: Opaque cursor returned by the previous call; 0 first.

    Returns:
        ``(new_cursor, fills_by_commodity_name)``. Callers store the cursor
        without interpreting it.
    """
    history: List[Transaction] = market.get_actor_transaction_history(actor) or []

    # Ids increase along the history, so the new tail is a suffix: walk back
    # from the end to find where it starts instead of rescanning everything.
    start = len(history)
    while start > 0 and history[start - 1].transaction_id > cursor:
        start -= 1

    grouped: Dict[str, CommodityFills] = {}
    for txn in history[start:]:
        if txn.transaction_id > cursor:
            cursor = txn.transaction_id
        if txn.buyer is actor:
            buying = True
        elif txn.seller is actor:
            buying = False
        else:
            continue  # not our fill

        bucket = grouped.setdefault(commodity_key(txn.commodity_type), CommodityFills())
        lot = FillLot(quantity=txn.quantity, price=txn.price)
        (bucket.buys if buying else bucket.sells).append(lot)

    return cursor, grouped


def skew_midpoint(
    midpoint: int,
    current_qty: int,
    target_qty: int,
    cap: float,
    min_price: int = 1,
) -> int:
    """Skew a quoted midpoint down when over-inventoried, up when under.

    Args:
        midpoint: Unskewed midpoint price.
        current_qty: Units currently held.
        target_qty: Desired units; a non-positive target disables the skew.
        cap: Maximum absolute skew as a fraction of the midpoint (0.5 == ±50%).
        min_price: Floor for the returned price.

    Returns:
        The skewed midpoint, at least ``min_price``.
    """
    if target_qty <= 0:
        return midpoint
    ratio = current_qty / target_qty  # 1.0 == on-target
    raw_skew = -0.5 * (ratio - 1.0)  # gentle slope
    clamped_skew = max(-cap, min(cap, raw_skew))
    return max(min_price, int(round(midpoint * (1.0 + clamped_skew))))


def flow_stock_target(
    market: "Market",
    commodity: "CommodityDefinition",
    days: int = DEFAULT_STOCK_DAYS,
    neutral: int = DEFAULT_NEUTRAL_STOCK,
    floor: int = 0,
) -> int:
    """Desired inventory sized from recent market flow.

    Args:
        market: Market whose rolling volume is read.
        commodity: Commodity to size.
        days: Days of average flow to hold.
        neutral: Target used when the market has no usable history.
        floor: Minimum target, e.g. an operator's one-tank reserve.

    Returns:
        ``days`` times average daily volume plus one, or ``neutral`` without
        history, never below ``floor``.
    """
    if market.has_history(commodity):
        average_volume = market.get_30_day_average_volume(commodity) or 0
        target = max(1, int(average_volume)) * days + 1
    else:
        target = neutral
    return max(floor, target)


def draw_spread(rng: random.Random = DEFAULT_RNG) -> float:
    """Draw this dealer's base half-spread as a fraction of the midpoint.

    Args:
        rng: Source of randomness; defaults to ``DEFAULT_RNG``.

    Returns:
        A uniform draw in ``[MIN_SPREAD_PERCENTAGE, MAX_SPREAD_PERCENTAGE]``.
    """
    return rng.uniform(MIN_SPREAD_PERCENTAGE, MAX_SPREAD_PERCENTAGE)


def front_loaded_weights(levels: int) -> List[int]:
    """Descending ladder weights, heaviest at the touch: ``[n, n-1, ..., 1]``."""
    return [levels - i for i in range(levels)]


def ladder_prices(
    midpoint: int, half_spread: int, levels: int, step: int, floor: int, ascending: bool
) -> List[int]:
    """Evenly spaced ladder prices walking away from the midpoint.

    Args:
        midpoint: Skewed midpoint the ladder brackets.
        half_spread: Distance from the midpoint to the touch.
        levels: Number of price levels.
        step: Spacing between levels.
        floor: Minimum price for any level.
        ascending: True for the ask side (prices rise), False for bids.

    Returns:
        ``levels`` prices, touch first.
    """
    direction = 1 if ascending else -1
    return [
        max(floor, midpoint + direction * (half_spread + i * step))
        for i in range(levels)
    ]


def cost_basis(
    units: int,
    total_cost: float,
    buys: Sequence[FillLot],
    sells: Sequence[FillLot],
) -> Tuple[int, float, float]:
    """Roll a running cost basis forward through one window of fills.

    Purchases add their cost to the pool. Sales retire units at the pool's
    average cost, so the per-unit basis is unchanged by selling and the sale
    price is irrelevant here (profit is the caller's business).

    Args:
        units: Units currently attributed to the pool.
        total_cost: Money spent acquiring those units.
        buys: Fills where we bought.
        sells: Fills where we sold; only their quantities are used.

    Returns:
        ``(units, total_cost, per_unit_basis)`` after applying the fills. The
        per-unit basis is 0.0 when the pool is empty. Selling more than the pool
        holds empties it rather than going negative.
    """
    for lot in buys:
        units += lot.quantity
        total_cost += lot.quantity * lot.price

    for lot in sells:
        if units <= 0:
            continue
        sold = min(lot.quantity, units)
        total_cost -= sold * (total_cost / units)
        units -= sold

    if units <= 0:
        return 0, 0.0, 0.0
    return units, total_cost, total_cost / units
