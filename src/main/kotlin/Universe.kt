import org.apache.commons.math3.random.Well19937c
import kotlin.random.Random

class Universe(val starSystems: List<StarSystem>) : Tickable {
    override fun tick() {
        starSystems.forEach { it.tick() }
    }
}

fun generateUniverse(): Universe {
    val starSystems = (1..50).map {
        val x = Random.nextInt(0, 600)
        val y = Random.nextInt(0, 600)
        // Give each starsystem it's own rng, since that's where each coroutine will live
        generateStarSystem(x, y, Well19937c())
    }

    return Universe(starSystems)
}