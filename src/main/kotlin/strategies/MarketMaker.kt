package strategies

import GoodKind
import Person
import actions.*
import org.apache.commons.math3.distribution.NormalDistribution
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min

const val MAX_SELL_ORDERS = 10
const val MAX_BUY_ORDERS = 10

class MarketMaker : PersonStrategy {
    private val allocations = mutableMapOf<GoodKind, Int>().withDefault { 0 }

    private fun ordersViaMarketConditions(person: Person, good: GoodKind): Collection<MarketAction> {
        val market = person.planet.getMarket(good)
        val amountHeld = person.goods[good];
        val amountAllocated = allocations.getOrDefault(good, 0)

        // Constant: Market makers use 30 day moving averages
        // TODO: Parameterize
        val averageVolume = market.get30DayAverageVolume();
        val averagePrice = market.get30DayAveragePrice();
        val priceSigma = max(market.get30DayStandardDeviation(), 1.0)

        // Constant: market makers aim to keep 30 days of inventory + 1
        // TODO: Parameterize
        val targetInventory = averageVolume * 30 + 1
        val currentInventoryAsPercentOfTarget = amountHeld / targetInventory

        // Determine a spread based on the average price and how much
        // inventory we have vs our target. Consider a gausian curve in
        // which x is the price point and f(x) is the portion of inventory
        // presented at that price point. As inventory grows from 0, we fill
        // the curve from the right side, until, when inventory is exactly
        // at target, we have filled half the curve. If inventory exceeds
        // our target, we extend into lower prices on the left side of the
        // graph.

        // The buy side uses the unfilled left side of the curve, such that
        // when we are not at target inventory, we are willing to pay over
        // the average price, and when we are over target, we are not
        // willing to page the average price.

        // Between the buy and sell side, seeking to be at our target
        // inventory should allow us to apply price pressure to the market

        val priceDist = NormalDistribution(averagePrice, priceSigma);
        val curvePercentile = max(0.01, min(0.99, 1 - (0.5 * currentInventoryAsPercentOfTarget)));
        var targetSellPrice = ceil(priceDist.inverseCumulativeProbability(curvePercentile)).toInt()
        val targetBuyPrice = floor(priceDist.inverseCumulativeProbability(curvePercentile)).toInt()
        if (targetBuyPrice == targetSellPrice) {
            targetSellPrice += 1
        }

        val marketActions = mutableListOf<MarketAction>()
        if (amountHeld > 0) {
            val curvePercentileStep = (1 - curvePercentile) / MAX_SELL_ORDERS
            val sellPercentiles = (0 until MAX_SELL_ORDERS).map { curvePercentile + (it * curvePercentileStep) }
            val orderPrices = sellPercentiles.map { max(priceDist.inverseCumulativeProbability(it).toInt() + 1, 2) }
            val targetOrderSize = ceil(amountHeld / MAX_SELL_ORDERS.toDouble()).toInt()

            // Combine orders that are of the same price
            val orderCountByPrice = orderPrices.groupingBy { it }.eachCount()

            var amountRemaining = amountHeld
            for ((price, orderCount) in orderCountByPrice) {
                if (amountRemaining <= 0) {
                    break
                }

                val orderSize = min(amountRemaining, targetOrderSize * orderCount)
                amountRemaining -= orderSize
                marketActions.add(SellGood(good, orderSize, price))
            }
        }

        if (amountAllocated > 0) {
            val curvePercentileStep = curvePercentile / MAX_BUY_ORDERS
            val buyPercentiles = (0 until MAX_BUY_ORDERS).map { curvePercentile - (it * curvePercentileStep) }
            val orderPrices = buyPercentiles.map { max(priceDist.inverseCumulativeProbability(it).toInt(), 1) }
            val targetOrderSize = ceil((amountAllocated / targetBuyPrice.toDouble())).toInt()

            // Combine orders that are of the same price
            val orderCountByPrice = orderPrices.groupingBy { it }.eachCount()

            var amountRemaining = amountAllocated
            for ((price, orderCount) in orderCountByPrice) {
                if (amountRemaining <= 0) {
                    break
                }

                val orderSize = min(amountRemaining / price, targetOrderSize * orderCount)
                if (orderSize > 0) {
                    amountRemaining -= orderSize * price
                    marketActions.add(BuyGood(good, orderSize, price))
                }
            }
        }

        return marketActions
    }

    private fun bootstrappingOrders(person: Person, good: GoodKind) : Collection<MarketAction> {
        val amountAllocated = allocations.getOrDefault(good, 0)
        val marketActions = mutableListOf<MarketAction>()
        for (i in mutableListOf(1,2,4)) {
            if (amountAllocated / i > 0) {
                marketActions.add(BuyGood(good, 1, amountAllocated / i))
            }
        }
        return marketActions
    }

    private fun affectMarket(person: Person, good: GoodKind) : Collection<MarketAction> {
        val market = person.planet.getMarket(good);

        return when (market.hasHistory()) {
            true -> ordersViaMarketConditions(person, good)
            else -> bootstrappingOrders(person, good)
        }
    }

    private fun allocateFunds(person: Person) {
        val fundsToAllocate = (person.money - allocations.values.sum()) / getGoodsOfConcern().size
        for (good in getGoodsOfConcern()) {
            allocations[good] = allocations.getValue(good) + fundsToAllocate
        }
    }

    private fun getGoodsOfConcern() : Array<GoodKind> {
        return GoodKind.values()
    }

    override fun pickNextActions(person: Person) : PersonStrategyOutput  {
        allocateFunds(person)
        val marketActions = mutableListOf<MarketAction>();
        for (good in getGoodsOfConcern()) {
            marketActions.addAll(affectMarket(person, good))
        }

        return PersonStrategyOutput(WorkForGovernment, marketActions)
    }
}