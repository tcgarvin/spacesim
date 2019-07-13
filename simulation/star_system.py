import math
from planet import Planet, generate_planet

class StarSystem:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y
        self.neighbors = []
        self.planets = []

    def __str__(self):
        return '{ x: ' + str(self.x) + ', y:' + str(self.y) + ', ' + str(self.neighbors) + '}'

    def distanceBetween(self, other):
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def add_neighbor(self, neighbor):
        self.neighbors.append(neighbor)

    def add_planet(self, planet : Planet):
        self.planets.append(planet)

    def tick(self):
        for planet in self.planets:
            planet.tick()

def generate_starsystem(x : float, y : float):
    result = StarSystem(x,y)
    result.add_planet(generate_planet())
    return result
