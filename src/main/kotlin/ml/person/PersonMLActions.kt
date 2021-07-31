package ml.person

import Person
import actions.MakeGood
import actions.WorkForGovernment
import markets.BuyGood
import markets.MarketAction
import markets.SellGood
import strategies.PersonMLAction
import strategies.PersonStrategyOutput
import kotlin.math.max
import kotlin.math.min

@OptIn(ExperimentalStdlibApi::class)
private val MARKET_ACTION_BUILDERS = buildList<(Person) -> MarketAction> {
    for (good in GoodKind.values()) {
        add {
            val market = it.planet.getMarket(good)
            val price = if (market.hasSellOrders()) market.getBestSellOrder().price else 1
            BuyGood(good, 1, price)
        }
        add {
            val market = it.planet.getMarket(good)
            val price = if (market.hasSellOrders()) market.getBestSellOrder().price else 1
            BuyGood( good, max(1, min(5, it.money / price)), price )
        }
        add {
            val market = it.planet.getMarket(good)
            val price = if (market.hasBuyOrders()) market.getBestBuyOrder().price else 1
            SellGood(good, 1, price)
        }
        add {
            val market = it.planet.getMarket(good)
            val price = if (market.hasBuyOrders()) market.getBestBuyOrder().price else 1
            SellGood(good, max(1, it.goods[good] / 2), price)
        }
    }
}

private val PERSON_ACTIONS = listOf(WorkForGovernment, MakeGood.FOOD, MakeGood.WOOD)

@OptIn(ExperimentalStdlibApi::class)
val PERSON_ML_ACTIONS = buildList<PersonMLAction> {
    for (personAction in PERSON_ACTIONS) {
        add { PersonStrategyOutput(personAction, listOf())}
        for (marketActionBuilder in MARKET_ACTION_BUILDERS) {
            add { PersonStrategyOutput(personAction, listOf(marketActionBuilder(it))) }
        }
    }
}