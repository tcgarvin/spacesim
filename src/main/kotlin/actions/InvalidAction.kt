package actions

import Person
import Planet

/**
 * Represents an action that will fail, causing an ActionError
 */
class InvalidAction(private val message:String) : PersonAction {
    override fun apply(person: Person) {
        throw ActionError(message)
    }

    override fun getPersonBias(person: Person): Double {
        return 1.0
    }

    override fun getPlanetBias(planet: Planet): Double {
        return 1.0
    }


}