from __future__ import annotations
from abc import ABC, abstractmethod
from collections import defaultdict
from math import log2
from random import randrange
from uuid import UUID, uuid4

from good import BagOfGoods, Recipe, food, wood
from grindstone import grindstone
from person_need import NeedHierarchy

from typing import TYPE_CHECKING, List
if TYPE_CHECKING:
    from planet import Planet

DAYS_WAGE = 10

class Person:
    """
    A planet-bound person.

    A person has one or more needs, which are evaluated each turn.
    """

    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.needs = NeedHierarchy()
        self.goods = BagOfGoods()
        self.money = 0
        self.factories = []
        self.partial_labor = {}
        self.planet = None
        self._has_executed_recipe = False

    def set_planet(self, planet:"Planet"):
        self.planet = planet

    def estimate_production(self, recipe: Recipe):
        return grindstone.calculate_effective_labor(
            recipe, (self, ), self.planet
        )

    def work_for_government(self):
        assert not self._has_executed_recipe
        self.money += DAYS_WAGE
        self._has_executed_recipe = True

    def execute_recipe(self, recipe: Recipe):
        """
        Executes a recipe one or more times.  If there is a partial effective
        labor left over and enough goods, a partial execution may be stored,
        which may allow for extra goods to be produced in the future.
        """
        assert not self._has_executed_recipe
        good = recipe.output_good
        goods_generated = grindstone.calculate_effective_labor(
            recipe, (self, ), self.planet
        )
        goods_generated += self.partial_labor.get(recipe, 0)
        self.goods[good] += int(goods_generated)
        self.partial_labor[recipe] = goods_generated % 1
        self._has_executed_recipe = True

    def tick(self):
        """
        "Involuntary" actions like satisfying "human needs"
        """
        self.needs.visit(self)
        self._has_executed_recipe = False

def generate_person(planet:Planet):
    return Person(uuid4())