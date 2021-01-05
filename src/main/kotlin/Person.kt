import actions.PersonAction
import needs.NeedsHierarchy
import strategies.PersonStrategy
import strategies.Plebian

class Person(val needs: NeedsHierarchy, val strategy: PersonStrategy, val planet: Planet) : Tickable {
    val goods = BagOfGoods()

    override fun tick() {
        val nextAction: PersonAction = strategy.pickNextAction(this)
        nextAction.apply(this)
        needs.visit(this)
    }

    fun getScore(): Double = needs.getScore(this)
}

fun generatePerson(planet:Planet): Person {
    return Person(NeedsHierarchy(), Plebian(), planet)
}