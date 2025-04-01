package actions

import Person
import Planet

object WorkForGovernment : PersonAction {
    override fun apply(person: Person) {
        person.addMoney(10)
    }

    override fun getPersonBias(person: Person): Double {
        return 1.0
    }

    override fun getPlanetBias(planet: Planet): Double {
        return 1.0
    }
}
