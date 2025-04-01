from typing import Iterable
from random import shuffle
from uuid import uuid4, UUID

from good import GoodKind, Recipe, basic_food_recipe, basic_wood_recipe
from order_matching_market import Market
from person import Person, generate_person
from person_actor import PersonActor, Plebeian, MarketMaker


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

    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.people = {}
        self.people_actors = {}
        self.markets = {}
        self.recipes = set()

    def add_person(self, person: Person, actor: PersonActor):
        self.people[person.uuid] = person
        self.people_actors[person.uuid] = actor
        person.set_planet(self)

    def add_recipe(self, recipe: Recipe):
        self.recipes.add(recipe)

    def knows_recipe(self, recipe: Recipe) -> bool:
        return recipe in self.recipes

    def get_market(self, good: GoodKind) -> Market:
        market = self.markets.get(good, None)
        if market is None:
            market = Market()
            self.markets[good] = market

        return market

    def tick(self):
        # TODO: Scramble turn order
        actor_turn_order = list(self.people_actors.values())
        shuffle(actor_turn_order)
        for actor in actor_turn_order:
            actor.tick()
            actor.person.tick()

        for market in self.markets.values():
            market.tick()


def generate_planet():
    result = Planet(uuid4())
    result.add_recipe(basic_food_recipe)
    result.add_recipe(basic_wood_recipe)
    strategies = [Plebeian] * 98 + [MarketMaker] * 2
    for Strategy in strategies:
        person = generate_person(result)
        person_actor = Strategy(person)
        result.add_person(person, person_actor)
    return result
