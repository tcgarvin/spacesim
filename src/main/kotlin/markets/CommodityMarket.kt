package markets

import kotlin.math.min

abstract class Order {
    abstract val id: Int
    abstract val units: Int
    abstract val price: Int

    var unitsFilled = 0

    open fun fill(numUnits: Int, strikePrice: Int) {
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
    val callback: (BuyOrder, Int, Int) -> Unit
) : Order() {
    override fun fill(numUnits: Int, strikePrice: Int) {
        super.fill(numUnits, strikePrice)
        callback(this, numUnits, strikePrice)
    }
}

class SellOrder(
    override val id: Int,
    override val units: Int,
    override val price: Int,
    val callback: (SellOrder, Int, Int) -> Unit
) : Order() {
    override fun fill(numUnits: Int, strikePrice: Int) {
        super.fill(numUnits, strikePrice)
        callback(this, numUnits, strikePrice)
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

class CommodityMarket {
    private val buyOrders = sortedSetOf(buyOrderComparator)
    private val sellOrders = sortedSetOf(sellOrderComparator)
    private var lastOrderId = 0

    fun hasBuyOrders(): Boolean {
        return buyOrders.size > 0
    }

    fun hasSellOrders(): Boolean {
        return sellOrders.size > 0
    }

    fun getBestBuyOrder(): BuyOrder {
        return buyOrders.first() ?: throw NoOrders()
    }

    fun getBestSellOrder(): SellOrder {
        return sellOrders.first() ?: throw NoOrders()
    }

    private fun hasMatchingOrders(): Boolean {
        if (!hasBuyOrders() || !hasSellOrders()) {
            return false
        }

        return getBestBuyOrder().price >= getBestSellOrder().price
    }

    fun cancelOrder(order : Order) {
        when (order) {
            is BuyOrder -> buyOrders.remove(order)
            is SellOrder -> sellOrders.remove(order)
        }
    }

    private fun getNextOrderID(): Int {
        lastOrderId += 1
        return lastOrderId
    }

    fun issueBuyOrder(units: Int, price: Int, callback: (BuyOrder, Int, Int) -> Unit = { _, _, _ -> }): BuyOrder {
        if (hasSellOrders() && getBestSellOrder().price < price) {
            throw NoNegativeMargin()
        }
        val order = BuyOrder(getNextOrderID(), units, price, callback)
        buyOrders.add(order)
        return order
    }

    fun issueSellOrder(units: Int, price: Int, callback: (SellOrder, Int, Int) -> Unit = { _, _, _ -> }): SellOrder {
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
            val strikePrice = if (bestBuyOrder.id > bestSellOrder.id) bestSellOrder.price else bestBuyOrder.price

            bestBuyOrder.fill(unitsTransacted, strikePrice)
            bestSellOrder.fill(unitsTransacted, strikePrice)

            if (bestBuyOrder.isFilled()) {
                buyOrders.remove(bestBuyOrder)
            }

            if (bestSellOrder.isFilled()) {
                sellOrders.remove(bestSellOrder)
            }
        }
    }

    class NoOrders : Exception()
    class NoNegativeMargin : Exception()
}