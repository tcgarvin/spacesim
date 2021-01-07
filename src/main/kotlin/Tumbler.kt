import org.apache.commons.math3.distribution.AbstractRealDistribution
import org.apache.commons.math3.distribution.NormalDistribution
import org.apache.commons.math3.random.RandomGenerator

typealias DistributionGenerator = (RandomGenerator) -> AbstractRealDistribution

/**
 * Random state management.
 *
 * This is a little overwrought, but the idea is to try to keep some of the randomness together, both consistent biases
 * that are generated once and then do not change, as well as random values that need to be drawn many times.  This
 * class is not intended to be thread-safe, as each Tumbler should exist within a single model object, such that no
 * two threads will be looking at the same tumbler.
 */
class Tumbler(private val rng: RandomGenerator) {
    private val biasMapping: MutableMap<Any, Double> = mutableMapOf()
    private val distributionMapping: MutableMap<Any, AbstractRealDistribution> = mutableMapOf()

    fun getBias(key: Any, distributionGenerator: DistributionGenerator): Double {
        return biasMapping.getOrPut(key) { distributionGenerator(rng).sample() }
    }

    fun getDistribution(key: Any, distributionGenerator: DistributionGenerator): AbstractRealDistribution {
        return distributionMapping.getOrPut(key) { distributionGenerator(rng) }
    }
}