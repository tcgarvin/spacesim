import math
from random import randint, shuffle

from good import GoodKind, Recipe, BagOfGoods
from ship import Ship, generate_ship
from ship_actor import ShipActor
from star_system import StarSystem, generate_starsystem


class Universe:
    def __init__(self):
        self.systems = []
        self.ships = []
        self.ship_actors = []
        self.generate()

    def tick(self):
        actor_turn_order = list(self.ship_actors)
        shuffle(actor_turn_order)
        for actor in actor_turn_order:
            actor.tick()
            actor.ship.tick()

        for starsystem in self.systems:
            starsystem.tick()

    def generate(self):
        new_systems = []
        new_ships = []
        new_ship_actors = []
        for i in range(100):
            x = randint(0, 1000)
            y = randint(0, 1000)
            system = generate_starsystem(x, y)
            new_systems.append(system)

            starting_ship = generate_ship()
            starting_ship.set_initial_position(system, system.planets[0])
            new_ships.append(starting_ship)

            ship_actor = ShipActor(starting_ship)
            new_ship_actors.append(ship_actor)

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

        self.systems = new_systems
        self.ships = new_ships
        self.ship_actors = new_ship_actors

    print("completed generation")
