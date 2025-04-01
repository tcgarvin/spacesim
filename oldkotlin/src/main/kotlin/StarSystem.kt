import org.apache.commons.math3.random.RandomGenerator
import strategies.PersonStrategy

class StarSystem(val x: Double, val y: Double, val planets: List<Planet>) : Tickable {
    var neighbors = listOf<StarSystem>()
        private set

    override fun tick() {
        planets.forEach { it.tick() }
    }

    fun addNeighbor(neighbor:StarSystem) {
        neighbors = neighbors + neighbor
    }
}

fun generateStarSystem(x: Double, y: Double, rng: RandomGenerator, trainingStrategy: PersonStrategy? = null): StarSystem {
    val planets = listOf(generatePlanet(rng, trainingStrategy))
    return StarSystem(x, y, planets)
}