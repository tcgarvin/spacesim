import kotlin.math.abs

open class Location(val x : Double, val y : Double) {
    var neighbors = listOf<Location>()
}


class LocationFactory() {
    private val starSystemLocations = mutableMapOf<StarSystem, StarSystemLocation>()
    private val planetLocations = mutableMapOf<Planet, PlanetLocation>()
    fun getStarSystemLocation(starSystem: StarSystem) : StarSystemLocation {
        return starSystemLocations.getOrPut(starSystem) { StarSystemLocation(starSystem.x, starSystem.y, starSystem) }
    }

    fun getPlanetLocation(planet: Planet) : PlanetLocation {
        return planetLocations.getValue(planet)
    }

    fun getOrPutPlanetLocation(planet: Planet, starSystem : StarSystem) : PlanetLocation {
        return planetLocations.getOrPut(planet) { PlanetLocation(starSystem.x, starSystem.y, planet) }
    }

    fun getStarLaneLocation(source : StarSystem, dest: StarSystem, percent : Double) : StarLaneLocation {
        if (percent < 0 || percent > 1) {
            throw IllegalArgumentException("Travel can be between 0 and 100% complete, not outside those bounds")
        }

        val xDistance = abs(dest.x - source.x)
        val yDistance = abs(dest.y - source.y)
        val x = source.x + xDistance * percent
        val y = source.y + yDistance * percent

        return StarLaneLocation(x, y, source, dest)
    }
}


class PlanetLocation(x : Double, y : Double, val planet: Planet) : Location(x,y)
class StarSystemLocation(x : Double, y : Double, val starSystem: StarSystem) : Location(x,y)
class StarLaneLocation(x : Double, y : Double, val source : StarSystem, val dest : StarSystem)

fun startLocationFactory(starSystems: Collection<StarSystem>) : LocationFactory {
    val locationFactory = LocationFactory()

    for (starSystem in starSystems) {
        val starSystemLocation = locationFactory.getStarSystemLocation(starSystem)
        for (planet in starSystem.planets) {
            val planetLocation = locationFactory.getOrPutPlanetLocation(planet, starSystem)
            starSystemLocation.neighbors = starSystemLocation.neighbors + planetLocation
            planetLocation.neighbors = planetLocation.neighbors + starSystemLocation
        }

        for (neighborSystem in starSystem.neighbors) {
            val neighborLocation = locationFactory.getStarSystemLocation(neighborSystem)
            if (!starSystemLocation.neighbors.contains(neighborLocation)) {
                starSystemLocation.neighbors = starSystemLocation.neighbors + neighborLocation
                neighborLocation.neighbors = neighborLocation.neighbors + starSystemLocation
            }
        }
    }

    return locationFactory
}