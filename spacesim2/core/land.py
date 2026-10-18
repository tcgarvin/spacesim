"""Land: a per-actor extraction coefficient drawn from a planet's curve.

A planet's ``PlanetAttributes`` give the mean availability of each
extractable resource and, per resource, a concentration that shapes how
that availability is spread over the planet's land. Each planet generates
``LANDS_PER_PLANET`` lands at setup. A regular actor claims one when it is
placed on the planet and keeps it for the run. Gathering and mining recipes
read the actor's land, not the planet, so two neighbors on the same planet
can face very different yields.

The coefficient is fixed for the actor's life, which is what the never-reset
``yield_modifier`` group of ``BrainCache`` requires.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Mapping

from spacesim2.core.planet_attributes import PlanetAttributes

# Lands generated per planet at setup. Only regular actors claim one, so the
# 100-actor default uses the pool exactly. More regular actors than lands
# raises ``NoFreeLandError`` at setup.
LANDS_PER_PLANET = 100

# A draw below this is barren and snaps to exactly 0.0. Every valuation site
# treats 0 as "no way to extract here", while a denormal like 1e-310 would
# overflow the per-unit cost division in ``_replacement_cost`` to infinity.
BARREN_FLOOR = 0.01


class NoFreeLandError(RuntimeError):
    """A planet's land pool is exhausted."""


@dataclass(frozen=True)
class Land:
    """One actor's share of a planet's extractable resources.

    ``coefficients`` maps a resource id to its availability in [0, 1].
    A resource with no entry is fully available, the same convention as
    ``PlanetAttributes.get_availability``.
    """

    coefficients: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for resource, value in self.coefficients.items():
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"{resource} must be between 0.0 and 1.0, got {value}")

    @classmethod
    def default(cls) -> "Land":
        """Land with no penalties, for actors that never extract."""
        return cls()

    @classmethod
    def at_mean(cls, attributes: PlanetAttributes) -> "Land":
        """Land whose every coefficient equals the planet mean.

        The infinite-concentration case, and what every actor had before
        land existed. Tests use it to pin a coefficient without sampling.
        """
        return cls(dict(attributes.to_dict()))

    def get_availability(self, commodity_id: str) -> float:
        """Coefficient for a resource; 1.0 if the land has no entry."""
        return self.coefficients.get(commodity_id, 1.0)

    def to_dict(self) -> Dict[str, float]:
        """Serialize to a plain dict for export."""
        return dict(self.coefficients)


def sample_coefficient(mean: float, concentration: float) -> float:
    """Draw one coefficient from a Beta curve with the given mean.

    The curve is Beta(mean * concentration, (1 - mean) * concentration).
    Concentration below 2 is U-shaped, near 2 is flat, and large values
    cluster tightly around the mean. Infinite concentration, or a mean of
    exactly 0 or 1, returns the mean itself. Draws below ``BARREN_FLOOR``
    snap to 0.0.
    """
    if not (0.0 <= mean <= 1.0):
        raise ValueError(f"mean must be between 0.0 and 1.0, got {mean}")
    if concentration <= 0.0:
        raise ValueError(f"concentration must be positive, got {concentration}")
    if math.isinf(concentration) or mean in (0.0, 1.0):
        return mean
    draw = random.betavariate(mean * concentration, (1.0 - mean) * concentration)
    return 0.0 if draw < BARREN_FLOOR else draw


def draw_land(attributes: PlanetAttributes) -> Land:
    """Draw one land from the planet's per-resource curves."""
    coefficients = {
        resource: sample_coefficient(mean, attributes.land_concentration_for(resource))
        for resource, mean in attributes.to_dict().items()
    }
    return Land(coefficients)


def generate_lands(
    attributes: PlanetAttributes, count: int = LANDS_PER_PLANET
) -> List[Land]:
    """Generate the planet's land pool."""
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    return [draw_land(attributes) for _ in range(count)]
