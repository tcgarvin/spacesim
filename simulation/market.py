﻿import math
from good import Good

class Market:
    def __init__(self, good: Good, max_price, elasticity, starting_supply):
        self.max_price = max_price
        self.elasticity = elasticity
        self.supply = starting_supply

    def current_price(self):
        # P = a − bQ, a - the price at which none of the good is desired, b the elasticity, Q the quantity available
        return max(self.max_price - (self.elasticity * self.supply), 0)

    def buy_one(self):
        if self.supply is 0:
            raise Exception("No available sellers")
        price = self.current_price()
        self.supply = self.supply - 1
        return price

    def sell_one(self):
        price = self.current_price()
        self.supply = self.supply + 1
        return price

    def buy(self, quantity):
        if self.supply < quantity:
            raise Exception("Insufficient goods")

        startingPrice = self.current_price()
        self.supply = self.supply - quantity
        endingPrice = self.current_price()
        return (startingPrice + (endingPrice + 1)) / 2.0

    def sell(self, quantity):
        revenue = 0
        for i in range(quantity):
            revenue = revenue + self.sell_one()
        return revenue
