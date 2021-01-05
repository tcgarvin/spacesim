class StarSystem(val x: Int, val y: Int, val planets: List<Planet>) : Tickable {
    override fun tick() {
        planets.forEach { it.tick() }
    }
}

fun generateStarSystem(x: Int, y: Int): StarSystem {
    val planets = listOf(generatePlanet())
    return StarSystem(x, y, planets)
}