"""Actor drives govern consumption and expose metrics for scoring.

Drives keep memory, so each instance is attached to one actor.
"""

from dataclasses import dataclass
from math import log1p
from typing import List

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry


def clamp01(x: float) -> float:
    """Clamp a value to the range [0, 1]."""
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def log_norm_ratio(x: float, target: float, cap: float) -> float:
    """Map a buffer to [0, 1] with diminishing returns.

    ln(1 + min(x, cap)/target) / ln(1 + cap/target). Each step toward the
    target raises the metric by less, and the metric saturates at cap.
    """
    ratio = min(max(x, 0.0), cap) / target
    denom = log1p(cap / target)
    return 0.0 if denom <= 0 else clamp01(log1p(ratio) / denom)


@dataclass
class DriveMetrics:
    """Drive metrics, each in [0, 1]."""

    health: float
    debt: float
    buffer: float
    urgency: float

    def get_name(self) -> str:
        raise NotImplementedError()

    def get_score(self) -> float:
        raise NotImplementedError()


def get_zero_metrics() -> DriveMetrics:
    return DriveMetrics(
        health=0,
        debt=0,
        buffer=0,
        urgency=0,
    )


class ActorDrive:
    # Welfare lost when one maintenance event is missed. One consumed unit of
    # a satisfying material avoids one miss, so this is also the per-unit
    # deprivation stake used to price buy orders. Subclasses set it to their
    # DEBT_MISS_PENALTY.
    MISS_PENALTY: float = 0.0
    # Whether this drive is a basic need: counted in wellbeing, the summary
    # verdict, and the prosperity purchase gate. Prosperity drives set False.
    WELLBEING: bool = True

    def __init__(self, commodity_registry: CommodityRegistry):
        self.metrics = get_zero_metrics()

    def can_purchase(self, actor: Actor) -> bool:
        """Whether the brain may place buy orders for this drive this turn.

        Needs always may. Prosperity drives gate on the actor's needs being
        met, so surplus is spent only after subsistence is secure.
        """
        return True

    def deprivation_stake(self) -> float:
        """Welfare value of one consumed unit of this drive's material.

        Each consumption event uses one unit and avoids one miss penalty, so
        the stake is the miss penalty regardless of event frequency. It is
        the numerator of willingness-to-pay and comparable across drives,
        since all drives measure welfare in avoided debt.
        """
        return self.MISS_PENALTY

    def materials(self) -> List[CommodityDefinition]:
        """Commodities that satisfy this drive, basic market good first.

        Need drives return only the basic good; the quality upgrade for a
        category belongs to its own ProsperityDrive instance. Subclasses
        override.
        """
        return []

    def material_servings(self, commodity_id: str) -> float:
        """Consumption events one unit of a material covers.

        1.0 for a consumable: one unit, one event. A durable covers many, so
        the brain counts stock, compares asks, prices and sizes orders per
        serving rather than per unit. Subclasses override.
        """
        return 1.0

    def target_units(self) -> int:
        """Inventory level the actor aims to keep on hand for this drive."""
        return 0

    def marginal_welfare(self) -> float:
        """Welfare benefit of one more unit at current coverage.

        Discounted by the buffer: a well-stocked drive values an extra unit
        less because it sits in inventory longer before use.
        """
        return self.deprivation_stake() * (1.0 - self.metrics.buffer)

    def security(self, actor: Actor, unit_price: float) -> float:
        """How safe the actor is from this drive, in [0, 1].

        The buffer measures stock on hand. Security also counts what the
        actor could buy at ``unit_price``, so a wealthy actor with a small
        pantry is still secure. Used to price money, not to decide purchases:
        the marginal welfare of a unit still follows the physical buffer.
        Defaults to the buffer for drives that do not model purchasing power.
        """
        return self.metrics.buffer

    def _update_metrics(
        self,
        health: float,
        debt: float,
        buffer: float,
        urgency: float,
    ) -> None:
        self.metrics.health = health
        self.metrics.debt = debt
        self.metrics.buffer = buffer
        self.metrics.urgency = urgency

    def tick(self, actor: Actor) -> DriveMetrics:
        """Check needs, consume goods if required, and update metrics."""
        raise NotImplementedError()
