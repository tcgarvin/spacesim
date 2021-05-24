package actions

import Person

interface PersonAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(person: Person)
}