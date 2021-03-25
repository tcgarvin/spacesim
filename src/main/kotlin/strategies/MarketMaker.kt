package strategies

import Person
import actions.*
import markets.CommodityMarket

class MarketMaker : PersonStrategy {
    private fun affectMarket(market: CommodityMarket) : Collection<MarketAction> {

    }

    override fun pickNextActions(person: Person) : PersonStrategyOutput  {
        val marketActions = mutableListOf<MarketAction>();
        for (good in GoodKind.values()) {
            val market = person.planet.getMarket(good);
            marketActions.addAll(affectMarket(market))
        }

        return PersonStrategyOutput(WorkForGovernment, marketActions)
    }
}