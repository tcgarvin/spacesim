package markets

import GoodKind
import actions.ActionError

/**
 * This is the action a market participant takes to issue a sell order
 */
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

    private fun returnEscrow(participant: MarketParticipant) {
        participant.goods[good] += goodsInEscrow
        goodsInEscrow = 0
    }

    override fun apply(participant: MarketParticipant) {
        val market = participant.location.getPlanet().getMarket(good)

        goodsInEscrow = units
        try {
            participant.goods[good] -= units
        } catch (e:BagOfGoods.ValueException) {
            throw ActionError("You don't have enough of the relevant good to issue the given sell order")
        }

        order = market.issueSellOrder(units, price) { callbackOrder, unitsFilled, strikePrice ->
            if (goodsInEscrow < unitsFilled) {
                throw EscrowExhausted()
            }

            participant.addMoney(strikePrice * unitsFilled)
            goodsInEscrow -= unitsFilled

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