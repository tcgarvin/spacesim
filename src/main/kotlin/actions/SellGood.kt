package actions

import GoodKind
import Person
import markets.Order

class SellGood(val good : GoodKind, val units : Int, val price : Int) : MarketAction {
    var order : Order? = null;
    private var goodsInEscrow = 0

    init {
        if (price <= 0) {
            throw IllegalArgumentException("Price cannot be less than 1")
        }
        if (units <= 0) {
            throw IllegalArgumentException("Cannot issue order for less than 1 unit")
        }
    }

    private fun returnEscrow(person:Person) {
        person.goods[good] += goodsInEscrow
        goodsInEscrow = 0
    }

    override fun apply(person: Person) {
        val market = person.planet.getMarket(good)

        goodsInEscrow = units
        person.goods[good] -= units

        order = market.issueSellOrder(units, price) { callbackOrder, unitsFilled, strikePrice ->
            if (goodsInEscrow < unitsFilled) {
                throw EscrowExhausted()
            }

            person.addMoney(strikePrice * unitsFilled)
            goodsInEscrow -= unitsFilled

            if (callbackOrder.isFilled()) {
                returnEscrow(person)
            }
        }
    }

    override fun cancel(person:Person) {
        if (order != null) {
            person.planet.getMarket(good).cancelOrder(order!!)
            returnEscrow(person)
        }
    }

    class EscrowExhausted : Exception()
}