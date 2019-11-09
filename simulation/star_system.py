from __future__ import annotations
import math
from typing import List
from uuid import uuid4, UUID

from planet import Planet, generate_planet


class StarSystem:
    def __init__(self, system_id:UUID, x: int, y: int):
        self.id = system_id
        self.x = x
        self.y = y
        self._neighbors = {}
        self.planets = []

    def __str__(self):
        return (
            "{ x: "
            + str(self.x)
            + ", y:"
            + str(self.y)
            + "}"
        )

    def distanceBetween(self, other):
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def add_neighbor(self, neighbor : StarSystem):
        self._neighbors[neighbor.id] = neighbor

    def add_planet(self, planet: Planet):
        self.planets.append(planet)

    def tick(self):
        for planet in self.planets:
            planet.tick()


def generate_starsystem(x: float, y: float):
    system_id = uuid4()
    result = StarSystem(system_id, x, y)
    result.add_planet(generate_planet())
    return result
