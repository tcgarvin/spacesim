package actions

import GoodKind
import Person
import markets.Order

class SellGood(val good : GoodKind, val units : Int, val price : Int) : MarketAction {
    var order : Order? = null;

    override fun apply(person: Person) {
        val market = person.planet.getMarket(good)
        order = market.issueBuyOrder(units, price) { _, unitsFilled, strikePrice ->
            person.money += strikePrice * unitsFilled
            person.goods[good] -= unitsFilled
        }
    }

    override fun cancel(person:Person) {
        if (order != null) {
            person.planet.getMarket(good).cancelOrder(order!!)
        }
    }
}