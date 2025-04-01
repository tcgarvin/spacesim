import actions.ActionError
import actions.NoError
import markets.MarketAction
import markets.MarketParticipant
import markets.NoNegativeMoney
import markets.NoOpMarketAction
import needs.NeedsHierarchy
import org.apache.commons.math3.random.RandomGenerator
import strategies.MarketMaker
import strategies.PersonStrategy
import strategies.Plebeian
import java.util.UUID

class Person(val needs: NeedsHierarchy, val strategy: PersonStrategy, val planet: Planet, val biases: Tumbler) : Tickable, MarketParticipant {
    val id = UUID.randomUUID()
    override val goods: BagOfGoods = BagOfGoods()
    override val location: Location
        get() = getLocationFactory().getPlanetLocation( planet )

    var money = 0
        private set

    val partialGoods = mutableMapOf<GoodKind, Double>().withDefault { 0.0 }

    private val pendingActions = mutableSetOf<MarketAction>()
    private var lastError : ActionError = NoError

    override fun tick() {
        pendingActions.forEach {it.cancel(this)}
        pendingActions.clear()
        val strategyOutput = strategy.pickNextActions(this, lastError)
        lastError = NoError
        getLogger().logPersonTurn(this, strategyOutput)
        try {
            strategyOutput.personAction.apply(this)
            for (action in strategyOutput.marketActions) {
                action.apply(this)
                if (action != NoOpMarketAction) {
                    pendingActions.add(action)
                }
            }
        } catch (e: ActionError) {
            lastError = e
        }
        needs.visit(this)
    }

    override fun addMoney(amount: Int) {
        if (money + amount < 0) {
            throw NoNegativeMoney()
        }

        money += amount
    }

    override fun removeMoney(amount: Int) {
        addMoney(-amount)
    }

    fun getScore(): Double = needs.getScore(this)
}

fun generatePerson(planet:Planet, rng: RandomGenerator): Person {
    return generatePerson(planet, rng, Plebeian())
}

fun generatePerson(planet: Planet, rng: RandomGenerator, personStrategy: PersonStrategy): Person {
    return Person(NeedsHierarchy(), personStrategy, planet, Tumbler(rng))
}

fun generateMarketMaker(planet: Planet, rng: RandomGenerator): Person {
    return Person(NeedsHierarchy(), MarketMaker(), planet, Tumbler(rng))
}