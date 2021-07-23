import generation.StarSystemMapGenerator
import org.apache.commons.math3.random.Well19937c
import org.tinfour.common.Vertex
import ships.ShipTracker
import ships.generateShip
import strategies.PersonStrategy

data class UniverseGenerationConfig(
    val mapSideLength: Double,
    val starDistributionSigma: Double,
    val numStars: Int,
    val coreRadius: Double,
    val starMargin: Double,
    val maxLaneLength: Double,
    val percentEdgesToRemove: Double,
    val trainingStrategy: PersonStrategy?
)

class Universe(val starSystems: Collection<StarSystem>, val shipTracker : ShipTracker) : Tickable {
    override fun tick() {
        starSystems.forEach { it.tick() }
        shipTracker.tick()
    }
}

fun generateUniverse(config: UniverseGenerationConfig): Universe {
    val mapGenerator = StarSystemMapGenerator()
    mapGenerator.generate(600.0, 100.0, 75, 60.0, 12.0, 90.0, 0.25)

    val index = mutableMapOf<Vertex, StarSystem>()
    for (vertex in mapGenerator.starLocations) {
        val starSystem = generateStarSystem(vertex.x, vertex.y, Well19937c())
        index[vertex] = starSystem
    }

    for (edge in mapGenerator.starLanes) {
        val a = index.getValue(edge.a)
        val b = index.getValue(edge.b)
        a.addNeighbor(b)
        b.addNeighbor(a)
    }

    val starSystems = index.values

    val locationFactory = startLocationFactory(starSystems)
    val ships = starSystems.map { generateShip(locationFactory, it) }
    return Universe(starSystems, ShipTracker(ships))
}