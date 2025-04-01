package markets

import org.apache.commons.math3.stat.descriptive.moment.StandardDeviation
import java.util.*
import kotlin.Comparator
import kotlin.math.min

class InvalidOrder(message:String) : Exception(message)

abstract class Order {
    abstract val id: Int
    abstract val units: Int
    abstract val price: Int

    var unitsFilled = 0

    open fun fill(numUnits: Int, strikePrice: Int) {
        unitsFilled += numUnits
    }

    fun unfilledUnits() : Int {
        return units - unitsFilled
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

    private var todaysVolume = 0
    private val volumeHistory = LinkedList<Int>()
    private var lastPrice = 0
    private val lastPriceHistory = LinkedList<Int>()

    fun hasBuyOrders(): Boolean {
        return buyOrders.size > 0
    }

    fun hasSellOrders(): Boolean {
        return sellOrders.size > 0
    }

    fun getBestBuyOrder(): BuyOrder {
        if (!hasBuyOrders()) {
            throw NoOrders()
        }
        return buyOrders.first()
    }

    fun getBestSellOrder(): SellOrder {
        if (!hasSellOrders()) {
            throw NoOrders()
        }
        return sellOrders.first()
    }

    fun hasHistory(): Boolean {
        return lastPriceHistory.size > 0
    }

    fun get30DayAveragePrice(): Double {
        return lastPriceHistory.average()
    }

    fun get30DayAverageVolume(): Double {
        return volumeHistory.average()
    }

    fun get30DayStandardDeviation(): Double {
        val sigma = StandardDeviation()
        for (price in lastPriceHistory) {
            sigma.increment(price.toDouble())
        }
        return sigma.result
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
        val order = BuyOrder(getNextOrderID(), units, price, callback)
        buyOrders.add(order)
        run()
        return order
    }

    fun issueSellOrder(units: Int, price: Int, callback: (SellOrder, Int, Int) -> Unit = { _, _, _ -> }): SellOrder {
        val order = SellOrder(getNextOrderID(), units, price, callback)
        sellOrders.add(order)
        run()
        return order
    }

    fun run() {
        while (hasMatchingOrders()) {
            val bestBuyOrder = getBestBuyOrder()
            val bestSellOrder = getBestSellOrder()

            val unitsTransacted = min(bestBuyOrder.unfilledUnits(), bestSellOrder.unfilledUnits())
            val strikePrice = if (bestBuyOrder.id > bestSellOrder.id) bestSellOrder.price else bestBuyOrder.price

            bestBuyOrder.fill(unitsTransacted, strikePrice)
            bestSellOrder.fill(unitsTransacted, strikePrice)

            if (bestBuyOrder.isFilled()) {
                buyOrders.remove(bestBuyOrder)
            }

            if (bestSellOrder.isFilled()) {
                sellOrders.remove(bestSellOrder)
            }

            lastPrice = strikePrice
            todaysVolume += unitsTransacted
        }
    }

    fun tick() {
        lastPriceHistory.addLast(lastPrice)
        while (lastPriceHistory.size > 30) {
            lastPriceHistory.removeFirst()
        }

        volumeHistory.addLast(todaysVolume)
        while (volumeHistory.size > 30) {
            volumeHistory.removeFirst()
        }
        todaysVolume = 0
    }

    class NoOrders : Exception()
}