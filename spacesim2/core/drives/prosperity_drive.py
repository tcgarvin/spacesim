"""Prosperity drives: consumption above subsistence.

One class covers every category. Each instance owns one tier 2 or tier 3
good, bids for it only while the actor's basic needs are met, and consumes
it at a rate scaled by the actor's taste for the category. Design and
rationale in ``docs/prosperity-design.md``.
"""

import random
from dataclasses import dataclass
from typing import Dict, List, Mapping

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    log_norm_ratio,
)

# Below every need drive's penalty (0.2 to 0.5), so prosperity drives sort
# last in the budget order and only spend surplus.
DEBT_MISS_PENALTY = 0.1
DEBT_DECAY_FACTOR = 0.8
URGENCY = 1.0

# Purchases are gated on every need drive being this healthy. The buffer is
# log-normalized against each drive's own target and cap: 0.2 is three
# units of food (half the 6-unit pantry target; a full pantry after the
# daily meal scores 0.32) and any one unit of clothing, shelter, or
# medicine, each of which scores 0.5. A floor above 0.32 would demand a
# full pantry; a floor above 0.5 would demand two units of every slow good.
GATE_MAX_DEBT = 0.25
GATE_MIN_BUFFER = 0.2

# Coverage is an exponential moving average of "event served", updated on
# event turns only. The half-life is expressed in turns and converted to
# events at the drive's own rate, so a fast and a slow category forget at
# the same wall-clock speed.
COVERAGE_HALF_LIFE_TURNS = 60.0

# The buffer saturates at this multiple of the target coverage.
BUFFER_CAP_MULTIPLE = 3.0

# Taste vector: every category at this weight, one favorite at the higher.
TASTE_BASE = 1.0
TASTE_FAVORITE = 3.0


@dataclass(frozen=True)
class ProsperityCategory:
    """One category of prosperity consumption and its base parameters."""

    name: str
    commodity_id: str
    base_event_prob: float  # consumption events per turn at taste 1.0
    base_target_units: int  # stock kept on hand at taste 1.0


PROSPERITY_CATEGORIES: tuple[ProsperityCategory, ...] = (
    # Hand-cooked food is made by the actor itself, four per labor-turn, so
    # the category runs at the clothing rate: at 1/3 per turn it drew 79% of
    # all process runs and the slow chains lost their labor (decision log,
    # 2026-09-07).
    ProsperityCategory("food", "food", 1.0 / 60.0, 2),
    ProsperityCategory("clothing", "quality_clothing", 1.0 / 60.0, 2),
    ProsperityCategory("health", "advanced_medicine", 1.0 / 90.0, 1),
    ProsperityCategory("luxury", "luxury_goods", 1.0 / 45.0, 2),
)
# prefab_housing and computers are durables, not prosperity consumables:
# ShelterDrive holds the dwelling and IndustrialistBrain holds the computer
# as productive capital, so neither has a category here. See
# docs/prosperity-design.md.

CATEGORY_NAMES: tuple[str, ...] = tuple(c.name for c in PROSPERITY_CATEGORIES)


def random_tastes() -> Dict[str, float]:
    """Roll a taste vector: base weight everywhere, one favorite category."""
    favorite = random.choice(CATEGORY_NAMES)
    return {
        name: TASTE_FAVORITE if name == favorite else TASTE_BASE
        for name in CATEGORY_NAMES
    }


def needs_are_met(actor: Actor) -> bool:
    """Whether every need drive is healthy enough to unlock prosperity bids."""
    for drive in actor.drives:
        if not drive.WELLBEING:
            continue
        if drive.metrics.debt >= GATE_MAX_DEBT:
            return False
        if drive.metrics.buffer < GATE_MIN_BUFFER:
            return False
    return True


@dataclass
class ProsperityDriveMetrics(DriveMetrics):
    """Drive metrics plus coverage, the drive's contribution to prosperity."""

    name: str = ""
    coverage: float = 0.0  # EMA of consumption events that found stock

    def get_name(self) -> str:
        return f"prosperity_{self.name}"

    def get_score(self) -> float:
        return self.coverage


