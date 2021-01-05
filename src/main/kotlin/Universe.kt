import kotlin.random.Random

class Universe(val starSystems: List<StarSystem>) : Tickable {
    override fun tick() {
        starSystems.forEach { it.tick() }
    }
}

fun generateUniverse(): Universe {
    val starSystems = (1..100).map {
        val x = Random.nextInt(0, 1000)
        val y = Random.nextInt(0, 1000)
        generateStarSystem(x, y)
    }

    return Universe(starSystems)
}