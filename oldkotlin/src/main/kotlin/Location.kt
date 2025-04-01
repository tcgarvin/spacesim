import kotlin.math.pow
import kotlin.math.sqrt

class NoPlanetAtThisLocation : Exception()
class NoStarSystemAtThisLocation : Exception()

abstract class Location(val x : Double, val y : Double) {
    var neighbors = listOf<Location>()

    open fun isInStarLane() : Boolean {
        return false
    }

    open fun isInSystemSpace() : Boolean {
        return false
    }

    open fun getStarSystem() : StarSystem {
        throw NoStarSystemAtThisLocation()
    }

    open fun isOnPlanet() : Boolean {
        return false
    }

    open fun getPlanet() : Planet {
        throw NoPlanetAtThisLocation()
    }
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

        val xDistance = dest.x - source.x
        val yDistance = dest.y - source.y
        val x = source.x + xDistance * percent
        val y = source.y + yDistance * percent

        return StarLaneLocation(x, y, getStarSystemLocation(source), getStarSystemLocation(dest))
    }
}


class PlanetLocation(x : Double, y : Double, private val planet: Planet) : Location(x,y) {
    override fun isOnPlanet() : Boolean {
        return true
    }

    override fun getPlanet() : Planet {
        return planet
    }

    override fun getStarSystem(): StarSystem {
        assert(neighbors.size == 1) // The only way left to go is up
        return neighbors[0].getStarSystem()
    }
}
class StarSystemLocation(x : Double, y : Double, private val starSystem: StarSystem) : Location(x,y) {
    override fun isInSystemSpace(): Boolean {
        return true
    }

    override fun getStarSystem(): StarSystem {
        return starSystem
    }
}

class StarLaneLocation(x : Double, y : Double, val source : StarSystemLocation, val dest : StarSystemLocation) : Location(x,y) {
    override fun isInStarLane(): Boolean {
        return true
    }

    fun advance(travelDistance : Double) : Location {
        val xDistanceRemaining = dest.x - x
        val yDistanceRemaining = dest.y - y
        val distanceRemaining = sqrt(xDistanceRemaining.pow(2) + yDistanceRemaining.pow(2))
        if (distanceRemaining < travelDistance) {
            return dest
        }

        val percentRemainingTraveled = travelDistance / distanceRemaining
        val nextX = x + xDistanceRemaining * percentRemainingTraveled
        val nextY = y + yDistanceRemaining * percentRemainingTraveled
        return StarLaneLocation(nextX, nextY, source, dest)
    }
}