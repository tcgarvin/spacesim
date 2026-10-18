import random

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    log_norm_ratio,
)

# Tunables
BASE_EVENT_PROB = 1.0 / 60.0  # ~one replacement event per 60 days
DEBT_DECAY_FACTOR = 0.8
QUALITY_DEBT_DECAY_FACTOR = 0.5
DEBT_MISS_PENALTY = 0.5  # steady state exceeds 1; clamp01 caps debt
BUFFER_TARGET_DAYS = 60.0  # good wardrobe cushion
BUFFER_MAX_DAYS = 180.0  # saturates at about 6 months
CLOTHING_NAME = "clothing"
QUALITY_CLOTHING_NAME = "quality_clothing"
URGENCY = 1.0  # fixed
DRIVE_NAME = "clothing"


class ClothingDriveMetrics(DriveMetrics):
    def get_name(self) -> str:
        return DRIVE_NAME

    def get_score(self) -> float:
        # Score is debt only.
        return 1 - self.debt


class ClothingDrive(ActorDrive):
    """Random-demand clothing replacement with quality tiers.

    - Daily Bernoulli demand with probability BASE_EVENT_PROB.
    - On an event, use clothing first, then quality_clothing as a fallback.
      A miss accrues debt. The prosperity clothing drive owns the quality
      good; it is not bid for here.
    - Quality clothing recovers debt faster.
    - Health reflects current stock, not the event outcome.
    - Buffer is expected days of coverage from all clothing types.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY
    TARGET_UNITS = 3

    def __init__(self, commodity_registry: CommodityRegistry):
        super().__init__(commodity_registry=commodity_registry)
        clothing_good = commodity_registry.get_commodity(CLOTHING_NAME)
        if clothing_good is None:
            raise ValueError(
                f"ClothingDrive requires a registered '{CLOTHING_NAME}' commodity"
            )
        self.clothing_good = clothing_good
        self.quality_good = commodity_registry.get_commodity(QUALITY_CLOTHING_NAME)
        self.metrics = ClothingDriveMetrics(
            health=1.0, debt=0.0, buffer=0.0, urgency=URGENCY
        )

    def materials(self) -> list[CommodityDefinition]:
        return [self.clothing_good]

    def target_units(self) -> int:
        return self.TARGET_UNITS

    def tick(self, actor: Actor) -> DriveMetrics:
        p_event = BASE_EVENT_PROB

        clothing_inventory = actor.inventory.get_available_quantity(self.clothing_good)
        quality_inventory = (
            actor.inventory.get_available_quantity(self.quality_good)
            if self.quality_good
            else 0
        )
        total_inventory = clothing_inventory + quality_inventory
        has_clothes = total_inventory > 0
        health = 1.0 if has_clothes else 0.0

        consumed_quality = False
        event_today = random.random() < p_event
        if event_today:
            # Basic first; quality is the fallback.
            if not actor.inventory.remove_commodity(self.clothing_good, 1):
                if self.quality_good and actor.inventory.remove_commodity(
                    self.quality_good, 1
                ):
                    consumed_quality = True

            # Post-consumption inventory.
            clothing_inventory = actor.inventory.get_available_quantity(
                self.clothing_good
            )
            quality_inventory = (
                actor.inventory.get_available_quantity(self.quality_good)
                if self.quality_good
                else 0
            )
            total_inventory = clothing_inventory + quality_inventory

        decay = QUALITY_DEBT_DECAY_FACTOR if consumed_quality else DEBT_DECAY_FACTOR
        debt = decay * self.metrics.debt + (not has_clothes) * DEBT_MISS_PENALTY
        debt = clamp01(debt)

        # Buffer counts both types.
        exp_events_per_day = max(p_event, 1e-9)
        expected_coverage_days = total_inventory / exp_events_per_day
        buffer = log_norm_ratio(
            expected_coverage_days, BUFFER_TARGET_DAYS, BUFFER_MAX_DAYS
        )

        self._update_metrics(health=health, debt=debt, buffer=buffer, urgency=URGENCY)
        return self.metrics
