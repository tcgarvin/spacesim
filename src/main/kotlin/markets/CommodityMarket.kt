package markets

import kotlin.math.min

abstract class Order {
    abstract val id: Int
    abstract val units: Int
    abstract val price: Int

    var unitsFilled = 0

    open fun fill(numUnits: Int) {
        unitsFilled += numUnits
    }

    fun isFilled(): Boolean {
        return units == unitsFilled
    }
}

class BuyOrder(
    override val id: Int,
    override val units: Int,
    override val price: Int,
    val callback: (BuyOrder) -> Unit
) : Order() {
    override fun fill(numUnits: Int) {
        super.fill(numUnits)
        callback(this)
    }
}

class SellOrder(
    override val id: Int,
    override val units: Int,
    override val price: Int,
    val callback: (SellOrder) -> Unit
) : Order() {
    override fun fill(numUnits: Int) {
        super.fill(numUnits)
        callback(this)
    }
}

private val buyOrderComparator: Comparator<BuyOrder> = compareBy(
    { -it.price },
    { it.id }
)

private val sellOrderComparator: Comparator<SellOrder> = compareBy(
    { it.price },
    { it.id }
)

class CommodityMarket() {
    private val buyOrders = sortedSetOf(buyOrderComparator)
    private val sellOrders = sortedSetOf(sellOrderComparator)
    private var lastOrderId = 0

    fun hasBuyOrders(): Boolean {
        return buyOrders.size > 0
    }

    fun hasSellOrders(): Boolean {
        return sellOrders.size > 0
    }

    fun getBestBuyOrder(): Order {
        return buyOrders.first() ?: throw NoOrders()
    }

    fun getBestSellOrder(): Order {
        return sellOrders.first() ?: throw NoOrders()
    }

    fun hasMatchingOrders(): Boolean {
        if (!hasBuyOrders() || !hasSellOrders()) {
            return false
        }

        return getBestBuyOrder().price >= getBestSellOrder().price
    }

    private fun getNextOrderID() : Int {
        lastOrderId += 1
        return lastOrderId
    }

    fun issueBuyOrder(units : Int, price : Int, callback: (BuyOrder) -> Unit = {}) : BuyOrder {
        if (hasSellOrders() && getBestSellOrder().price < price) {
            throw NoNegativeMargin()
        }
        val order = BuyOrder(getNextOrderID(), units, price, callback)
        buyOrders.add(order)
        return order
    }

    fun issueSellOrder(units : Int, price : Int, callback : (SellOrder) -> Unit = {}) : SellOrder {
        if (hasBuyOrders() && getBestBuyOrder().price > price) {
            throw NoNegativeMargin()
        }
        val order = SellOrder(getNextOrderID(), units, price, callback)
        sellOrders.add(order)
        return order
    }

    fun run() {
        while (hasMatchingOrders()) {
            val bestBuyOrder = getBestBuyOrder()
            val bestSellOrder = getBestSellOrder()

            val unitsTransacted = min(bestBuyOrder.units, bestSellOrder.units)

            bestBuyOrder.fill(unitsTransacted)
            bestSellOrder.fill(unitsTransacted)

            if (bestBuyOrder.isFilled()) {
                buyOrders.remove(bestBuyOrder)
            }

            if (bestSellOrder.isFilled()) {
                sellOrders.remove(bestSellOrder)
            }
        }
    }

    class NoOrders() : Exception()
    class NoNegativeMargin() : Exception()
}