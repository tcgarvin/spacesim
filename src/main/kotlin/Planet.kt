import markets.CommodityMarket
import org.apache.commons.math3.random.RandomGenerator
import strategies.PersonStrategy

class Planet(val biases: Tumbler) : Tickable {
    val people: MutableList<Person> = mutableListOf()
    private val markets: MutableMap<GoodKind, CommodityMarket> = mutableMapOf()

    override fun tick() {
        people.forEach { it.tick() }
        markets.values.forEach { it.tick() }
    }

    fun addPerson(person: Person) {
        people.add(person)
    }

    fun getMarket(commodity: GoodKind) : CommodityMarket{
        return markets.getOrPut(commodity) { CommodityMarket() }
    }

    fun getActiveMarkets() : Map<GoodKind, CommodityMarket> {
        return markets.filterValues { it.hasBuyOrders() || it.hasSellOrders() }
    }
}

fun generatePlanet(rng: RandomGenerator, trainingStrategy: PersonStrategy? = null): Planet {
    val result = Planet(Tumbler(rng))
    if (trainingStrategy != null) {
        result.addPerson(generatePerson(result, rng, trainingStrategy))
    } else {
        result.addPerson(generatePerson(result, rng))
    }
    repeat(97) { result.addPerson(generatePerson(result, rng)) }
    repeat(2) { result.addPerson(generateMarketMaker(result, rng)) }
    return result
}