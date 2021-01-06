import org.apache.commons.math3.random.RandomGenerator

class StarSystem(val x: Int, val y: Int, val planets: List<Planet>) : Tickable {
    override fun tick() {
        planets.forEach { it.tick() }
    }
}

fun generateStarSystem(x: Int, y: Int, rng: RandomGenerator): StarSystem {
    val planets = listOf(generatePlanet(rng))
    return StarSystem(x, y, planets)
}