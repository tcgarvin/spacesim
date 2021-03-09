package needs

import Person

class NeedsHierarchy {
    val foodNeed = FoodNeed()
    val shelterNeed = ShelterNeed()
    private val needs = listOf(foodNeed, shelterNeed)

    fun visit(person: Person) {
        needs.forEach { it.visit(person) }
    }

    fun getScore(person: Person): Double {
        return needs.map {it.getScore(person)}.average()
    }
}