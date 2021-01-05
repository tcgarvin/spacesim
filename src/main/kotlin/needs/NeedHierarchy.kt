package needs

import Person

class NeedsHierarchy {
    val foodNeed = FoodNeed()

    fun visit(person: Person) {
        foodNeed.visit(person)
    }

    fun getScore(person: Person): Double {
        return foodNeed.getScore(person)
    }
}