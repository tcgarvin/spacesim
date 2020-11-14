from abc import ABC, abstractmethod

from good import GoodKind, food, wood, BagOfGoods
from planet import Planet
from ship import Ship
from star_system import StarSystem

class PlanState:
    def __init__(self, money: int, goods: BagOfGoods, starsystem=None: StarSystem, planet=None: Planet, parent=None, time_cost=0:int, move_here=None):
        self.money = money
        self.goods = goods
        self.starsystem = starsystem
        self.planet = planet
        self.time_cost = time_cost
        self.parent = parent
        self.move_here = move_here

    def clone(self):


    def is_not_worse_than(self, other: PlanState):
        """
        This is mildly subtle. Should not be used in a mathmatically "greater
        than" sense, but rather in a "Is this state better in any single
        facet?" way. This should not be viewed as a commutative operator.
        """
        if self.money < other.money:
            return False

        if self.goods.keys() < other.goods.keys():
            return False

        for good in self.goods.keys() | other.goods.keys():
            if self.goods[good] < other.goods[good]:
                return False

        if self.money == other.money and 
           self.goods.equals(other.goods) and
           self.time_cost < other.time_cost:
            return False

        return True


class MoveCommand(ABC):
    @abstractmethod
    def get_cost(self):
        raise NotImplementedError()

    @abstractmethod
    def apply(self, ship: PlanState):
        raise NotImplementedError()

class LaunchCommand(MoveCommand):
    def apply(self, ship: PlanState):
        

class LandCommand(MoveCommand):
    def apply(self, ship: PlanState):
        ship.land()

class LeaveOrbitCommand(MoveCommand):
    def apply(self, ship: PlanState):
        ship.leave_orbit()

class EnterOrbitCommand(MoveCommand):
    def apply(self, ship: PlanState):
        ship.enter_orbit()

class StartFTLCommand(MoveCommand):
    def __init__(self, destination: StarSystem):
        self.destination = destination

    def apply(self, ship: PlanState):
        ship.start_ftl(self.destination)

class WorkForGovernmentCommand(MoveCommand):
    def apply(self, ship: PlanState):
        ship.work_for_government()

class SellCommand(MoveCommand):
    def __init__(self, good: GoodKind):
        ship.good = good

    def apply(self, ship: PlanState):
        # TODO
        pass

class BuyCommand(MoveCommand):
    def __init__(self, good: GoodKind);
        ship.good = good

    def apply(self, ship: PlanState):
        # TODO
        pass


class ShipActor:
    def __init__(self, ship: Ship):
        self.ship = ship

    def tick(self):

        pass