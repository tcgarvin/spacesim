from good import BagOfGoods
from person_actor import Person
from planet import Planet
from star_system import StarSystem
from universe import Universe


def bag_of_goods_to_json(bag: BagOfGoods):
    return {g.name: int(bag[g]) for g in bag.keys() if bag[g] > 0}


def person_to_json(person: Person):
    return {
        "id": str(person.uuid),
        "needs": {
            need.name: round(float(need.get_score(person)),2)
            for need in person.needs.get_needs()
        },
        "goods": bag_of_goods_to_json(person.goods),
        "money": person.money
    }

def market_to_json(name, market):
    return {
        "good": name,
        "open": market.last_session_open(),
        "close": market.last_session_close(),
        "volume": market.last_session_volume(),
        "high": market.last_session_high(),
        "low": market.last_session_low()
    }


def planet_to_json(planet: Planet):
    return {
        "id": str(planet.uuid),
        "population": list(map(person_to_json, planet.people.values())),
        "markets": list(map(market_to_json, planet.markets.items()))
    }


def starsystem_to_json(system: StarSystem):
    return {
        "id": str(system.id),
        "planets": list(map(planet_to_json, system.planets)),
        "neighbors": [str(n) for n in system._neighbors.keys()],
        "x": system.x,
        "y": system.y,
    }


def universe_to_json(universe: Universe):
    return {"starsystems": list(map(starsystem_to_json, universe.systems))}
