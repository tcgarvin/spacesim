class Planet() : Tickable {
    val people: MutableList<Person> = mutableListOf()

    override fun tick() {
        people.forEach { it.tick() }
    }

    fun addPerson(person: Person) {
        people.add(person)
    }
}

fun generatePlanet(): Planet {
    val result = Planet()
    (1..100).forEach { result.addPerson(generatePerson(result)) }
    return result
}