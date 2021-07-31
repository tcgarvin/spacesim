import ml.person.PersonControlProcess
import ml.person.PersonMLState
import org.deeplearning4j.rl4j.learning.sync.qlearning.QLearning.QLConfiguration
import org.deeplearning4j.rl4j.learning.sync.qlearning.discrete.QLearningDiscreteDense
import org.deeplearning4j.rl4j.network.dqn.DQNFactoryStdDense
import org.nd4j.linalg.learning.config.Adam


fun main(args: Array<String>) {

    val qLearner = QLConfiguration(
        123,  //Random seed
        30,  //Max step Every epoch
        100 * 2000,  //Max step
        100 * 2000,  //Max size of experience replay
        40,  //size of batches
        10,  //target update (hard)
        0,  //num step noop warmup
        0.01,  //reward scaling
        0.9,  //gamma
        1.0,  //td-error clipping
        0.1f,  //min epsilon
        100,  //num step for eps greedy anneal
        false //double DQN
    )

    // The neural network used by the agent. Note that there is no need to specify the number of inputs/outputs.
    // These will be read from the gym environment at the start of training.
    val network = DQNFactoryStdDense.Configuration.builder()
        .updater(Adam(0.001))
        .numLayer(2)
        .numHiddenNodes(16)
        .build();

    //Create the gym environment. We include these through the rl4j-gym dependency.
    val mdp = PersonControlProcess()

    //Create the solver.
    val dql = QLearningDiscreteDense<PersonMLState>(mdp, network, qLearner)

    dql.train()
    mdp.close()
}