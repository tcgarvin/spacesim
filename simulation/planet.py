from typing import Iterable
from uuid import uuid4, UUID

from good import GoodKind, Recipe, basic_food_recipe
from order_matching_market import Market
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

    def __init__(self, uuid: UUID):
        self.uuid = uuid
        self.people = {}
        self.people_actors = {}
        self.markets = {}
        self.recipes = set()

    def add_person(self, person: Person, actor: PersonActor):
        self.people[person.uuid] = person
        self.people_actors[person.uuid] = actor

    def add_recipe(self, recipe: Recipe):
        self.recipes.add(recipe)

    def knows_recipe(self, recipe: Recipe):
        return recipe in self.recipes

    def get_market(self, good: GoodKind):
        market = self.markets.get(good, None)
        if market is None:
            market = Market()
            self.markets[good] = market

        return market

    def tick(self):
        # TODO: Scramble turn order
        to_prune = []
        for person_id, actor in self.people_actors.items():
            actor.tick(self)
            if self.people[person_id].is_dead():
                to_prune.append(person_id)

        for person_id in to_prune:
            del self.people[person_id]
            del self.people_actors[person_id]

        for market in self.markets.values():
            market.tick()


def generate_planet():
    result = Planet(uuid4())
    for i in range(100):
        person = generate_person()
        person_actor = PersonActor(person)
        result.add_person(person, person_actor)
        result.add_recipe(basic_food_recipe)
    return result
