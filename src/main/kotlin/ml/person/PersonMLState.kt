package ml.person

import Person
import actions.MakeGood
import org.deeplearning4j.rl4j.space.Encodable
import kotlin.math.pow

// XXX: This needs to match up precisely with the actual array init below
const val INPUT_LAYER_SIZE = 8 + (4 + 8 * 4) * 2 + 2 + 4

/**
 * Truth be told, I have no idea how to encode large integers while keeping sensitivity to small numbers, but this
 * function is responsible for it.
 */
fun addInt(array: DoubleArray, offset: Int, value:Int, neuronCount:Int) : Int {
    for (i in 0 until neuronCount) {
        val mod = 4.0.pow(i + 1).toInt()
        array[offset + i] = if (value < mod) value / (mod - 1).toDouble() else 1.0
    }
    return offset + neuronCount
}

fun addNeuron(array: DoubleArray, offset: Int, value: Double) : Int {
    array[offset] = value
    return offset + 1
}

class PersonMLState(person:Person) : Encodable {
    val value : DoubleArray

    init {
        value = DoubleArray(INPUT_LAYER_SIZE)
        var offset = 0

        val markets = person.planet.getActiveMarkets()

        offset = addInt(value, 0, person.money, 8)
        for (good in GoodKind.values()) {
            offset = addInt(value, offset, person.goods[good], 4)
            if (good in markets) {
                val market = markets.getValue(good)
                offset = addInt(value, offset, market.get30DayAveragePrice().toInt(), 8)
                offset = addInt(value, offset, market.get30DayAverageVolume().toInt(), 8)
                val bestSellPrice = if (market.hasSellOrders()) market.getBestSellOrder().price else 0
                val bestBuyPrice = if (market.hasBuyOrders()) market.getBestBuyOrder().price else Int.MAX_VALUE
                offset = addInt(value, offset, bestSellPrice, 8)
                offset = addInt(value, offset, bestBuyPrice, 8)
            }  else {
                offset = addInt(value, offset, 0, 8)
                offset = addInt(value, offset, 0, 8)
                offset = addInt(value, offset, 0, 8)
                offset = addInt(value, offset, 0, 8)
            }
        }

        offset = addNeuron(value, offset, person.needs.foodNeed.getScore(person))
        offset = addNeuron(value, offset, person.needs.shelterNeed.getScore(person))

        offset = addNeuron(value, offset, MakeGood.WOOD.getPersonBias(person))
        offset = addNeuron(value, offset, MakeGood.WOOD.getPlanetBias(person.planet))
        offset = addNeuron(value, offset, MakeGood.FOOD.getPersonBias(person))
        offset = addNeuron(value, offset, MakeGood.FOOD.getPlanetBias(person.planet))

        assert(offset == INPUT_LAYER_SIZE)
    }
    override fun toArray(): DoubleArray {
        return value
    }

}
