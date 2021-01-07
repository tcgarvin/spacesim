import org.apache.commons.math3.random.RandomGenerator

class Planet(val biases: Tumbler) : Tickable {
    val people: MutableList<Person> = mutableListOf()

    override fun tick() {
        people.forEach { it.tick() }
    }

    fun addPerson(person: Person) {
        people.add(person)
    }
}

fun generatePlanet(rng: RandomGenerator): Planet {
    val result = Planet(Tumbler(rng))
    (1..100).forEach { result.addPerson(generatePerson(result, rng)) }
    return result
}