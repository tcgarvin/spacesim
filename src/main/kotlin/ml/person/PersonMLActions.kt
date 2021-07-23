package ml.person

import Person
import actions.MakeGood
import actions.PersonAction
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
        add { BuyGood(good, 1, it.planet.getMarket(good).getBestSellOrder().price) }
        add {
            BuyGood(
                good,
                max(1, min(5, it.money / it.planet.getMarket(good).getBestSellOrder().price)),
                it.planet.getMarket(good).getBestSellOrder().price
            )
        }
        add { SellGood(good, 1, it.planet.getMarket(good).getBestBuyOrder().price) }
        add { SellGood(good, max(1, it.goods[good] / 2), it.planet.getMarket(good).getBestBuyOrder().price) }
    }
}

@OptIn(ExperimentalStdlibApi::class)
private val PERSON_ACTIONS = buildList<PersonAction> {
    WorkForGovernment
    MakeGood.FOOD
    MakeGood.WOOD
}

@OptIn(ExperimentalStdlibApi::class)
val PERSON_ML_ACTIONS = buildList<PersonMLAction> {
    for (personAction in PERSON_ACTIONS) {
        add { PersonStrategyOutput(personAction, listOf())}
        for (marketActionBuilder in MARKET_ACTION_BUILDERS) {
            add { PersonStrategyOutput(personAction, listOf(marketActionBuilder(it))) }
        }
    }
}