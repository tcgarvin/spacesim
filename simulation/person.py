from collections import defaultdict
from uuid import UUID, uuid4

from good import BagOfGoods, Recipe, food
from grindstone import grindstone


class Person:
    """
    A planet-bound person.

    A person has one need: Food.  They have a health of 10, which rises and
    falls depending on whether or not they eat food.  If their health reaches 
    0, they die, and are expected to be removed from the simulation.
    """

    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.goods = BagOfGoods()
        self.money = 0
        self.health = 10
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

    def is_dead(self):
        return self.health <= 0

    def gain_health(self, amount=1):
        self.health += amount
        if self.health > 10:
            self.health = 10

    def lose_health(self, amount=1):
        self.health -= amount

    def tick(self):
        """
        Involuntary actions like eating and dying, I guess?
        """
        if self.goods[food] > 0:
            self.goods[food] -= 1
            self.gain_health()
        else:
            self.lose_health()


def generate_person():
    return Person(uuid4())
