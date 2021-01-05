package strategies

import Person
import actions.PersonAction

interface PersonStrategy {
    fun pickNextAction(person: Person) : PersonAction
}