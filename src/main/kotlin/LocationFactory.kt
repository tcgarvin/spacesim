private var locationFactorySingleton : LocationFactory? = null;
fun getLocationFactory() : LocationFactory {
    return locationFactorySingleton!!
}

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

    locationFactorySingleton = locationFactory
    return locationFactory
}