import actions.PersonAction
import needs.NeedsHierarchy
import org.apache.commons.math3.random.RandomGenerator
import strategies.PersonStrategy
import strategies.Plebeian

class Person(val needs: NeedsHierarchy, val strategy: PersonStrategy, val planet: Planet, val biases: Tumbler) : Tickable {
    val goods = BagOfGoods()
    val partialGoods = mutableMapOf<GoodKind, Double>().withDefault { 0.0 }

    override fun tick() {
        val nextAction: PersonAction = strategy.pickNextAction(this)
        nextAction.apply(this)
        needs.visit(this)
    }

    fun getScore(): Double = needs.getScore(this)
}

fun generatePerson(planet:Planet, rng: RandomGenerator): Person {
    return Person(NeedsHierarchy(), Plebeian(), planet, Tumbler(rng))
}