package ships.actions

import Planet
import getLocationFactory
import ships.Ship

class LandOnPlanet(private val planet: Planet) : ShipAction {
    override fun apply(ship: Ship) {
        val planetLocation = getLocationFactory().getPlanetLocation(planet)

        if (!ship.location.neighbors.contains(planetLocation)) {
            throw ImproperAction("Planet is not a neighbor of the ship's location")
        }

        ship.location = planetLocation
    }
}