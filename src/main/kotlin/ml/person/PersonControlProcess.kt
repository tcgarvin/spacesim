package ml.person

import Simulation
import UniverseGenerationConfig
import actions.WorkForGovernment
import generateUniverse
import org.deeplearning4j.gym.StepReply
import org.deeplearning4j.rl4j.mdp.MDP
import org.deeplearning4j.rl4j.space.ArrayObservationSpace
import org.deeplearning4j.rl4j.space.DiscreteSpace
import org.deeplearning4j.rl4j.space.ObservationSpace
import strategies.MLHookStrategy
import strategies.PersonStrategyOutput
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

class PersonControlProcess : MDP<PersonMLState, Int, DiscreteSpace> {

    private val observationSpace = ArrayObservationSpace<PersonMLState>(intArrayOf(INPUT_LAYER_SIZE))
    private val actionSpace = DiscreteSpace(PERSON_ML_ACTIONS.size)

    private var reward = 0.0
    private var simulation : Thread? = null
    private var simulationHook : MLHookStrategy<PersonMLState>? = null
    private var simLive = AtomicBoolean(false)
    private var tick = AtomicInteger(0)

    init {
        reset()
    }

    override fun getObservationSpace(): ObservationSpace<PersonMLState> {
        return observationSpace
    }

    override fun getActionSpace(): DiscreteSpace {
        return actionSpace
    }

    /**
     * Not sure why there's both a reset and a newInstance
     */
    override fun reset(): PersonMLState {
        close()
        simulationHook = MLHookStrategy(stateGenerator = { Pair(PersonMLState(it), it.getScore()) })
        val universeConfig = UniverseGenerationConfig(600.0, 100.0, 75, 60.0, 12.0, 90.0, 0.25, simulationHook)

        reward = 0.0
        tick.set(0)
        simLive.set(true)
        simulation = thread(start = true, isDaemon = false, name = "Training Simulation") {
            val universe = generateUniverse(universeConfig)
            val sim = Simulation(universe)
            for (turn in 0 until 300) {
                sim.tick()
                tick.incrementAndGet()
            }
            simLive.set(false)
        }

        return simulationHook!!.actionRequestQueue.take().first
    }

    /**
     * What is this about?  Is it just to close out clients and the like?  I'm going to call it the first part of a
     * restart
     */
    override fun close() {
        reward = 0.0
        while (simulation?.isAlive == true) {
            // A little hacky. Rather than interrupt the simulation, we'll feed it some back actions, see if we can get
            // it to end
            while (simulationHook!!.actionDecisionQueue.size < 10) {
                simulationHook!!.actionDecisionQueue.put { PersonStrategyOutput(WorkForGovernment, listOf()) }
            }
            simulation!!.join(100)
        }
    }

    override fun isDone(): Boolean {
        // Without knowing the details of when this is called, a little worried about race conditions
        return tick.get() >= 250
    }

    override fun newInstance(): MDP<PersonMLState, Int, DiscreteSpace> {
        return PersonControlProcess()
    }

    override fun step(action: Int?): StepReply<PersonMLState> {
        // Action it nullable, but I think that's just a holdover from Java
        if (action == null) {
            throw Exception("Action cannot be null")
        }
        simulationHook!!.actionDecisionQueue.put(PERSON_ML_ACTIONS[action])

        val simResponse = simulationHook!!.actionRequestQueue.take()
        return StepReply(simResponse.first, simResponse.second, isDone, null)
    }

}