from person import Person
from good import basic_food_recipe, basic_wood_recipe, food
from grindstone import grindstone


class PersonActor:
    def __init__(self, person: Person):
        self.person = person
        self.partial_labor = {}
        self.best_at = None

    def tick(self, planet: "Planet"):
        """
        Entrypoint for person logic.  Makes and executes decisions, and
        triggers consumption/health/involuntary person tick.
        """
        p = self.person

        # Second iteration of strategy logic is straightforward: Obtain food if
        # needed, then wood, then obtain whatever we are best at.
        food_need = p.needs.food
        wood_need = p.needs.shelter
        if food_need.score < food_need.MAX_SCORE or p.goods[food] < 1:
            p.execute_recipe(basic_food_recipe, planet)

        elif wood_need.score < wood_need.MAX_SCORE:
            p.execute_recipe(basic_wood_recipe, planet)

        else:
            if self.best_at is None:
                foodskill = p.estimate_production(basic_food_recipe, planet)
                woodskill = p.estimate_production(basic_wood_recipe, planet)
                self.best_at = basic_food_recipe if foodskill > woodskill else basic_wood_recipe
            p.execute_recipe(self.best_at, planet)

        p.tick()
