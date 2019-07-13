from __future__ import annotations
from collections import Counter

class GoodKind:
    """
    The definition of a kind of good.  "Vegtable" is a kind of good, as is 
    "Iron Ore", "Rocket Fuel", and "Electic Motor"
    """
    def __init__(self, name : str):
        assert len(name) > 0

        self.name = name

    def __hash__(self):
        # Implimenting hash so I can use this in dictionaries
        return hash(repr(self))

class BagOfGoods(Counter):
    def several(self, times : int):
        """
        Returns a new bag of goods containing a set of goods that is a
        multiple (times) of the callee.
        """
        return {g: self[g] * times for g in self.keys()}

    def divide(self, other : BagOfGoods):
        """
        Divides one bag of goods by another.  Returns the quotient as an
        integer.  (Whole number quotients, only.  "Natural" division, no
        negatives, floats, etc.)
        """
        return min((self[g] // other[g] for g in other.keys()))

    def divide_with_remainder(self, other : BagOfGoods):
        """
        Like divide(), but returns the quotent and a new bag of goods representing the remainder
        after division.
        """
        quotient = self.divide(other)
        remainder = BagOfGoods({g: self[g] - other[g] * quotient for g in self.keys()})

        return quotient, remainder

class Recipe():
    """
    An accounting of the goods and labor needed to produce something.  An
    invocation of a recipe produces one output
    """
    def __init__(self, labor_amount : float, required_goods : BagOfGoods, output_good : GoodKind):
        self.labor_amount = labor_amount
        self.required_goods = required_goods
        self.output_good = output_good

    def determine_required_goods(self, output_amount : int):
        """
        Determines the amount of goods and labor required for a given amount of
        output.  Basically does a multiplication
        """
        required_goods = self.required_goods.several(output_amount)
        required_labor = self.labor_amount * output_amount
        return required_goods, required_labor

class FactoryKind:
    def __init__(self, recipe : Recipe, rate : float, name : str = ""):
        self.name = name
        self.rate = rate
        self.recipe = recipe

    def produce_goods(self, number : int, amount_labor : float, input_goods : BagOfGoods):
        """
        Calculates the goods produced by the given amount of labor and the
        given goods.
        """
        required_goods, required_labor = self.recipe.determine_required_goods(number)
        
        #if amount_labor < required_labor:
        