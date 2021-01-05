from __future__ import annotations
from abc import ABC, abstractmethod
from collections import defaultdict
from math import log2
from random import randrange
from uuid import UUID, uuid4

from good import BagOfGoods, GoodKind, Recipe, food, wood
from grindstone import grindstone
from order_matching_market import Market, Order, BuyOrder, SellOrder, OrderCallback
from person_need import NeedHierarchy

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from planet import Planet

DAYS_WAGE = 10


class Person:
    """
    A planet-bound person.

    A person has one or more needs, which are evaluated each turn.
    """

    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.needs = NeedHierarchy()
        self.goods = BagOfGoods()
        self.money = 0
        self.factories = []
        self.partial_labor = {}
        self.planet = None
        self.buy_orders = defaultdict(dict)
        self.sell_orders = defaultdict(dict)
        self._has_executed_recipe = False
        self._affected_markets = []

    def set_planet(self, planet: "Planet"):
        self.planet = planet

    def estimate_production(self, recipe: Recipe):
        return grindstone.calculate_effective_labor(recipe, (self,), self.planet)

    def work_for_government(self):
        assert not self._has_executed_recipe
        self.money += DAYS_WAGE
        self._has_executed_recipe = True

    def execute_recipe(self, recipe: Recipe):
        """
        Executes a recipe one or more times.  If there is a partial effective
        labor left over and enough goods, a partial execution may be stored,
        which may allow for extra goods to be produced in the future.
        """
        assert not self._has_executed_recipe
        good = recipe.output_good
        goods_generated = grindstone.calculate_effective_labor(
            recipe, (self,), self.planet
        )
        goods_generated += self.partial_labor.get(recipe, 0)
        self.goods[good] += int(goods_generated)
        self.partial_labor[recipe] = goods_generated % 1
        self._has_executed_recipe = True

    def place_buy_order(
        self, good: GoodKind, offer_price: float, quantity: int, callback: OrderCallback
    ) -> BuyOrder:
        market = self.planet.markets[good]
        order = market.place_buy_order(offer_price, quantity, lambda o, f: self._order_callback(good, o, f, callback))
        self.buy_orders[good][order.order_number] = order
        if market not in self._affected_markets:
            self._affected_markets.append(market)
        return order

    def place_sell_order(
        self, good: GoodKind, offer_price: float, quantity: int, callback: OrderCallback
    ) -> SellOrder:
        market = self.planet.markets[good]
        order = market.place_sell_order(offer_price, quantity, lambda o, f: self._order_callback(good, o, f, callback))
        self.sell_orders[good][order.order_number] = order
        if market not in self._affected_markets:
            self._affected_markets.append(market)
        return order

    def cancel_buy_order(self, good: GoodKind, order:BuyOrder):
        market = self.planet.markets[good]
        market.cancel_buy_order(order)
        del self.buy_orders[good][order.order_number]

    def cancel_sell_order(self, good: GoodKind, order:SellOrder):
        market = self.planet.markets[good]
        market.cancel_sell_order(order)
        del self.sell_orders[good][order.order_number]

    def cancel_all_orders(self):
        for good, order_index in self.sell_orders.items():
            market = self.planet.markets[good]
            for order in order_index.values():
                market.cancel_sell_order(order)

        for good, order_index in self.buy_orders.items():
            market = self.planet.markets[good]
            for order in order_index.values():
                market.cancel_buy_order(order)

        self.buy_orders = defaultdict(dict)
        self.sell_orders = defaultdict(dict)

    def tick(self):
        """
        "Involuntary" actions like satisfying "human needs", and cleanup
        """
        self.needs.visit(self)

        while len(self._affected_markets) > 0:
            market = self._affected_markets.pop()
            market.execute_orders()

        self._has_executed_recipe = False

    def _order_callback(self, good: GoodKind, order: Order, filled: int, callback: OrderCallback):
        if isinstance(order, BuyOrder):
            self.money -= order.offer_price * filled
            self.goods[good] += filled
            if order.is_filled():
                del self.buy_orders[good][order.order_number]

        elif isinstance(order, SellOrder):
            self.money += order.offer_price * filled
            self.goods[good] -= filled
            if order.is_filled():
                del self.sell_orders[good][order.order_number]

        callback(order, filled)

        


def generate_person(planet: Planet):
    return Person(uuid4())
