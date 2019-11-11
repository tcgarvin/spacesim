from collections import defaultdict
import json
from sparklines import sparklines

from good import food, wood
from log import SimulationLogger, make_log_dir_name
from model_json import universe_to_json
from person import FoodNeed, ShelterNeed
from universe import Universe


class Simulation:
    def __init__(self, universe: Universe):
        self.universe = universe
        self.max_ticks = 1000

    def show_status(self):
        good_amounts = {kind: [0] * 41 for kind in (food, wood)}
        need_scores = {need.__name__: [0] * (need.MAX_SCORE + 1) for need in (FoodNeed, ShelterNeed)}
        for system in self.universe.systems:
            for planet in system.planets:
                for person in planet.people.values():
                    for kind, amount in person.goods.items():
                        bounded_amount = min(amount, 40)
                        good_amounts[kind][bounded_amount] += 1

                    for need in (person.needs.food, person.needs.shelter):
                        score = need.get_score()
                        need_scores[need.__class__.__name__][score] += 1

        print(f"{'-' * 40}")
        print("Goods Held Distributions")
        for good, distribution in good_amounts.items():
            for line in sparklines(distribution):
                print(f"{good.name:>15}: 0 {line} 40+")

        print("Need Score Distributions")
        for need_name, distribution in need_scores.items():
            for line in sparklines(distribution):
                print(f"{need_name:>15}: 0 {line} {len(distribution) - 1}")

        print("")

    def run(self):
        tick = 0
        logger = SimulationLogger(make_log_dir_name())
        while tick <= self.max_ticks:
            self.universe.tick()
            print(f"Day {tick:>4}.\r", end="")

            if tick % 100 == 0:
                self.show_status()

            tick += 1

            logger.log_state(tick, universe_to_json(self.universe))