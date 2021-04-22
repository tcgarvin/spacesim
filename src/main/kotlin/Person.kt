import actions.MarketAction
import actions.NoOpMarketAction
import needs.NeedsHierarchy
import org.apache.commons.math3.random.RandomGenerator
import strategies.MarketMaker
import strategies.PersonStrategy
import strategies.Plebeian


class Person(val needs: NeedsHierarchy, val strategy: PersonStrategy, val planet: Planet, val biases: Tumbler) : Tickable {
    val goods = BagOfGoods()
    var money = 0
        private set

    val partialGoods = mutableMapOf<GoodKind, Double>().withDefault { 0.0 }

    private val pendingActions = mutableSetOf<MarketAction>()

    override fun tick() {
        pendingActions.forEach {it.cancel(this)}
        pendingActions.clear()

        val strategyOutput = strategy.pickNextActions(this)
        strategyOutput.personAction.apply(this)
        for (action in strategyOutput.marketActions) {
            action.apply(this)
            if (action != NoOpMarketAction) {
                pendingActions.add(action)
            }
        }
        needs.visit(this)
    }

    fun addMoney(amount: Int) {
        if (money + amount < 0) {
            throw NoNegativeMoney()
        }

        money += amount
    }

    fun removeMoney(amount: Int) {
        addMoney(-amount)
    }

    fun getScore(): Double = needs.getScore(this)

    class NoNegativeMoney : Exception()
}

fun generatePerson(planet:Planet, rng: RandomGenerator): Person {
    return Person(NeedsHierarchy(), Plebeian(), planet, Tumbler(rng))
}

fun generateMarketMaker(planet: Planet, rng: RandomGenerator): Person {
    return Person(NeedsHierarchy(), MarketMaker(), planet, Tumbler(rng))
}