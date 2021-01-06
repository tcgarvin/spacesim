import actions.PersonAction
import needs.NeedsHierarchy
import org.apache.commons.math3.random.RandomGenerator
import strategies.PersonStrategy
import strategies.Plebeian

class Person(val needs: NeedsHierarchy, val strategy: PersonStrategy, val planet: Planet, private val rng: RandomGenerator) : Tickable {
    val goods = BagOfGoods()
    val biases = BiasMapping(rng)

    override fun tick() {
        val nextAction: PersonAction = strategy.pickNextAction(this)
        nextAction.apply(this)
        needs.visit(this)
    }

    fun getScore(): Double = needs.getScore(this)
}

fun generatePerson(planet:Planet, rng: RandomGenerator): Person {
    return Person(NeedsHierarchy(), Plebeian(), planet, rng)
}