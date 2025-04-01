package strategies

import Person
import actions.ActionError
import actions.NoError
import actions.PersonAction
import markets.MarketAction

data class PersonStrategyOutput(val personAction: PersonAction, val marketActions: Collection<MarketAction>)

interface PersonStrategy {
    fun pickNextActions(person: Person, lastError: ActionError = NoError) : PersonStrategyOutput
}

