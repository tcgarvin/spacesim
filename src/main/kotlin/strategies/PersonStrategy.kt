package strategies

import Person
import actions.MarketAction
import actions.PersonAction

data class PersonStrategyOutput(val personAction: PersonAction, val marketActions: Collection<MarketAction>)

interface PersonStrategy {
    fun pickNextActions(person: Person) : PersonStrategyOutput
}

