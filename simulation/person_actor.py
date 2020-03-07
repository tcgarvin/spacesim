from abc import ABC, abstractmethod
from collections import defaultdict, deque
from statistics import mean

from person import Person
from person_need import Need
from good import Recipe, basic_food_recipe, basic_wood_recipe, food, wood
from grindstone import grindstone

class PersonActor(ABC):
    @abstractmethod
    def tick(self):
        """
        Entrypoint for person logic.  Makes and executes decisions.
        """
        pass

class Plebeian(PersonActor):
    """
    A Plebeian is the most basic actor, making things by hand or taking jobs,
    buying things they cannot make by hand at the market.  A regular joe.
    """
    def __init__(self, person: Person):
        self.person = person
        self.partial_labor = {}
        self.best_at = None

    def _estimate_gain(self, need: Need, recipe: Recipe):
        estimated_fulfillment = self.person.estimate_production(recipe)
        marginal_utility = need.get_marginal_utility(self.person)
        score_gain = estimated_fulfillment * marginal_utility
        return score_gain

    def tick(self):
        """
        # Third iteration of strategy logic is straightforward: Obtain whatever
        # gives us the greatest increase in score. If no further score is
        # possible, then obtain whatever we are best at.
        """
        p = self.person

        food_need = p.needs.food
        wood_need = p.needs.shelter

        food_score_gain = self._estimate_gain(food_need, basic_food_recipe)
        shelter_score_gain = self._estimate_gain(wood_need, basic_wood_recipe)
        if food_score_gain > 0 and food_score_gain > shelter_score_gain:
            p.execute_recipe(basic_food_recipe)

        elif shelter_score_gain > 0 and shelter_score_gain > food_score_gain:
            p.execute_recipe(basic_wood_recipe)

        else:
            if self.best_at is None:
                foodskill = p.estimate_production(basic_food_recipe)
                woodskill = p.estimate_production(basic_wood_recipe)
                self.best_at = (
                    basic_food_recipe if foodskill > woodskill else basic_wood_recipe
                )
            p.execute_recipe(self.best_at)

class MarketMaker(PersonActor):
    """
    A MarketMaker prefers to arbitrage in the market to make their living.
    """
    def __init__(self, person:Person):
        self.person = person
        self._allocations = defaultdict(int)
        self._sold_recently = defaultdict(lambda: deque(maxlen=30))
        self._bought_recently = defaultdict(lambda: deque(maxlen=30))

    def tick(self):
        """
        """
        p = self.person
        for good in [food, wood]:
            amount_held = p.goods[good]
            money_available = self._allocations[good]
            market = p.planet.markets[good]
            avg_sell_volume = mean(self._sold_recently)
            avg_buy_volume = mean(self._sold_recently)

            # Posit: market makers want volume to grow
            target_sell_volume = int(avg_sell_volume * 1.01 + 1)

            # To meet the target volume, we will need at least that amount in
            # hand, times some factor. For now, let's say we want to be able
            # to continue selling at the current rate without buying for a week.
            amount_wanted_on_hand = int(avg_sell_volume * 7) + 1
            amount_need_to_buy = amount_wanted_on_hand - amount_held


        pass

