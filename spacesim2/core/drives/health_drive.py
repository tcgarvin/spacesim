import random

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    log_norm_ratio,
)

# Stochastic health events. A comfort-tier need, less frequent than clothing.
BASE_EVENT_PROB = 1.0 / 90.0  # about 1 health event per 90 days
DEBT_DECAY_FACTOR = 0.8
QUALITY_DEBT_DECAY_FACTOR = 0.5
DEBT_MISS_PENALTY = 0.4  # less critical than food
BUFFER_TARGET_DAYS = 90.0
BUFFER_MAX_DAYS = 270.0  # saturates at about 9 months
URGENCY = 1.0
DRIVE_NAME = "health"

MEDICINE_NAME = "medicine"
ADVANCED_MEDICINE_NAME = "advanced_medicine"


class HealthDriveMetrics(DriveMetrics):
    def get_name(self) -> str:
        return DRIVE_NAME

    def get_score(self) -> float:
        return 1 - self.debt


class HealthDrive(ActorDrive):
    """Health maintenance with quality tiers.

    - Stochastic health events, about 1 per 90 days.
    - Uses advanced_medicine first, then medicine.
    - Quality medicine recovers debt faster.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY
    TARGET_UNITS = 2

    def __init__(self, commodity_registry: CommodityRegistry):
        super().__init__(commodity_registry=commodity_registry)
        self.medicine = commodity_registry.get_commodity(MEDICINE_NAME)
        self.quality_medicine = commodity_registry.get_commodity(ADVANCED_MEDICINE_NAME)
        self.metrics = HealthDriveMetrics(
            health=1.0, debt=0.0, buffer=0.0, urgency=URGENCY
        )

    def materials(self) -> list[CommodityDefinition]:
        # The prosperity health drive owns advanced_medicine; here it is only
        # an emergency fallback.
        return [self.medicine] if self.medicine else []

    def target_units(self) -> int:
        return self.TARGET_UNITS

    def tick(self, actor: Actor) -> DriveMetrics:
        p_event = BASE_EVENT_PROB

        medicine_qty = (
            actor.inventory.get_available_quantity(self.medicine)
            if self.medicine
            else 0
        )
        quality_qty = (
            actor.inventory.get_available_quantity(self.quality_medicine)
            if self.quality_medicine
            else 0
        )
        total_qty = medicine_qty + quality_qty
        has_medicine = total_qty > 0
        health = 1.0 if has_medicine else 0.0

        consumed_quality = False
        event_today = random.random() < p_event
        did_treat = False

        if event_today and has_medicine:
            # Basic first; quality is the fallback.
            if self.medicine and actor.inventory.remove_commodity(self.medicine, 1):
                did_treat = True
            elif self.quality_medicine and actor.inventory.remove_commodity(
                self.quality_medicine, 1
            ):
                did_treat = True
                consumed_quality = True

            # Post-consumption inventory.
            medicine_qty = (
                actor.inventory.get_available_quantity(self.medicine)
                if self.medicine
                else 0
            )
            quality_qty = (
                actor.inventory.get_available_quantity(self.quality_medicine)
                if self.quality_medicine
                else 0
            )
            total_qty = medicine_qty + quality_qty

        # Update debt
        if event_today:
            decay = QUALITY_DEBT_DECAY_FACTOR if consumed_quality else DEBT_DECAY_FACTOR
            debt = decay * self.metrics.debt
            if not did_treat:
                debt += DEBT_MISS_PENALTY
            debt = clamp01(debt)
        else:
            decay_rate = DEBT_DECAY_FACTOR if has_medicine else 1.0
            debt = self.metrics.debt * decay_rate

        # Buffer counts both types.
        exp_events_per_day = max(p_event, 1e-9)
        expected_coverage_days = total_qty / exp_events_per_day
        buffer = log_norm_ratio(
            expected_coverage_days, BUFFER_TARGET_DAYS, BUFFER_MAX_DAYS
        )

        self._update_metrics(health=health, debt=debt, buffer=buffer, urgency=URGENCY)
        return self.metrics
