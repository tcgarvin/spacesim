from __future__ import annotations
from typing import List

from good import FactoryKind, Recipe, GoodKind, BagOfGoods


class Grindstone:
    """
    A Grindstone is a place at which people do work.  Here lives code for executing recipes to create goods, factories, etc.
    """

    def __init__(self):
        self.effectiveness_ratings = {}

    def calculate_effective_labor(
        self,
        recipe: Recipe,
        laborers: List["Person"],
        planet: "Planet",
        factory: FactoryKind = None,
    ):
        """
        Calculates the amount of production done given:
          - A set of laborers
          - The factory (if any) in which production is occuring
          - The recipe being used
          - The planet the production is occuring on

        There's an assumption in here that 1 "Effective labor" equals one execution of the given recipe.

        This method outputs the number of times the recipe was executed.  This is returned as a float, such that partial executions are possible.
        """
        # This calculation is boring right now, but could involved everything from planet bonuses to randomness
        if not planet.knows_recipe(recipe):
            return 0  # Maybe should throw exception

        effective_labor = 0
        for laborer in laborers:
            effectiveness = self.effectiveness_ratings.get(laborer.uuid, None)
            if effectiveness is None:
                effectiveness = recipe.draw_person_variation()
                self.effectiveness_ratings[laborer.uuid] = effectiveness

            effective_labor += effectiveness * recipe.draw_labor_variation()

        planet_multiplier = self.effectiveness_ratings.get(planet.uuid, None)
        if planet_multiplier is None:
            planet_multiplier = recipe.draw_planet_variation()
            self.effectiveness_ratings[planet.uuid] = planet_multiplier

        recipe_times_executed = effective_labor * planet_multiplier
        return recipe_times_executed


grindstone = Grindstone()  # Singleton?  Unclear.
