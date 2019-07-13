from typing import Iterable
from good import GoodKind
from supply_curve_market import Market
from person import Person, generate_person
from person_actor import PersonActor

class Planet:
    """
    A representation of planet and it's simple economy

    A planet can have:
        people:
            A person has a set of actions that they can take:
                Produce goods by hand by following a recipe
                Consume consumer goods
                Build a factory out of goods
                Work in a factory
                buy goods at market
                sell goods at market
        factories:
            a factory converts goods inputs  into outputs according to a recipe
            in order for a factory to work it must have raw goods and people who want to work at it.
                ? How will wages be handled            
        market:
            In this fictional economy all buyers and sellers can find each other and anounce their prices immediately.
            There's no theft, or other distortions and goods are transported instantly for no cost.
    """
    def __init__(self):
        self.people = []
        self.people_actors = []
        self.markets = {}
        self.recipies = []
    
    def add_person(self, person : Person):
        self.people.append(person)

    def add_person_actor(self, actor : PersonActor):
        self.people_actors.append(actor)

    def get_market(self, good : GoodKind):
        market = self.markets.get(good, None)
        if market is None:
            market = Market(good, 10, 1, 0)


    def tick(self):
        # TODO: Scramble turn order
        for actor in self.people_actors:
            actor.tick(self)

        for market in self.markets.values():
            market.tick()


def generate_planet():
    result = Planet()
    for i in range(100):
        person = generate_person()
        person_actor = PersonActor(person)
        result.add_person(person)
        result.add_person_actor(person_actor)
    return result