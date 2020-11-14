from collections import defaultdict
from math import atan2, cos, sin
from uuid import UUID, uuid4

from good import BagOfGoods, GoodKind
from order_matching_market import BuyOrder, SellOrder, Order, OrderCallback
from planet import Planet
from star_system import StarSystem

FTL_SPEED = 10

class ShipState:
    LANDED = "LANDED"
    IN_ORBIT = "IN_ORBIT"
    IN_SYSTEM = "IN_SYSTEM"
    INTERSTELLAR = "INTERSTELLAR"

class Ship:
    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.fuel = 0
        self.cargo = BagOfGoods()
        self.money = 0
        self.buy_orders = defaultdict(dict)
        self.sell_orders = defaultdict(dict)
        self._affected_markets = []
        self._action_taken = False
        self.ftl_destination = None

    def set_initial_position(self, system: StarSystem, planet: Planet):
        self.starsystem = system
        self.x = system.x
        self.y = system.y
        self.planet = planet
        self.state = ShipState.LANDED

    def launch(self):
        assert not self._action_taken
        assert self.state == ShipState.LANDED

        self.state = ShipState.IN_ORBIT
        self._action_taken = True

    def leave_orbit(self):
        assert not self._action_taken
        assert self.state == ShipState.IN_ORBIT

        self.state = ShipState.IN_SYSTEM
        self.planet = None
        self._action_taken = true

    def enter_orbit(self, planet:Planet):
        assert not self._action_taken
        assert self.state == ShipState.IN_SYSTEM
        assert planet in self.starsystem.planets

        self.planet = planet
        self.state = ShipState.IN_ORBIT
        self._action_taken = True

    def start_ftl(self, system:StarSystem):
        assert not self._action_taken
        assert self.state == ShipState.IN_SYSTEM
        assert self.starsystem.is_neighbor_to(system)

        self.state = ShipState.INTERSTELLAR
        self.ftl_destination = system
        self.starsystem = None
        self._action_taken = True

    def land(self):
        assert not self._action_taken
        assert self.state == ShipState.IN_ORBIT

        self.state == ShipState.LANDED
        self._action_taken = True

    def work_for_government(self):
        assert not self._action_taken
        assert self.state == "LANDED"

        self.money += 10
        self._action_taken = True

    # This Market code has nearly duplicated from person.py.  This could be 
    # deduplicated, but it's not clear to me that the pattern is going to be 
    # similar enough between the two instances.
    def place_buy_order(
        self, good: GoodKind, offer_price: float, quantity: int, callback: OrderCallback
    ) -> BuyOrder:
        assert self.planet is not None

        market = self.planet.markets[good]
        order = market.place_buy_order(offer_price, quantity, lambda o, f: self._order_callback(good, o, f, callback))
        self.buy_orders[good][order.order_number] = order
        if market not in self._affected_markets:
            self._affected_markets.append(market)
        return order

    def place_sell_order(
        self, good: GoodKind, offer_price: float, quantity: int, callback: OrderCallback
    ) -> SellOrder:
        assert self.planet is not None

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
        while len(self._affected_markets) > 0:
            market = self._affected_markets.pop()
            market.execute_orders()

        if self.state == ShipState.INTERSTELLAR:
            dest = self.ftl_destination
            distance_to_target = dest.distanceBetween(self)
            if distance <= FTL_SPEED:
                self.state = ShipState.IN_SYSTEM
                self.starsystem = dest
                self.ftl_destination = None
                self.x = self.starsystem.x
                self.y = self.starsystem.y
            else:
                direction = atan2(dest.y - self.y, dest.x - self.x)
                dy = cos(direction)
                dx = sin(direction)
                self.x += dx
                self.y += dy

        self._action_taken = False

    def _order_callback(self, good: GoodKind, order: Order, filled: int, callback: OrderCallback):
        if isinstance(order, BuyOrder):
            self.money -= order.offer_price * filled
            self.cargo[good] += filled
            if order.is_filled():
                del self.buy_orders[good][order.order_number]

        elif isinstance(order, SellOrder):
            self.money += order.offer_price * filled
            self.cargo[good] -= filled
            if order.is_filled():
                del self.sell_orders[good][order.order_number]

        callback(order, filled)

def generate_ship():
    return Ship(uuid4())