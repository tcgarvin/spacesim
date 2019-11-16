from person import Person, Need
from good import Recipe, basic_food_recipe, basic_wood_recipe, food
from grindstone import grindstone


class PersonActor:
    def __init__(self, person: Person):
        self.person = person
        self.partial_labor = {}
        self.best_at = None

    def _estimate_gain(self, need:Need, recipe:Recipe, planet:"Planet"):
        estimated_fulfillment = self.person.estimate_production(recipe,planet)
        marginal_utility = need.get_marginal_utility()
        score_gain = estimated_fulfillment * marginal_utility
        return score_gain


    def tick(self, planet: "Planet"):
        """
        Entrypoint for person logic.  Makes and executes decisions, and
        triggers consumption/health/involuntary person tick.
        """
        p = self.person

        # Third iteration of strategy logic is straightforward: Obtain whatever
        # gives us the greatest increase in score. If no further score is 
        # possible, then obtain whatever we are best at.
        food_need = p.needs.food
        wood_need = p.needs.shelter

        food_score_gain = self._estimate_gain(food_need, basic_food_recipe, planet)
        shelter_score_gain = self._estimate_gain(wood_need, basic_wood_recipe, planet)
        if food_score_gain > 0 and food_score_gain > shelter_score_gain:
            p.execute_recipe(basic_food_recipe, planet)

        elif shelter_score_gain > 0 and shelter_score_gain > food_score_gain:
            p.execute_recipe(basic_wood_recipe, planet)

        else:
            if self.best_at is None:
                foodskill = p.estimate_production(basic_food_recipe, planet)
                woodskill = p.estimate_production(basic_wood_recipe, planet)
                self.best_at = basic_food_recipe if foodskill > woodskill else basic_wood_recipe
            p.execute_recipe(self.best_at, planet)

        p.tick()
