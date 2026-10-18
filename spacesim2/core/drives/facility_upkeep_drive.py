"""Upkeep drive for a facility owned by a service actor.

Same shape as ClothingDrive and ShelterDrive: a daily Bernoulli event, a
material consumed on the event, debt accrued when it cannot be covered. What
differs is that the material is drawn from the facility's weighted failure
table, so a spaceport occasionally needs an exotic good rather than always
the cheap one.
"""

import random
from typing import Dict, List, Optional

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    log_norm_ratio,
)
from spacesim2.core.facility import FacilityDefinition, FailureType

# Tunables. Event probability comes from the facility's yaml entry.
DEBT_DECAY_FACTOR = 0.8
DEBT_MISS_PENALTY = 0.5  # a missed repair is as serious as a missed shelter event
BUFFER_TARGET_DAYS = 120.0
BUFFER_MAX_DAYS = 360.0
URGENCY = 1.0
DRIVE_NAME = "facility_upkeep"


class FacilityUpkeepDriveMetrics(DriveMetrics):
    def get_name(self) -> str:
        return DRIVE_NAME

    def get_score(self) -> float:
        # Score is debt: accumulated neglect of the facility.
        return 1 - self.debt


class FacilityUpkeepDrive(ActorDrive):
    """Maintenance of one facility, priced through the drive-bid path.

    Metrics:

    - ``health`` is the facility's *condition*: ``clamp01(1 - debt)``. Unlike
      the consumption drives, condition is not "do I hold a unit right now"
      but "how well maintained is this thing", which is exactly the
      complement of accumulated neglect. The operator brain reads
      ``metrics.health`` as condition and scales service by it.
    - ``debt`` accrues DEBT_MISS_PENALTY on every event the actor cannot
      cover and decays by DEBT_DECAY_FACTOR on every repair.
    - ``buffer`` is log-normalized days of coverage from stock on hand
      against the expected material draw per day.
    - ``urgency`` is fixed.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY

    def __init__(
        self, commodity_registry: CommodityRegistry, facility: FacilityDefinition
    ):
        super().__init__(commodity_registry=commodity_registry)
        self.facility = facility
        # Failure rows sorted by weight, most likely first. materials() and
        # the weighted draw both read this order.
        self._failures: List[FailureType] = sorted(
            facility.upkeep.failures, key=lambda f: f.weight, reverse=True
        )
        self._weights: List[float] = [failure.weight for failure in self._failures]

        materials_by_id: Dict[str, CommodityDefinition] = {}
        ordered_materials: List[CommodityDefinition] = []
        for failure in self._failures:
            commodity = commodity_registry.get_commodity(failure.material_id)
            if commodity is None:
                raise ValueError(
                    f"FacilityUpkeepDrive for '{facility.id}' requires a registered "
                    f"'{failure.material_id}' commodity"
                )
            if failure.material_id not in materials_by_id:
                materials_by_id[failure.material_id] = commodity
                ordered_materials.append(commodity)
        self._materials_by_id = materials_by_id
        self._materials = ordered_materials

        # Working buffer: the largest single repair, so one of any failure can
        # always be covered from stock.
        self._target_units = max(failure.quantity for failure in self._failures)
        # Expected units drawn per day, used for the coverage buffer.
        total_weight = sum(self._weights)
        expected_units_per_event = (
            sum(f.weight * f.quantity for f in self._failures) / total_weight
        )
        self._expected_units_per_day = max(
            facility.upkeep.event_probability * expected_units_per_event, 1e-9
        )

        # Failure type of the most recent event the actor could not cover, so
        # the operator brain can target its next bid. None once repaired.
        self.last_unmet: Optional[FailureType] = None

        self.metrics = FacilityUpkeepDriveMetrics(
            health=1.0, debt=0.0, buffer=0.0, urgency=URGENCY
        )

    def materials(self) -> List[CommodityDefinition]:
        """Distinct repair materials, most likely failure first."""
        return list(self._materials)

    def target_units(self) -> int:
        return self._target_units

    def _stock(self, actor: Actor) -> int:
        return sum(
            actor.inventory.get_available_quantity(commodity)
            for commodity in self._materials
        )

    def tick(self, actor: Actor) -> DriveMetrics:
        """Roll one upkeep event and update condition."""
        p_event = self.facility.upkeep.event_probability

        did_repair = False
        event_today = random.random() < p_event
        if event_today:
            failure = random.choices(self._failures, weights=self._weights, k=1)[0]
            material = self._materials_by_id[failure.material_id]
            if actor.inventory.remove_commodity(material, failure.quantity):
                did_repair = True
                self.last_unmet = None
            else:
                self.last_unmet = failure

        if event_today:
            debt = DEBT_DECAY_FACTOR * self.metrics.debt
            if not did_repair:
                debt += DEBT_MISS_PENALTY
        else:
            # Quiet days only heal a facility whose owner is holding repair
            # stock; a bare operator stays as neglected as it was.
            has_stock = self._stock(actor) > 0
            debt = self.metrics.debt * (DEBT_DECAY_FACTOR if has_stock else 1.0)
        debt = clamp01(debt)

        expected_coverage_days = self._stock(actor) / self._expected_units_per_day
        buffer = log_norm_ratio(
            expected_coverage_days, BUFFER_TARGET_DAYS, BUFFER_MAX_DAYS
        )

        self._update_metrics(
            health=clamp01(1.0 - debt), debt=debt, buffer=buffer, urgency=URGENCY
        )
        return self.metrics
