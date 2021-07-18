package actorml

import Person
import burlap.mdp.core.action.Action
import strategies.PersonStrategy
import strategies.PersonStrategyOutput
import java.util.*

class BurlapPersonStrategy(actionQueue: Queue<Action>, resultQueue: Queue<Pair<PersonState, Int>>) : PersonStrategy {
    override fun pickNextActions(person: Person): PersonStrategyOutput {

    }
}