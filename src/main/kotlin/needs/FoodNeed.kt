package needs

import Person

private const val FOOD_TIME = 30

class FoodNeed : PhysicalNeed() {
    private val recentDays = MutableList(FOOD_TIME) { 1 }

    override fun visit(person: Person) {
        val wasFed: Int
        if (person.goods[GoodKind.FOOD] > 0) {
            person.goods[GoodKind.FOOD] -= 1
            wasFed = 1
        }
        else {
            wasFed = 0
        }

        recentDays.removeFirst()
        recentDays.add(wasFed)
    }

    override fun getScore(person: Person): Double {
        var numerator = 0
        var denominator = 0
        for (i in 0 until FOOD_TIME) {
            val weight = i + 1
            val value = recentDays[i]
            numerator += value * weight
            denominator += weight
        }
        return numerator.toDouble() / denominator
    }
}