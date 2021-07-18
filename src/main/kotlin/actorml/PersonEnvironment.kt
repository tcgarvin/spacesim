package actorml

import burlap.mdp.core.action.Action
import burlap.mdp.core.state.State
import burlap.mdp.singleagent.environment.Environment
import burlap.mdp.singleagent.environment.EnvironmentOutcome
import java.util.*

class PersonEnvironment(
    private val actionQueue: Queue<Pair<Action, Action>>,
    private val resultQueue: Queue<Pair<PersonState, Double>>
) : Environment {

    private var currentObservation: PersonState = resultQueue.remove().first
    private var lastReward: Double = 0.0

    override fun currentObservation(): State {
        return currentObservation
    }

    override fun executeAction(a: Action?): EnvironmentOutcome {
        actionQueue.offer(a)

        val result = resultQueue.remove()
        return EnvironmentOutcome(currentObservation, a, result.first, result.second, false)
    }

    override fun lastReward(): Double {
        return lastReward
    }

    override fun isInTerminalState(): Boolean {
        return false
    }

    override fun resetEnvironment() {
        TODO("Not implemented")
    }
}