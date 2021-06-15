package markets

import BagOfGoods
import Location

class NoNegativeMoney : Exception()

interface MarketParticipant {
    val goods : BagOfGoods
    val location : Location

    fun addMoney(amount : Int)
    fun removeMoney(amount : Int)
}

interface MarketAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(participant: MarketParticipant)

    /**
     * Cancel this action
     */
    fun cancel(participant: MarketParticipant)
}

object NoOpMarketAction : MarketAction {
    override fun apply(participant: MarketParticipant) {}

    override fun cancel(participant: MarketParticipant) {}
}
