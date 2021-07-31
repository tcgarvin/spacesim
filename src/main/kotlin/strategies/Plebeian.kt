package strategies

import Person
import actions.*
import markets.BuyGood
import markets.MarketAction
import markets.NoOpMarketAction
import markets.SellGood

val randomGoodList = listOf(GoodKind.FOOD, GoodKind.WOOD);

class Plebeian : PersonStrategy {
    private fun makeRandomBuyAction(person: Person) : MarketAction {
        val targetGood = randomGoodList.random();
        val targetMarket = person.planet.getMarket(targetGood);
        if (!targetMarket.hasSellOrders()) {
            return NoOpMarketAction
        }
        val targetPrice = targetMarket.getBestSellOrder().price;
        val maxPossibleUnits = person.money / targetPrice
        if (maxPossibleUnits == 0) {
            return NoOpMarketAction;
        }

        val targetUnits = person.biases.rng.nextInt(maxPossibleUnits) + 1
        return BuyGood(targetGood, targetUnits, targetPrice)
    }

    private fun makeRandomSellAction(person: Person) : MarketAction {
        val targetGood = randomGoodList.random();
        val targetMarket = person.planet.getMarket(targetGood);
        if (! targetMarket.hasBuyOrders()) {
            return NoOpMarketAction;
        }
        val targetPrice = targetMarket.getBestBuyOrder().price;
        val maxPossibleUnits = person.goods[targetGood]
        if (maxPossibleUnits == 0) {
            return NoOpMarketAction;
        }

        val targetUnits = person.biases.rng.nextInt(maxPossibleUnits) + 1
        return SellGood(targetGood, targetUnits, targetPrice)
    }

    override fun pickNextActions(person: Person, lastError: ActionError) : PersonStrategyOutput  {
        val rng = person.biases.rng;

        val marketAction = when (rng.nextInt(3)) {
            1 -> makeRandomBuyAction(person);
            2 -> makeRandomSellAction(person);
            else -> NoOpMarketAction
        }

        val personAction = when (rng.nextInt(3)) {
            1 -> MakeGood.WOOD;
            2 -> MakeGood.FOOD;
            else -> WorkForGovernment
        }

        return PersonStrategyOutput(personAction, listOf(marketAction))
    }
}