package actions

import DistributionGenerator
import GoodKind
import Person
import org.apache.commons.math3.distribution.NormalDistribution

private fun normal(mean: Double, sigma: Double): DistributionGenerator =
    { rng -> NormalDistribution(rng, mean, sigma) }

enum class MakeGood(
    val kind: GoodKind,
    val planetDistributionGenerator: DistributionGenerator,
    val personDistributionGenerator: DistributionGenerator,
    val applicationDistributionGenerator: DistributionGenerator
) : PersonAction {
    
    FOOD(GoodKind.FOOD, normal(1.0, 0.05), normal(1.0, 0.05), normal(1.0, 0.05)),
    WOOD(GoodKind.WOOD, normal(1.0, 0.05), normal(1.0, 0.05), normal(1.0, 0.05));

    override fun apply(person: Person) {
        val planetBias = person.planet.biases.getBias(this, planetDistributionGenerator)
        val personBias = person.biases.getBias(this, personDistributionGenerator)
        val applicationRoll = person.planet.biases.getDistribution(this, applicationDistributionGenerator).sample()
        val goodsMade = planetBias * personBias * applicationRoll

        val wholeGoodsMade = goodsMade.toInt()
        val partialGoodsMade = goodsMade - wholeGoodsMade

        var existingPartialGoods = person.partialGoods.getValue(kind)
        existingPartialGoods += partialGoodsMade
        val additionalGoods = existingPartialGoods.toInt()
        existingPartialGoods -= additionalGoods

        person.partialGoods[kind] = existingPartialGoods
        person.goods[kind] += wholeGoodsMade + additionalGoods
    }
}