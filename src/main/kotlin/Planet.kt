import markets.CommodityMarket
import org.apache.commons.math3.random.RandomGenerator

class Planet(val biases: Tumbler) : Tickable {
    val people: MutableList<Person> = mutableListOf()
    private val markets: MutableMap<GoodKind, CommodityMarket> = mutableMapOf()

    override fun tick() {
        people.forEach { it.tick() }
    }

    fun addPerson(person: Person) {
        people.add(person)
    }

    fun getMarket(commodity: GoodKind) : CommodityMarket{
        return markets.getOrPut(commodity) { CommodityMarket() }
    }
}

fun generatePlanet(rng: RandomGenerator): Planet {
    val result = Planet(Tumbler(rng))
    (1..100).forEach { result.addPerson(generatePerson(result, rng)) }
    return result
}