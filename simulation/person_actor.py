from person import Person
from good import food, basic_food_recipe
from grindstone import grindstone


class PersonActor:
    def __init__(self, person: Person):
        self.person = person
        self.partial_labor = {}

    def tick(self, planet: "Planet"):
        """
        Entrypoint for person logic.  Makes and executes decisions, and
        triggers consumption/health/involuntary person tick.
        """
        p = self.person

        if p.is_dead():
            return

        # First iteration of strategy logic is straightforward: Make food all of the time.
        food_generated = grindstone.calculate_effective_labor(
            basic_food_recipe, (p,), planet
        )
        food_generated += self.partial_labor.get(basic_food_recipe, 0)
        p.goods[food] += int(food_generated)
        self.partial_labor[basic_food_recipe] = food_generated % 1

        p.tick()
