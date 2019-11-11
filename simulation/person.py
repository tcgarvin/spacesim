from __future__ import annotations
from abc import ABC, abstractmethod
from collections import defaultdict
from random import randrange
from uuid import UUID, uuid4
from typing import List

from good import BagOfGoods, Recipe, food, wood
from grindstone import grindstone


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

    def estimate_production(self, recipe: Recipe, planet: "Planet"):
        return grindstone.calculate_effective_labor(
            recipe, (self, ), planet
        )


    def execute_recipe(self, recipe: Recipe, planet: "Planet"):
        """
        Executes a recipe one or more times.  If there is a partial effective
        labor left over and enough goods, a partial execution may be stored,
        which may allow for extra goods to be produced in the future.
        """
        good = recipe.output_good
        goods_generated = grindstone.calculate_effective_labor(
            recipe, (self, ), planet
        )
        goods_generated += self.partial_labor.get(recipe, 0)
        self.goods[good] += int(goods_generated)
        self.partial_labor[recipe] = goods_generated % 1

    def tick(self):
        """
        "Involuntary" actions like satisfying "human needs"
        """
        self.needs.visit(self)

class Need(ABC):
    """
    A need in the spirit of Mavlov's hierarchy
    """
    MAX_SCORE = 0
    def __init__(self):
        self.score = self.MAX_SCORE

    def slide_score(self, amount):
        self.score += amount
        if self.score < 0:
            self.score = 0
        elif self.score > self.MAX_SCORE:
            self.score = self.MAX_SCORE

    @abstractmethod
    def visit(self, person : Person):
        """
        Checks that the conditions to satisfy the need is being met.  May 
        consume goods or have other effects on the person.
        """
        pass

    def get_score(self):
        """
        Returns the current score
        """
        return self.score

    @property
    def name(self):
        raise NotImplementedError()


class FoodNeed(Need):
    MAX_SCORE = 30

    def visit(self, person : Person):
        if person.goods[food] > 0:
            person.goods[food] -= 1
            self.slide_score(1)
        else:
            self.slide_score(-1)

    @property
    def name(self):
        return "FoodNeed"


class ShelterNeed(Need):
    MAX_SCORE = 30

    def visit(self, person : Person):
        if (randrange(14) == 0):
            # Shelter has degraded, needs some routine maintenance
            self.slide_score(-1)

        if self.score < self.MAX_SCORE and person.goods[wood] > 0:
            # If shelter is not in good shape but we have wood, can spend a few
            # minutes fixing it up
            person.goods[wood] -= 1
            self.slide_score(1)

    @property
    def name(self):
        return "ShelterNeed"

class NeedHierarchy:
    def __init__(self):
        self.food = FoodNeed()
        self.shelter = ShelterNeed()

    def visit(self, person: Person):
        self.food.visit(person)
        self.shelter.visit(person)

def generate_person():
    return Person(uuid4())
