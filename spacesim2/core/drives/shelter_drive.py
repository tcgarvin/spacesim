import random

from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import (
    ActorDrive,
    DriveMetrics,
    clamp01,
    log_norm_ratio,
)

# Stochastic maintenance model, same shape as ClothingDrive.
BASE_EVENT_PROB = (
    1.0 / 120.0
)  # about 1 maintenance event per 120 days, less frequent than clothing
DEBT_DECAY_FACTOR = 0.8
QUALITY_DEBT_DECAY_FACTOR = 0.5
DEBT_MISS_PENALTY = 0.5  # missing shelter is serious
BUFFER_TARGET_DAYS = 120.0  # 4 months
BUFFER_MAX_DAYS = 360.0  # saturates at 1 year
URGENCY = 1.0
DRIVE_NAME = "shelter"
# A prefab dwelling serves every maintenance event and is consumed only this
# often: mean 10 events, about 1200 turns at the 1/120 event rate.
PREFAB_WEAR_PROBABILITY = 0.1
PREFAB_SERVINGS = 1.0 / PREFAB_WEAR_PROBABILITY

BUILDING_MATERIALS_NAME = "simple_building_materials"
PREFAB_HOUSING_NAME = "prefab_housing"


class ShelterDriveMetrics(DriveMetrics):
    def get_name(self) -> str:
        return DRIVE_NAME

    def get_score(self) -> float:
        # Score is debt: accumulated shelter degradation.
        return 1 - self.debt


class ShelterDrive(ActorDrive):
    """Shelter maintenance with two tiers, one of them durable.

    - Stochastic maintenance events, about 1 per 120 days.
    - A prefab dwelling serves every event and wears out only once in ten,
      so it is the durable tier. Without one the event consumes 1 unit of
      simple_building_materials.
    - The prefab recovers debt faster, as any quality serving does.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY
    TARGET_UNITS = 3

    def __init__(self, commodity_registry: CommodityRegistry):
        super().__init__(commodity_registry=commodity_registry)
        building_materials = commodity_registry.get_commodity(BUILDING_MATERIALS_NAME)
        if building_materials is None:
            raise ValueError(
                f"ShelterDrive requires a registered '{BUILDING_MATERIALS_NAME}' commodity"
            )
        self.building_materials = building_materials
        self.quality_materials = commodity_registry.get_commodity(PREFAB_HOUSING_NAME)
        self.metrics = ShelterDriveMetrics(
            health=1.0, debt=0.0, buffer=0.0, urgency=URGENCY
        )

    def materials(self) -> list[CommodityDefinition]:
        """Both tiers: the consumable first, then the durable dwelling."""
        if self.quality_materials is None:
            return [self.building_materials]
        return [self.building_materials, self.quality_materials]

    def material_servings(self, commodity_id: str) -> float:
        """A prefab covers PREFAB_SERVINGS events; a brick covers one."""
        return PREFAB_SERVINGS if commodity_id == PREFAB_HOUSING_NAME else 1.0

    def target_units(self) -> int:
        return self.TARGET_UNITS

    def tick(self, actor: Actor) -> DriveMetrics:
        """Process shelter maintenance for this turn."""
        p_event = BASE_EVENT_PROB

        materials_qty = actor.inventory.get_available_quantity(self.building_materials)
        prefab_qty = (
            actor.inventory.get_available_quantity(self.quality_materials)
            if self.quality_materials
            else 0
        )
        # Servings, not units: one prefab covers PREFAB_SERVINGS events.
        total_servings = materials_qty + prefab_qty * PREFAB_SERVINGS
        has_shelter_materials = total_servings > 0

        health = 1.0 if has_shelter_materials else 0.0

        event_today = random.random() < p_event
        did_maintain = False
        used_quality = False

        if event_today and has_shelter_materials:
            if prefab_qty > 0 and self.quality_materials:
                # The dwelling serves the event and only sometimes wears out.
                did_maintain = True
                used_quality = True
                if random.random() < PREFAB_WEAR_PROBABILITY:
                    if actor.inventory.remove_commodity(self.quality_materials, 1):
                        prefab_qty -= 1
            elif actor.inventory.remove_commodity(self.building_materials, 1):
                did_maintain = True
                materials_qty -= 1

            total_servings = materials_qty + prefab_qty * PREFAB_SERVINGS

        if event_today:
            decay = QUALITY_DEBT_DECAY_FACTOR if used_quality else DEBT_DECAY_FACTOR
            debt = decay * self.metrics.debt
            if not did_maintain:
                debt += DEBT_MISS_PENALTY
            debt = clamp01(debt)
        else:
            decay_rate = DEBT_DECAY_FACTOR if has_shelter_materials else 1.0
            debt = self.metrics.debt * decay_rate

        exp_events_per_day = max(p_event, 1e-9)
        expected_coverage_days = total_servings / exp_events_per_day
        buffer = log_norm_ratio(
            expected_coverage_days, BUFFER_TARGET_DAYS, BUFFER_MAX_DAYS
        )

        self._update_metrics(health=health, debt=debt, buffer=buffer, urgency=URGENCY)
        return self.metrics
