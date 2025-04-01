package actions

import Person
import Planet

interface PersonAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(person: Person)

    /**
     * Determine what a person's bias is for this action.  Useful for AI algorithms to know what they're good at or not
     *
     * @return Multiplier
     */
    fun getPersonBias(person: Person) : Double

    /**
     * Determine what a planet's bias is for this action.  Useful for AI algorithms to know what they're good at or not
     *
     * @return Multiplier
     */
    fun getPlanetBias(planet: Planet) : Double
}