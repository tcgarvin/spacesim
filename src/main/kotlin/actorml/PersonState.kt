package actorml

import Person
import burlap.mdp.core.state.State
import java.lang.IllegalArgumentException

@OptIn(ExperimentalStdlibApi::class)
class PersonState : State {
    private val variables : List<Int>

    constructor(person: Person) {
        // The idea here (I think) is to enumerate everything that makes this state interesting.  We also need this to be
        // roughly immutable, or at least not change with the environment
        variables = buildList {
            val markets = person.planet.getActiveMarkets()

            add(person.money)
            for (good in GoodKind.values()) {
                add(person.goods[good])
                if (good in markets) {
                    val market = markets.getValue(good)
                    add(market.get30DayAveragePrice().toInt())
                    add(market.get30DayAverageVolume().toInt())
                    add(market.getBestSellOrder().price)
                    add(market.getBestBuyOrder().price)
                }  else {
                    add(0)
                    add(0)
                    add(0)
                    add(0)
                }
            }

            add((person.needs.foodNeed.getScore(person) * 100).toInt())
            add((person.needs.shelterNeed.getScore(person) * 100).toInt())
        }
    }

    private constructor(variables : List<Int>) {
       this.variables = variables
    }

    override fun variableKeys(): MutableList<Any> {
        return mutableListOf(variables.indices)
    }

    override fun get(variableKey: Any?): Any {
        return when (variableKey) {
            is Int -> variables[variableKey]
            else -> throw IllegalArgumentException("PersonState has only Ints for keys")
        }
    }

    override fun copy(): State {
        return PersonState(variables)
    }
}