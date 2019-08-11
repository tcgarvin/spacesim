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

    def __init__(self, uuid: UUID, needs : List[Need]):
        self.uuid = uuid
        self.needs = needs
        self.goods = BagOfGoods()
        self.money = 0
        self.factories = []
        self._partial_execution_recipe = None
        self._partial_execution_amount = 0

    def execute_recipe(self, recipe: Recipe, planet: "Planet"):
        """
        Executes a recipe one or more times.  If there is a partial effective
        labor left over and enough goods, a partial execution may be stored,
        which may allow for extra goods to be produced in the future.
        """
        partial_execution_amount = (
            self._partial_execution_amount
            if self._partial_execution_recipe is recipe
            else 0
        )
        effective_labor_executions = (
            grindstone.calculate_effective_labor(recipe, (self,), planet)
            + partial_execution_amount
        )
        effective_goods_executions, remaining_goods = grindstone.calculate_effective_goods(
            recipe, self.goods, planet
        )

        executions = min(int(effective_labor_executions), effective_goods_executions)
        self.goods = remaining_goods
        self.goods.update({recipe.output_good: executions})

        # We store excess labor iff there are enough goods that we could have made another execution
        left_over_labor = 0
        if effective_goods_executions > executions:
            left_over_labor = effective_labor_executions - int(
                effective_labor_executions
            )

        self._partial_execution_recipe = recipe
        self._partial_execution_amount = left_over_labor


    def tick(self):
        """
        "Involuntary" actions like satisfying "human needs"
        """
        for need in self.needs:
            need.visit(self)


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


class FoodNeed(Need):
    MAX_SCORE = 30

    def visit(self, person : Person):
        if person.goods[food] > 0:
            person.goods[food] -= 1
            self.slide_score(1)
        else:
            self.slide_score(-1)


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


def generate_person():
    return Person(uuid4(), [FoodNeed(), ShelterNeed()])
