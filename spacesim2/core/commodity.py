from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass(frozen=True, eq=False)
class CommodityDefinition:
    """Definition of a commodity.

    Commodities are singletons owned by ``CommodityRegistry``, one object per
    id, so identity is the right equality. ``eq=False`` keeps ``object``'s
    identity ``__hash__``/``__eq__``. These objects are dict keys on hot paths
    (inventories, order books, price histories), and a field-based hash would
    rehash all fields on every lookup.

    ``transportable`` is whether a ship can carry the good between planets.
    ``tradable`` is whether it can be bought and sold on a planet market at
    all; it defaults to ``transportable`` when the YAML omits it. The two
    differ only for a good too large to ship but still sold where it is made,
    such as ``ship_hull``. Facilities are neither.
    """

    id: str
    name: str
    transportable: bool
    description: str
    tradable: bool = True

    def __str__(self) -> str:
        return self.name


class CommodityRegistry:
    """Registry that loads and manages commodity definitions."""

    def __init__(self) -> None:
        self._commodities: Dict[str, CommodityDefinition] = {}
        # Shared list returned by all_commodities(); dropped on registration.
        # The registry is immutable after setup, so sharing is safe as long as
        # callers never mutate it.
        self._all_cache: Optional[List[CommodityDefinition]] = None

    def load_from_file(self, filepath: str | Path) -> None:
        """Load commodity definitions from a YAML file.

        Raises ``ValueError`` on a malformed entry: a commodity the rest of
        the simulation cannot price or carry is not something to continue
        past.
        """
        with open(filepath, "r") as f:
            commodities_data = yaml.safe_load(f)

        for commodity_data in commodities_data:
            transportable = commodity_data["transportable"]
            tradable = commodity_data.get("tradable", transportable)
            if transportable and not tradable:
                raise ValueError(
                    f"Commodity {commodity_data['id']!r} is transportable but "
                    "not tradable; ships buy and sell everything they carry."
                )
            commodity_def = CommodityDefinition(
                id=commodity_data["id"],
                name=commodity_data["name"],
                transportable=transportable,
                description=commodity_data["description"],
                tradable=tradable,
            )
            self._commodities[commodity_def.id] = commodity_def
        self._all_cache = None

    def add_commodity(self, commodity_def: CommodityDefinition) -> None:
        """Add a commodity definition to the registry."""
        self._commodities[commodity_def.id] = commodity_def
        self._all_cache = None

    def get_commodity(self, commodity_id: str) -> Optional[CommodityDefinition]:
        """Get a commodity definition by ID."""
        return self._commodities.get(commodity_id)

    def all_commodities(self) -> List[CommodityDefinition]:
        """All commodity definitions, as a cached shared list.

        Called on hot paths every turn. Callers must not mutate the result.
        """
        if self._all_cache is None:
            self._all_cache = list(self._commodities.values())
        return self._all_cache

    def __getitem__(self, commodity_id: str) -> CommodityDefinition:
        """Get a commodity definition by ID using dictionary-like access."""
        returnable = self.get_commodity(commodity_id)
        if returnable is None:
            raise KeyError(f"Commodity with ID '{commodity_id}' not found.")
        return returnable


class Inventory:
    """Manages an actor's inventory of commodities."""

    # Class-level default so Mock(spec=Inventory) sees the attribute;
    # instances shadow it.
    version: int = 0

    def __init__(self) -> None:
        self.commodities: Dict[CommodityDefinition, int] = {}
        self.reserved_commodities: Dict[
            CommodityDefinition, int
        ] = {}  # For market orders
        # Bumped on every mutation (add, remove, reserve, unreserve) so
        # caches can detect a changed inventory with one int comparison.
        # See actor_brain.BrainCache.
        self.version: int = 0

    def add_commodity(self, commodity: CommodityDefinition, quantity: int) -> None:
        """Add a quantity of a commodity to the inventory."""
        if quantity <= 0:
            return

        current_quantity = self.commodities.get(commodity, 0)
        self.commodities[commodity] = current_quantity + quantity
        self.version += 1

    def remove_commodity(self, commodity: CommodityDefinition, quantity: int) -> bool:
        """Remove a quantity of a commodity.

        Returns False, removing nothing, if not enough is available.
        """
        if quantity <= 0:
            return True

        current_quantity = self.commodities.get(commodity, 0)
        if current_quantity < quantity:
            return False

        self.commodities[commodity] = current_quantity - quantity
        self.version += 1

        if self.commodities[commodity] == 0:
            del self.commodities[commodity]

        return True

    def clear(self) -> None:
        """Drop everything held, reserved included.

        Migration uses it: a migrant travels with nothing. Reserved goods
        back live sell orders, so cancel those first or the book will hold
        orders against stock that is gone.
        """
        if not self.commodities and not self.reserved_commodities:
            return
        self.commodities.clear()
        self.reserved_commodities.clear()
        self.version += 1

    def reserve_commodity(self, commodity: CommodityDefinition, quantity: int) -> bool:
        """Move a quantity from available to reserved for a market order.

        Returns False, reserving nothing, if not enough is available.
        """
        available = self.get_available_quantity(commodity)
        if available < quantity:
            return False

        self.commodities[commodity] -= quantity

        current_reserved = self.reserved_commodities.get(commodity, 0)
        self.reserved_commodities[commodity] = current_reserved + quantity

        if self.commodities[commodity] == 0:
            del self.commodities[commodity]

        self.version += 1
        return True

    def unreserve_commodity(
        self, commodity: CommodityDefinition, quantity: int
    ) -> None:
        """Move a quantity from reserved back to available.

        Unreserves at most what is currently reserved.
        """
        if quantity <= 0:
            return

        current_reserved = self.reserved_commodities.get(commodity, 0)
        quantity_to_unreserve = min(quantity, current_reserved)

        if quantity_to_unreserve > 0:
            self.reserved_commodities[commodity] = (
                current_reserved - quantity_to_unreserve
            )

            current_quantity = self.commodities.get(commodity, 0)
            self.commodities[commodity] = current_quantity + quantity_to_unreserve

            if self.reserved_commodities[commodity] == 0:
                del self.reserved_commodities[commodity]
            self.version += 1

    def get_quantity(self, commodity: CommodityDefinition) -> int:
        """Total quantity of a commodity, available plus reserved."""
        available = self.commodities.get(commodity, 0)
        reserved = self.reserved_commodities.get(commodity, 0)
        return available + reserved

    def get_available_quantity(self, commodity: CommodityDefinition) -> int:
        """Available (unreserved) quantity of a commodity."""
        return self.commodities.get(commodity, 0)

    def get_reserved_quantity(self, commodity: CommodityDefinition) -> int:
        """Quantity of a commodity reserved for market orders."""
        return self.reserved_commodities.get(commodity, 0)

    def has_quantity(self, commodity: CommodityDefinition, quantity: int) -> bool:
        """Whether at least this much of a commodity is available (unreserved)."""
        return self.get_available_quantity(commodity) >= quantity

    def get_total_quantity(self) -> int:
        """Total units across all commodities, available plus reserved."""
        total = 0

        for quantity in self.commodities.values():
            total += quantity

        for quantity in self.reserved_commodities.values():
            total += quantity

        return total
