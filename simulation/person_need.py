from __future__ import annotations
from abc import ABC, abstractmethod
from good import food, wood
from math import log2, log10
from random import randrange

from typing import TYPE_CHECKING, Iterable
if TYPE_CHECKING:
    from person import Person

class Need(ABC):
    @abstractmethod
    def visit(self, person:Person):
        """
        Checks that the conditions to satisfy the need is being met.  May 
        consume goods or have other effects on the person.
        """
        pass

    @abstractmethod
    def get_score(self, person:Person, **kwargs) -> float:
        """
        Returns the current score
        """
        pass

    @abstractmethod
    def get_marginal_utility(self, person:Person, **kwargs) -> float:
        """
        Returns the change in score that will occur with the next unit increase in satisfaction.
        """
        pass

    @abstractmethod
    def get_next_tier_impediment(self, person:Person, **kwargs) -> float:
        """
        Returns a float between 0 and 1 inclusive that indicates whether or not
        the potential score of the next tier of needs is impeded by this need.
        A return value of 0 indicates that the next tier is completely impeded,
        while a 1 indicates no impediment whatsoever.
        """
        pass


    @property
    def name(self) -> str:
        raise NotImplementedError()


class PhysicalNeed(Need):
    """
    A need in the spirit of Mavlov's hierarchy
    """
    MAX_FULFILLMENT_SCORE = 0
    def __init__(self):
        self.fulfillment_score = self.MAX_FULFILLMENT_SCORE // 2

    def slide_fulfillment_score(self, amount):
        self.fulfillment_score += amount
        if self.fulfillment_score < 0:
            self.fulfillment_score = 0
        elif self.fulfillment_score > self.MAX_FULFILLMENT_SCORE:
            self.fulfillment_score = self.MAX_FULFILLMENT_SCORE

    def get_score(self, person:Person, tweak_fulfillment:int=0):
        return log2(self.get_fulfillment_score() + tweak_fulfillment + 1)

    def get_marginal_utility(self, person:Person):
        return self.get_score(person, tweak_fulfillment=1) - self.get_score(person)

    def get_fulfillment_score(self):
        """
        Returns the current fulfillment_score.  The fulfillment score increases
        linearly to a maxumum, while the score itself may be subject to
        decreasing marginal utility
        """
        return self.fulfillment_score

    def get_next_tier_impediment(self, person):
        return self.fulfillment_score / self.MAX_FULFILLMENT_SCORE


class FoodNeed(PhysicalNeed):
    MAX_FULFILLMENT_SCORE = 30

    def visit(self, person : Person):
        if person.goods[food] > 0:
            person.goods[food] -= 1
            self.slide_fulfillment_score(1)
        else:
            self.slide_fulfillment_score(-1)

    @property
    def name(self):
        return "FoodNeed"


class ShelterNeed(PhysicalNeed):
    MAX_FULFILLMENT_SCORE = 30

    def visit(self, person : Person):
        if (randrange(14) == 0):
            # Shelter has degraded, needs some routine maintenance
            self.slide_fulfillment_score(-1)

        if self.fulfillment_score < self.MAX_FULFILLMENT_SCORE and person.goods[wood] > 0:
            # If shelter is not in good shape but we have wood, can spend a few
            # minutes fixing it up
            person.goods[wood] -= 1
            self.slide_fulfillment_score(1)

    @property
    def name(self):
        return "ShelterNeed"

class WealthNeed(Need):
    def __init__(self, preliminary_needs:Iterable[Need]):
        self.preliminary_needs = preliminary_needs

    def visit(self, person:Person):
        pass

    def get_score(self, person:Person, tweak_money=0):
        modifier = 1
        for need in self.preliminary_needs:
            modifier *= need.get_next_tier_impediment(person)

        return log10(person.money + 10) * modifier

    def get_marginal_utility(self, person:Person, **kwargs):
        return self.get_score(person, tweak_money=1) - self.get_score(person)

    def get_next_tier_impediment(self, person:Person):
        return 1.0

    @property
    def name(self):
        raise NotImplementedError()


class NeedHierarchy:
    def __init__(self):
        self.food = FoodNeed()
        self.shelter = ShelterNeed()

    def visit(self, person: Person):
        self.food.visit(person)
        self.shelter.visit(person)