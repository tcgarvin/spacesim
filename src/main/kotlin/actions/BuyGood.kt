package actions

import GoodKind
import Person
import markets.Order

class BuyGood(val good : GoodKind, val units : Int, val price : Int) : MarketAction {
    var order : Order? = null
    var escrow = 0

    init {
        if (price <= 0) {
            throw IllegalArgumentException("Price cannot be less than 1")
        }
        if (units <= 0) {
            throw IllegalArgumentException("Cannot issue order for less than 1 unit")
        }
    }

    private fun returnEscrow(person : Person) {
        person.addMoney(escrow)
        escrow = 0
    }

    override fun apply(person: Person) {
        val market = person.planet.getMarket(good)

        escrow = price * units
        person.removeMoney(price * units)

        order = market.issueBuyOrder(units, price) { callbackOrder, unitsFilled, strikePrice ->
            if (strikePrice * unitsFilled > escrow) {
                throw EscrowExhausted()
            }
            escrow -= strikePrice * unitsFilled
            person.goods[good] += unitsFilled

            if (callbackOrder.isFilled()) {
                returnEscrow(person)
            }
        }
    }

    override fun cancel(person: Person) {
        if (order != null) {
            person.planet.getMarket(good).cancelOrder(order!!)
            returnEscrow(person)
        }
    }

    class EscrowExhausted : Exception()
}