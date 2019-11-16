from __future__ import annotations
from collections import Counter
from distribution import Distribution, Normal


class GoodKind:
    """
    The definition of a kind of good.  "Vegtable" is a kind of good, as is 
    "Iron Ore", "Rocket Fuel", and "Electic Motor"
    """

    def __init__(self, name: str):
        assert len(name) > 0

        self.name = name

    def __hash__(self):
        # Implimenting hash so I can use this in dictionaries
        # TODO, look again at Dataclasses
        return hash(repr(self))

    def __repr__(self):
        return f"GoodKind('{self.name}')"


class BagOfGoods(Counter):
    def several(self, times: int):
        """
        Returns a new bag of goods containing a set of goods that is a
        multiple (times) of the callee.
        """
        return BagOfGoods({g: self[g] * times for g in self.keys()})

    def divide(self, other: BagOfGoods):
        """
        Divides one bag of goods by another.  Returns the quotient as an
        integer.  (Whole number quotients, only.  "Natural" division, no
        negatives, floats, etc.)
        """
        if not any(other.elements()):
            raise ZeroDivisionError()
        return min((self[g] // other[g] for g in other.keys()))

    def divide_with_remainder(self, other: BagOfGoods):
        """
        Like divide(), but returns the quotent and a new bag of goods representing the remainder
        after division.
        """
        quotient = self.divide(other)
        remainder = BagOfGoods({g: self[g] - other[g] * quotient for g in self.keys()})

        return quotient, remainder


class Recipe:
    """
    An accounting of the goods and labor needed to produce something.  An
    invocation of a recipe produces one output
    """

    def __init__(
        self,
        labor_amount: float,
        required_goods: BagOfGoods,
        planet_variation: Distribution,
        person_variation: Distribution,
        labor_variation: Distribution,
        output_good: GoodKind,
    ):
        self.labor_amount = labor_amount
        self.required_goods = required_goods
        self.planet_variation = planet_variation
        self.person_variation = person_variation
        self.labor_variation = labor_variation
        self.output_good = output_good

    def draw_planet_variation(self):
        return max(0, self.planet_variation.draw())

    def draw_person_variation(self):
        return max(0, self.person_variation.draw())

    def draw_labor_variation(self):
        return max(0, self.labor_variation.draw())

    def determine_required_goods(self, output_amount: int):
        """
        Determines the amount of goods and labor required for a given amount of
        output.  Basically does a multiplication
        """
        required_goods = self.required_goods.several(output_amount)
        required_labor = self.labor_amount * output_amount
        return required_goods, required_labor

    def __hash__(self):
        # Implimenting hash so I can use this in dictionaries
        # TODO, look again at Dataclasses1G
        return hash(repr(self))

    def __str__(self):
        return f"Recipe(for '{self.output_good}')"


class FactoryKind:
    def __init__(self, recipe: Recipe, rate: float, name: str = ""):
        self.name = name
        self.rate = rate
        self.recipe = recipe


good_index = {}


def generate_good(good_name: str):
    good = GoodKind(good_name)
    good_index[good_name] = good
    return good


food = generate_good("Food")
wood = generate_good("Wood")

basic_recipe_index = {}


def generate_basic_recipe(
    labor: int,
    good: GoodKind,
    planet_variation: Distribution,
    person_variation: Distribution,
    labor_variation: Distribution,
    required_goods=BagOfGoods(),
):
    recipe = Recipe(
        labor, required_goods, planet_variation, person_variation, labor_variation, good
    )
    basic_recipe_index[good.name] = recipe
    return recipe


basic_food_recipe = generate_basic_recipe(
    1, food, Normal(0.5, 0.5), Normal(1, 0.2), Normal(1, 0.05)
)
basic_wood_recipe = generate_basic_recipe(
    1, wood, Normal(1, 0.5), Normal(1, 0.2), Normal(1, 0.05)
)
