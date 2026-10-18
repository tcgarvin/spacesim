from spacesim2.core.actor import Actor
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.drives.actor_drive import ActorDrive, DriveMetrics, log_norm_ratio

DAILY_CONSUMPTION = 1
DEBT_DECAY_FACTOR = 0.8
QUALITY_DEBT_DECAY_FACTOR = 0.5
DEBT_MISS_PENALTY = 0.2  # with 0.8 decay, steady-state debt is <= 1.0
PANTRY_TARGET = 7.0
PANTRY_MAX = 30.0
URGENCY = 1
DRIVE_NAME = "food"

STAPLE_COMMODITY_ID = "processed_food"
QUALITY_COMMODITY_ID = "food"


class FoodDriveMetrics(DriveMetrics):
    def get_name(self) -> str:
        return DRIVE_NAME

    def get_score(self) -> float:
        # Score is hunger, measured by debt.
        return 1 - self.debt


class FoodDrive(ActorDrive):
    """The daily meal.

    The staple is ``processed_food``, made in bulk at a chemical plant. The
    quality good is ``food``, cooked by hand from biomass; it is more
    expensive per unit and eating it decays debt faster. Both are bid for,
    both count toward the pantry, and the cheaper ask wins each purchase.
    """

    MISS_PENALTY = DEBT_MISS_PENALTY
    TARGET_UNITS = 6

    def __init__(self, commodity_registry: CommodityRegistry):
        super().__init__(commodity_registry=commodity_registry)
        self.metrics = FoodDriveMetrics(
            health=1.0, debt=0.0, buffer=0.0, urgency=URGENCY
        )
        staple = commodity_registry.get_commodity(STAPLE_COMMODITY_ID)
        if staple is None:
            raise ValueError(
                f"FoodDrive requires a registered '{STAPLE_COMMODITY_ID}' commodity"
            )
        quality = commodity_registry.get_commodity(QUALITY_COMMODITY_ID)
        if quality is None:
            raise ValueError(
                f"FoodDrive requires a registered '{QUALITY_COMMODITY_ID}' commodity"
            )
        self.staple_commodity = staple
        self.quality_commodity = quality

    def materials(self) -> list[CommodityDefinition]:
        # Staple first: it is the cheaper unit and the default target when
        # neither good is for sale locally.
        return [self.staple_commodity, self.quality_commodity]

    def target_units(self) -> int:
        return self.TARGET_UNITS

    def security(self, actor: Actor, unit_price: float) -> float:
        """Food security counting both the pantry and purchasing power.

        Actors keep only a few days of food on hand by policy, so the pantry
        buffer alone sits low forever and money never looks cheap, which
        caps what any actor will pay for comfort goods at a small multiple
        of the food price. Days of food the actor could buy with its money
        are added to the pantry before the same log-normalization, so a
        solvent actor on a working food market is secure even with a small
        pantry, while a broke actor is not.
        """
        pantry_days = self.pantry_units(actor) / DAILY_CONSUMPTION
        affordable_days = actor.money / unit_price if unit_price > 0 else 0.0
        return log_norm_ratio(pantry_days + affordable_days, PANTRY_TARGET, PANTRY_MAX)

    def pantry_units(self, actor: Actor) -> int:
        """Units of food of either quality the actor holds."""
        return actor.inventory.get_available_quantity(
            self.staple_commodity
        ) + actor.inventory.get_available_quantity(self.quality_commodity)

    def tick(self, actor: Actor) -> DriveMetrics:
        # Staple first; the hand-cooked good is eaten only when the staple
        # has run out, and it decays debt faster.
        did_eat = False
        ate_quality = False
        if actor.inventory.remove_commodity(self.staple_commodity, DAILY_CONSUMPTION):
            did_eat = True
        elif actor.inventory.remove_commodity(
            self.quality_commodity, DAILY_CONSUMPTION
        ):
            did_eat = True
            ate_quality = True
        actor.food_consumed_this_turn = did_eat

        # Buffer counts both food types.
        pantry_days = self.pantry_units(actor) / DAILY_CONSUMPTION

        decay = QUALITY_DEBT_DECAY_FACTOR if ate_quality else DEBT_DECAY_FACTOR
        self._update_metrics(
            health=float(did_eat),
            debt=self.metrics.debt * decay + (not did_eat) * DEBT_MISS_PENALTY,
            buffer=log_norm_ratio(pantry_days, PANTRY_TARGET, PANTRY_MAX),
            urgency=URGENCY,
        )

        return self.metrics


def food_pantry_units(actor: Actor) -> int:
    """Units of edible food the actor holds, staple plus hand-cooked.

    The brains' cook-or-gather gates read this instead of the ``food``
    commodity alone, so an actor living on bought staple does not also cook.
    An actor with no food drive returns 0 and so keeps cooking; only actors
    that eat carry the drive.
    """
    for drive in actor.drives:
        if isinstance(drive, FoodDrive):
            return drive.pantry_units(actor)
    return 0
