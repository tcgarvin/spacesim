"""Per-planet attributes that control resource availability."""

import math
import random
from dataclasses import dataclass, field
from typing import Dict

# Range of the per-resource land concentration rolled at setup, sampled
# log-uniformly. At the low end the planet's land is U-shaped: a few rich
# sites and many barren ones. Near 2 it is flat. At the high end nearly
# every land sits at the planet mean. See ``land.sample_coefficient``.
LAND_CONCENTRATION_MIN = 0.5
LAND_CONCENTRATION_MAX = 50.0

# Lower bound of the uniform biomass draw. Draws below zero clamp to exactly
# 0.0, so the share of planets that cannot grow food at all is
# -low / (1 - low): -0.2 / 1.2 = 1/6. Raise the magnitude for more barren
# planets, set it to 0.0 for none.
BIOMASS_DRAW_LOW = -0.2

RESOURCE_ATTRIBUTES = (
    "biomass",
    "fiber",
    "wood",
    "common_metal_ore",
    "nova_fuel_ore",
    "simple_building_materials",
    "silica",
    "rare_earth_ore",
)


def _log_uniform(low: float, high: float) -> float:
    """Sample uniformly in log space between ``low`` and ``high``."""
    return math.exp(random.uniform(math.log(low), math.log(high)))


def _bimodal_sample(
    low_min: float, low_max: float, high_min: float, high_max: float
) -> float:
    """Sample uniformly from the low range or the high range, 50/50.

    Models resources that are either rare or abundant.
    """
    if random.random() < 0.5:
        return random.uniform(low_min, low_max)
    return random.uniform(high_min, high_max)


@dataclass
class PlanetAttributes:
    """Per-planet resource availability.

    Each resource attribute is a float in [0.0, 1.0]: 0.0 absent, 0.5
    average, 1.0 abundant. It is the mean of the planet's land curve for
    that resource; ``land_concentration`` shapes the spread around it. An
    actor's realized yield comes from the land it claimed (``core/land.py``),
    never from these means directly.
    """

    # Availability of each extractable resource.
    biomass: float = 1.0
    fiber: float = 1.0
    wood: float = 1.0
    common_metal_ore: float = 1.0
    nova_fuel_ore: float = 1.0
    simple_building_materials: float = 1.0
    silica: float = 1.0
    rare_earth_ore: float = 1.0
    # Beta concentration per resource for land draws. A missing entry means
    # infinite concentration: every land gets the mean exactly.
    land_concentration: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate attribute ranges."""
        for attr_name in RESOURCE_ATTRIBUTES:
            value = getattr(self, attr_name)
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"{attr_name} must be between 0.0 and 1.0, got {value}"
                )
        for resource, concentration in self.land_concentration.items():
            if resource not in RESOURCE_ATTRIBUTES:
                raise ValueError(f"unknown resource in land_concentration: {resource}")
            if concentration <= 0.0:
                raise ValueError(
                    f"land_concentration[{resource}] must be positive, "
                    f"got {concentration}"
                )

    def land_concentration_for(self, resource: str) -> float:
        """Concentration of the land curve for a resource; inf if unset."""
        return self.land_concentration.get(resource, math.inf)

    @classmethod
    def generate_random(cls) -> "PlanetAttributes":
        """Roll random attributes with a distribution per resource.

        - biomass: uniform ``BIOMASS_DRAW_LOW``-1.0, negative draws clamped
          to 0.0, so 1/6 of planets have no organic life. Hand-made food
          breaks even at biomass 0.167: gather_biomass yields 8 x
          availability (floored at 1 unit by ``ProcessCommand.execute``),
          make_food turns 4 biomass into 4 food, and an actor eats 1 food
          per turn. Planets below that cannot feed themselves and must
          import food or lose their population to migration.
        - fiber, wood, common_metal_ore, silica: uniform 0.0-1.0
        - nova_fuel_ore: bimodal, 0.0-0.3 or 0.7-1.0
        - rare_earth_ore: bimodal, 0.0-0.2 or 0.6-1.0
        - simple_building_materials: uniform 0.3-1.0, always some available

        Each resource also gets a land concentration, log-uniform between
        ``LAND_CONCENTRATION_MIN`` and ``LAND_CONCENTRATION_MAX``.
        """
        return cls(
            biomass=max(0.0, random.uniform(BIOMASS_DRAW_LOW, 1.0)),
            fiber=random.uniform(0.0, 1.0),
            wood=random.uniform(0.0, 1.0),
            common_metal_ore=random.uniform(0.0, 1.0),
            nova_fuel_ore=_bimodal_sample(0.0, 0.3, 0.7, 1.0),
            simple_building_materials=random.uniform(0.3, 1.0),
            silica=random.uniform(0.0, 1.0),
            rare_earth_ore=_bimodal_sample(0.0, 0.2, 0.6, 1.0),
            land_concentration={
                resource: _log_uniform(LAND_CONCENTRATION_MIN, LAND_CONCENTRATION_MAX)
                for resource in RESOURCE_ATTRIBUTES
            },
        )

    @classmethod
    def default(cls) -> "PlanetAttributes":
        """Return attributes with no penalties (all 1.0)."""
        return cls()

    def get_availability(self, commodity_id: str) -> float:
        """Availability for a commodity; 1.0 if it has no attribute."""
        return getattr(self, commodity_id, 1.0)

    def to_dict(self) -> Dict[str, float]:
        """Serialize the resource means to a dict of attribute name to value.

        Land concentration is exported separately, in ``lands.json``.
        """
        return {resource: getattr(self, resource) for resource in RESOURCE_ATTRIBUTES}
