package needs

import Person

const val MAX_SHELTER_ABILITY = 10
const val ODDS_OF_DEGRADING = 0.1

class ShelterNeed : PhysicalNeed() {
    private var shelterQuality = MAX_SHELTER_ABILITY
    override fun visit(person: Person) {
        if (person.planet.biases.rng.nextFloat() < ODDS_OF_DEGRADING) {
            shelterQuality -= 1
        }

        if (shelterQuality < MAX_SHELTER_ABILITY) {
            if (person.goods[GoodKind.WOOD] > 0) {
                person.goods[GoodKind.WOOD] -= 1
                shelterQuality += 1
            }
        }
    }

    override fun getScore(person: Person): Double {
        return shelterQuality.toDouble() / MAX_SHELTER_ABILITY
    }
}
