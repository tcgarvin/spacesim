package markets

import GoodKind
import actions.ActionError

/**
 * This is the action a market participant takes to issue a buy order
 */
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

    private fun returnEscrow(participant: MarketParticipant) {
        participant.addMoney(escrow)
        escrow = 0
    }

    override fun apply(participant: MarketParticipant) {
        val market = participant.location.getPlanet().getMarket(good)

        escrow = price * units
        try {
            participant.removeMoney(price * units)
        } catch (e: NoNegativeMoney) {
            throw ActionError("Not enough money available to place the given buy order")
        }

        order = market.issueBuyOrder(units, price) { callbackOrder, unitsFilled, strikePrice ->
            if (strikePrice * unitsFilled > escrow) {
                throw EscrowExhausted()
            }
            escrow -= strikePrice * unitsFilled
            participant.goods[good] += unitsFilled

            if (callbackOrder.isFilled()) {
                returnEscrow(participant)
            }
        }
    }

    override fun cancel(participant: MarketParticipant) {
        if (order != null) {
            participant.location.getPlanet().getMarket(good).cancelOrder(order!!)
            returnEscrow(participant)
        }
    }

    class EscrowExhausted : Exception()
}