from good import BagOfGoods, GoodKind
from order_matching_market import Market
from person_actor import Person
from planet import Planet
from ship import Ship
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

def market_to_json(good: GoodKind, market: Market):
    volume = market.last_session_volume()
    result = {
        "good": good.name,
        "open": market.last_session_open(),
        "close": market.last_session_close(),
        "volume": volume,
        "avgVolume": market.get_30d_avg_volume()
    }

    if volume > 0:
        result["high"] = market.last_session_high()
        result["low"] = market.last_session_low()

    if market.has_buy_orders():
        result["bestBuyOffer"] = market.highest_buy_offer()

    if market.has_sell_orders():
        result["bestSellOffer"] = market.lowest_sell_offer()

    return result


def planet_to_json(planet: Planet):
    return {
        "id": str(planet.uuid),
        "population": list(map(person_to_json, planet.people.values())),
        "markets": list(map(lambda i: market_to_json(i[0],i[1]), planet.markets.items()))
    }


def starsystem_to_json(system: StarSystem):
    return {
        "id": str(system.uuid),
        "planets": list(map(planet_to_json, system.planets)),
        "neighbors": [str(n) for n in system._neighbors.keys()],
        "x": system.x,
        "y": system.y,
    }

def ship_to_json(ship: Ship):
    result = {
        "id": str(ship.uuid),
        "x": ship.x,
        "y": ship.y,
        "state": ship.state,
        "money": ship.money,
        "cargo": bag_of_goods_to_json(ship.cargo)
    }

    if ship.starsystem is not None:
        result["starsystem"] = str(ship.starsystem.uuid)

    if ship.planet is not None:
        result["planet"] = str(ship.planet.uuid)
    
    return result


def universe_to_json(universe: Universe):
    return {
        "starsystems": list(map(starsystem_to_json, universe.systems)),
        "ships": list(map(ship_to_json, universe.ships))
    }
