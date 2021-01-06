import org.apache.commons.math3.distribution.AbstractRealDistribution
import org.apache.commons.math3.distribution.NormalDistribution
import org.apache.commons.math3.random.RandomGenerator

class BiasMapping(private val rng: RandomGenerator) {
    private val mapping: MutableMap<Any, Double> = mutableMapOf()

    fun getBias(key: Any, distribution: AbstractRealDistribution): Double {
        return mapping.getOrPut(key) { distribution.sample() }
    }

    fun getNormalDistribution(mean: Double, sigma: Double) : NormalDistribution {
        return NormalDistribution(rng, mean, sigma)
    }
}