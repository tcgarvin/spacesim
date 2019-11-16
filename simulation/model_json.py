from good import BagOfGoods
from person_actor import Person
from planet import Planet
from star_system import StarSystem
from universe import Universe

def bag_of_goods_to_json(bag : BagOfGoods):
    return {g.name: bag[g] for g in bag.keys() if bag[g] > 0}

def person_to_json(person : Person):
    return {
        "id": str(person.uuid),
        "needs": {need.name: need.get_fulfillment_score() for need in (person.needs.food, person.needs.shelter)},
        "goods": bag_of_goods_to_json(person.goods)
    }


def planet_to_json(planet : Planet):
    return {
        "id": str(planet.uuid),
        "population": list(map(person_to_json, planet.people.values()))
    }


def starsystem_to_json(system : StarSystem):
    return {
        "id": str(system.id),
        "planets": list(map(planet_to_json, system.planets)),
        "neighbors": [str(n) for n in system._neighbors.keys()],
        "x": system.x,
        "y": system.y
    }

def universe_to_json(universe: Universe):
    return {
        "starsystems": list(map(starsystem_to_json, universe.systems))
    }