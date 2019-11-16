from __future__ import annotations
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any, Callable, List

from sortedcontainers import SortedKeyList


def count_quantity(orders: List[Order]):
    quantity = 0
    for order in orders:
        quantity += order.quantity_unfilled()

    return quantity


@dataclass()
class Order:
    order_number: int
    offer_price: float
    quantity: int
    callback: OrderCallback = field(repr=False)
    _quantity_filled: int = 0

    def is_filled(self):
        return self.quantity == self._quantity_filled

    def quantity_filled(self):
        return self._quantity_filled

    def quantity_unfilled(self):
        return self.quantity - self._quantity_filled

    def fill(self, quantity_filled):
        self._quantity_filled += quantity_filled


class BuyOrder(Order):
    pass


class SellOrder(Order):
    pass


OrderCallback = Callable[[Order, int], Any]


class Market:
    def __init__(self):
        # Implimentation note: Orders are kept in order, such that the most
        # competative orders are the first in the list, and older orders have
        # priority over newer ones.
        self.sell_orders = SortedKeyList(
            key=lambda order: (order.offer_price, order.order_number)
        )
        self.buy_orders = SortedKeyList(
            key=lambda order: (-order.offer_price, order.order_number)
        )
        self.order_number = 1

    def best_buy_orders(self):
        """
        Get all buy orders that share the most competative (highest) offer price.
        Orders are returned according to the same sorting as the larger order list.
        """
        best_buy_order_price = self.buy_orders[0].offer_price
        return self.buy_orders.irange_key(
            (-best_buy_order_price, 0), (-best_buy_order_price, self.order_number)
        )

    def best_sell_orders(self):
        """
        Get all sell orders that share the most competative (lowest) offer price
        Orders are returned according to the same sorting as the larger order list.
        """
        best_sell_order_price = self.sell_orders[0].offer_price
        return self.sell_orders.irange_key(
            (best_sell_order_price, 0), (best_sell_order_price, self.order_number)
        )

    def lowest_sell_offer(self):
        """
        Get the most competitive sell price
        """
        return self.sell_orders[0].offer_price

    def highest_buy_offer(self):
        """
        Get the most competitive buy price
        """
        return self.buy_orders[0].offer_price

    def place_buy_order(
        self, offer_price: float, quantity: int, callback: OrderCallback
    ) -> BuyOrder:
        """
        Places an order, which is returned (unfilled) to the caller.  Upon
        fulfilment, the person holding the order is called back with 
        `.fill_order(order, amount_filled)`. This market is not doing escarow,
        and it is assumed that the person has kept the needed money in hand to 
        be removed now in exchange for goods.
        """
        order = BuyOrder(self.order_number, offer_price, quantity, callback)
        self.order_number += 1
        self.buy_orders.add(order)
        return order

    def place_sell_order(
        self, offer_price: float, quantity: int, callback: OrderCallback
    ) -> SellOrder:
        """
        Places an order, which is returned (unfilled) to the caller.  Upon
        fulfilment, the person holding the order is called back with 
        `.fill_order(order, amount_filled)`. This market is not doing escarow,
        and it is assumed that the person has kept the needed goods in hand to 
        be removed now in exchange for money.
        """
        order = SellOrder(self.order_number, offer_price, quantity, callback)
        self.order_number += 1
        self.sell_orders.add(order)
        return order

    def cancel_buy_order(self, order: BuyOrder):
        """
        Cancels a buy order.  If the order is not in the market, raises a ValueError
        """
        self.buy_orders.remove(order)

    def cancel_sell_order(self, order: SellOrder):
        """
        Cancels a sell order.  If the order is not in the market, raises a ValueError
        """
        self.sell_orders.remove(order)

    def _resolve_orders(self, orders, num_resolved):
        """
        Resolve a number of orders as much as possible.  In the trivial case,
        all orders are totally filled.  In more complicated resolutions, orders
        are resolved oldest first, leaving some orders unfilled or partially 
        filled.

        returns a list of orders that have been wholy or partially filled
        """
        remaining = num_resolved
        modified_orders = []
        for order in orders:
            to_fill = min(order.quantity_unfilled(), remaining)

            if to_fill > 0:
                remaining -= to_fill
                order.fill(to_fill)
                modified_orders.append((order, to_fill))

                if remaining == 0:
                    break

        return modified_orders

    def execute_orders(self):
        while (
            len(self.buy_orders) > 0
            and len(self.sell_orders) > 0
            and self.highest_buy_offer() >= self.lowest_sell_offer()
        ):
            best_buy_offers = list(self.best_buy_orders())
            best_sell_offers = list(self.best_sell_orders())

            buy_quantity = count_quantity(best_buy_offers)
            sell_quantity = count_quantity(best_sell_offers)

            quantity_resolved = min(buy_quantity, sell_quantity)
            assert quantity_resolved > 0

            executed_buy_orders = self._resolve_orders(
                best_buy_offers, quantity_resolved
            )
            executed_sell_orders = self._resolve_orders(
                best_sell_offers, quantity_resolved
            )
            assert len(executed_buy_orders) > 0
            assert len(executed_sell_orders) > 0

            for order, num_filled in executed_buy_orders:
                if order.is_filled():
                    self.buy_orders.remove(order)

            for order, num_filled in executed_sell_orders:
                if order.is_filled():
                    self.sell_orders.remove(order)

            # Finally, inform the actors that the orders are executed
            for order, num_filled in executed_buy_orders:
                order.callback(order, num_filled)

            for order, num_filled in executed_sell_orders:
                order.callback(order, num_filled)
