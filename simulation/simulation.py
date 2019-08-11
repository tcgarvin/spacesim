from collections import defaultdict
from sparklines import sparklines

from good import food
from universe import Universe


class Simulation:
    def __init__(self, universe: Universe):
        self.universe = universe
        self.max_ticks = 1000

    def run(self):
        tick = 0
        while tick < self.max_ticks:
            self.universe.tick()
            population, live_planets = self.universe.calculate_population()
            print(
                f"Day {tick:>4}.   Population: {population:>5}.   Active Planets: {live_planets:>3}."
            )

            tick += 1

        aggregate_person_food = [0] * 41
        aggregate_person_health = [0] * 11
        for system in self.universe.systems:
            for planet in system.planets:
                for person in planet.people.values():
                    amount_food = min(person.goods[food], 40)
                    aggregate_person_food[amount_food] += 1
                    aggregate_person_health[person.health] += 1
                aggregate_person_health[0] += 100 - len(planet.people)

        for line in sparklines(aggregate_person_food):
            print(" Food distribution for living:", line)
        for line in sparklines(aggregate_person_health):
            print(
                "    Health Level Distribution:",
                line,
                f"({100*100 - aggregate_person_health[0]} / {100*100} alive)",
            )
