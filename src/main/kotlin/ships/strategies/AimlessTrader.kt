package ships.strategies

import GoodKind
import Planet
import markets.BuyGood
import markets.SellGood
import ships.Ship
import ships.actions.EnterStarLane
import ships.actions.LandOnPlanet
import ships.actions.TakeOff
import ships.actions.WorkForGovernment
import kotlin.math.max


class AimlessTrader : ShipStrategy {
    private var mode = Mode.BUY
    override fun pickNextActions(ship: Ship): ShipStrategyOutput {
        return when (mode) {
            Mode.BUY -> pickBuyActions(ship)
            Mode.SELL -> pickSellActions(ship)
            Mode.MOVE -> pickMoveActions(ship)
        }
    }

    private fun pickBuyActions(ship: Ship) : ShipStrategyOutput {
        val goodsInHold = ship.goods.keys.filter { ship.goods[it] > 0 }
        if (goodsInHold.size > 1) {
            mode = Mode.SELL
            return pickNextActions(ship)
        }

        val planet = ship.location.getPlanet()

        val targetGood : GoodKind
        if (goodsInHold.isNotEmpty()) {
            targetGood = goodsInHold.first()
        } else {
            val availableGoods = getAvailableGoods(planet)
            if (availableGoods.isNotEmpty()) {
                targetGood = availableGoods.random()
            } else {
                mode = Mode.MOVE
                return pickNextActions(ship)
            }
        }

        val market = planet.getMarket(targetGood)
        if (!market.hasSellOrders()) {
            mode = Mode.MOVE
            return pickNextActions(ship)
        }

        val targetPrice = if (market.getBestSellOrder().price < market.get30DayAveragePrice()) {
            ((market.getBestSellOrder().price + market.get30DayAveragePrice()) / 2).toInt() + 1
        } else {
            market.getBestSellOrder().price
        }

        if (ship.money < targetPrice && ship.goods[targetGood] > 0) {
            mode = Mode.MOVE
            return pickNextActions(ship)
        }

        val marketOrders = if (ship.money > targetPrice) {
            listOf(BuyGood(targetGood, ship.money / targetPrice, targetPrice))
        } else {
            emptyList()
        }

        return ShipStrategyOutput(WorkForGovernment, marketOrders)
    }

    private fun getAvailableGoods(planet: Planet) : Collection<GoodKind> {
        return planet.getActiveMarkets()
            .filterValues { it.hasBuyOrders() }
            .keys
    }

    private fun pickSellActions(ship: Ship) : ShipStrategyOutput {
        val planet = ship.location.getPlanet()
        val goodsInHold = ship.goods.filterValues { it > 0 }.keys

        if (goodsInHold.size == 0) {
            mode = Mode.BUY
            return pickNextActions(ship)
        }

        val marketActions = goodsInHold.map {
            val market = planet.getMarket(it)
            val avgPrice = market.get30DayAveragePrice()
            val bestBuyPrice:Int = if (market.hasBuyOrders()) market.getBestBuyOrder().price else avgPrice.toInt()
            val targetPrice = if (bestBuyPrice > avgPrice) {
                ((bestBuyPrice + avgPrice) / 2).toInt()
            } else {
                bestBuyPrice
            }

            SellGood(it, ship.goods[it], max(1, targetPrice))
        }

        return ShipStrategyOutput(WorkForGovernment, marketActions)
    }

    private fun pickMoveActions(ship: Ship) : ShipStrategyOutput {
        val action = when {
            ship.location.isOnPlanet() -> {
                TakeOff()
            }
            Math.random() > 0.5 -> {
                mode = Mode.SELL
                LandOnPlanet(ship.location.neighbors
                    .filter { it.isOnPlanet() }
                    .random()
                    .getPlanet())
            }
            else -> {
                EnterStarLane(ship.location.neighbors
                    .filter { it.isInSystemSpace() }
                    .random()
                    .getStarSystem())
            }
        }

        return ShipStrategyOutput(action, emptyList())
    }

    private enum class Mode { BUY, MOVE, SELL }
}