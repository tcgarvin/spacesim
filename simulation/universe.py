from location import Location
from ship import Ship
from random import randint
from star_system import StarSystem, generate_starsystem
from good import GoodKind, Recipe, BagOfGoods
import math


class Universe:
    def __init__(self):
        self.systems = []
        self.ships = []
        self.generate()

    def tick(self):
        for starsystem in self.systems:
            starsystem.tick()

    def generate(self):
        new_systems = []
        for i in range(100):
            x = randint(0, 1000)
            y = randint(0, 1000)
            new_systems.append(generate_starsystem(x, y))

        for system in new_systems:
            # Find the nearest system in 4 directions and link to them
            closest = [None, None, None, None]
            for candidate in new_systems:
                offset_x: int = system.x - candidate.x
                offset_y: int = system.y - candidate.x
                direction: float = math.atan2(offset_y, offset_x) + math.pi
                quadrant: int = int(math.floor(direction / (math.pi / 2))) - 1

                # print(quadrant, closest)
                closestSoFar: StarSystem = closest[quadrant]
                if closestSoFar == None:
                    closest[quadrant] = candidate
                elif system.distanceBetween(candidate) > system.distanceBetween(
                    closestSoFar
                ):
                    closest[quadrant] = candidate

            for near_system in closest:
                if near_system is None:
                    continue
                system.add_neighbor(near_system)
                near_system.add_neighbor(system)

        new_ships = []
        for i in range(100):
            location = Location(new_systems[i])
            ship = Ship(location)
            new_ships.append(ship)

        self.systems = new_systems
        self.ships = new_ships

    print("completed generation")