class ProsperityDrive(ActorDrive):
    """Consumption of one upgraded good, unlocked by met needs.

    Taste scales the event rate and the target stock and nothing else. A
    faster rate lowers the buffer for the same stock, which raises marginal
    welfare and so willingness to pay: preference for the favorite good
    comes out of the existing buffer arithmetic.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY
    WELLBEING = False

    def __init__(
        self,
        commodity_registry: CommodityRegistry,
        category: ProsperityCategory,
        taste: float = TASTE_BASE,
    ):
        super().__init__(commodity_registry=commodity_registry)
        if taste <= 0.0:
            raise ValueError(f"taste must be positive, got {taste}")
        good = commodity_registry.get_commodity(category.commodity_id)
        if good is None:
            raise ValueError(
                f"ProsperityDrive '{category.name}' requires a registered "
                f"'{category.commodity_id}' commodity"
            )
        self.category = category
        self.good = good
        self.taste = taste
        self.event_prob = min(1.0, category.base_event_prob * taste)
        # The base class types ``metrics`` as DriveMetrics; this alias keeps
        # the coverage field typed without a cast at every use.
        self.prosperity_metrics = ProsperityDriveMetrics(
            health=0.0, debt=0.0, buffer=0.0, urgency=URGENCY, name=category.name
        )
        self.metrics = self.prosperity_metrics

    def materials(self) -> List[CommodityDefinition]:
        return [self.good]

    def target_units(self) -> int:
        return max(1, round(self.category.base_target_units * self.taste))

    def can_purchase(self, actor: Actor) -> bool:
        return needs_are_met(actor)

    def tick(self, actor: Actor) -> DriveMetrics:
        stock = actor.inventory.get_available_quantity(self.good)
        health = 1.0 if stock > 0 else 0.0

        event_today = random.random() < self.event_prob
        debt = self.metrics.debt
        coverage = self.prosperity_metrics.coverage
        if event_today:
            served = actor.inventory.remove_commodity(self.good, 1)
            if served:
                stock -= 1
            debt = clamp01(debt * DEBT_DECAY_FACTOR + (not served) * DEBT_MISS_PENALTY)
            coverage += (float(served) - coverage) * self._coverage_alpha()

        # Target and cap are in turns of coverage, so taste cancels: a
        # favorite good is stocked deeper but also used faster.
        target_turns = self.category.base_target_units / self.category.base_event_prob
        coverage_turns = stock / self.event_prob
        buffer = log_norm_ratio(
            coverage_turns, target_turns, target_turns * BUFFER_CAP_MULTIPLE
        )

        self._update_metrics(health=health, debt=debt, buffer=buffer, urgency=URGENCY)
        self.prosperity_metrics.coverage = clamp01(coverage)
        return self.metrics

    def _coverage_alpha(self) -> float:
        """Per-event EMA weight for a half-life of COVERAGE_HALF_LIFE_TURNS."""
        events_per_half_life = COVERAGE_HALF_LIFE_TURNS * self.event_prob
        if events_per_half_life <= 0.0:
            return 1.0
        return float(1.0 - 0.5 ** (1.0 / events_per_half_life))


def prosperity_drives(
    commodity_registry: CommodityRegistry, tastes: Mapping[str, float]
) -> List[ProsperityDrive]:
    """One drive per category, weighted by the actor's tastes."""
    return [
        ProsperityDrive(
            commodity_registry, category, tastes.get(category.name, TASTE_BASE)
        )
        for category in PROSPERITY_CATEGORIES
    ]


def prosperity_index(actor: Actor) -> float:
    """Mean coverage over the actor's prosperity drives, in [0, 1].

    Unweighted, so the index means the same thing for every actor and a
    taste draw does not inflate it. 0.0 when the actor has none.
    """
    scores = [
        d.metrics.coverage
        for d in actor.drives
        # isinstance, not WELLBEING, because this needs the
        # ProsperityDriveMetrics-only `coverage` field.
        if isinstance(d.metrics, ProsperityDriveMetrics)
    ]
    if not scores:
        return 0.0
    return clamp01(sum(scores) / len(scores))
