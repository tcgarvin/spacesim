import random
from typing import TYPE_CHECKING, List, Optional, Tuple

from spacesim2.core.contracts import ContractBoard
from spacesim2.core.land import LANDS_PER_PLANET, Land, NoFreeLandError, generate_lands
from spacesim2.core.planet_attributes import PlanetAttributes

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.market import Market
    from spacesim2.core.ship import Ship


class Planet:
    """Represents a planet in the simulation."""

    def __init__(
        self,
        name: str,
        market: "Market",
        x: float = 0.0,
        y: float = 0.0,
        attributes: Optional[PlanetAttributes] = None,
        num_lands: int = LANDS_PER_PLANET,
    ) -> None:
        self.name = name
        self.x = x
        self.y = y
        self.actors: List["Actor"] = []
        self.ships: List["Ship"] = []  # Ships docked at this planet
        self.market: "Market" = market
        # Transport jobs posted here, read by every ship; see core/contracts.py.
        self.contracts = ContractBoard(self)
        # Default: no resource penalties (all availabilities 1.0).
        self.attributes = attributes if attributes is not None else PlanetAttributes()
        # Unclaimed lands. Claiming removes one at random; see claim_land.
        self.free_lands: List[Land] = generate_lands(self.attributes, num_lands)

    def add_actor(self, actor: "Actor") -> None:
        """Add an actor to this planet, drawing it a land from the pool.

        A land-claiming actor draws one land here and keeps it until it
        leaves. Migration reserves the destination land before the actor
        departs, so it arrives through ``add_actor_with_land`` instead.
        """
        self.actors.append(actor)
        actor.planet = self
        if actor.claims_land:
            actor.land = self.claim_land()

    def add_actor_with_land(self, actor: "Actor", land: Land) -> None:
        """Add an actor that already holds a land reserved from this pool.

        The migration path claims the destination land at departure, so the
        arrival must not draw a second one. A non-claiming actor keeps its
        penalty-free default and ``land`` is returned to the pool.
        """
        self.actors.append(actor)
        actor.planet = self
        if actor.claims_land:
            actor.land = land
        else:
            self.release_land(land)

    def remove_actor(self, actor: "Actor") -> None:
        """Remove an actor from this planet and return its land to the pool.

        The actor keeps ``planet`` pointing here until it is placed
        somewhere else; membership of ``self.actors`` is what this changes.
        """
        if actor not in self.actors:
            raise ValueError(f"{actor.name} is not on {self.name}")
        self.actors.remove(actor)
        if actor.claims_land:
            self.release_land(actor.land)
            actor.land = Land.default()

    def release_land(self, land: Land) -> None:
        """Return a land to the unclaimed pool."""
        self.free_lands.append(land)

    def claim_land(self) -> Land:
        """Remove and return one land chosen at random from the pool."""
        if not self.free_lands:
            raise NoFreeLandError(f"{self.name} has no free land left to claim")
        index = random.randrange(len(self.free_lands))
        # Swap-pop keeps the claim O(1); pool order carries no meaning.
        land = self.free_lands[index]
        self.free_lands[index] = self.free_lands[-1]
        self.free_lands.pop()
        return land

    def add_ship(self, ship: "Ship") -> None:
        """Add a ship to this planet."""
        self.ships.append(ship)
        ship.planet = self

    def get_position(self) -> Tuple[float, float]:
        """Get the position of the planet as a tuple."""
        return (self.x, self.y)
