package strategies

import Person
import actions.ActionError
import actions.InvalidAction
import actions.NoError
import java.util.concurrent.LinkedBlockingQueue

typealias PersonMLAction = (Person) -> PersonStrategyOutput

/**
 * Lets an off-thread system (like an ML model) control a person
 *
 * @param stateGenerator Generates a state suitable for the ML from the current state of the person.  Should return a
 * pair of the state object the ML needs, and a Double to capture the reward from the most recent turn.  This method is
 * provided to give the ML system flexibility in what it gets.  Probably the only thing that really matters is that you
 * don't take any mutable bits of state (like the person object itself), to avoid race conditions, threading issues,
 * etc. Copy what you need.
 */
class MLHookStrategy<STATE>(val stateGenerator: (Person, ActionError) -> Pair<STATE, Double>) : PersonStrategy {
    val actionRequestQueue = LinkedBlockingQueue<Pair<STATE, Double>>()
    val actionDecisionQueue = LinkedBlockingQueue<PersonMLAction>()

    override fun pickNextActions(person: Person, lastError: ActionError): PersonStrategyOutput {
        if (lastError != NoError) {
            println("Last turn did not go so well: ${lastError.message}")
        }
        actionRequestQueue.put(stateGenerator(person, lastError))
        val mlActionGenerator = actionDecisionQueue.take()

        return try {
            mlActionGenerator(person)
        } catch (e: Exception) {
            PersonStrategyOutput(InvalidAction(e.message ?: e.toString()), listOf())
        }
    }
}