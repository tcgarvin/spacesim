package ships.actions

import Planet
import StarSystem
import getLocationFactory
import ships.Ship

class EnterStarLane(private val destination: StarSystem) : ShipAction {
    override fun apply(ship: Ship) {
        if (!ship.location.isInSystemSpace()) {
            throw ImproperAction("You must be in system space to enter a starlane")
        }

        val destinationLocation = getLocationFactory().getStarSystemLocation(destination)
        if (!ship.location.neighbors.contains(destinationLocation)) {
            throw ImproperAction("Destination StarSystem is not a neighbor of the current StarSystem")
        }

        ship.location = getLocationFactory().getStarLaneLocation(ship.location.getStarSystem(), destination, 0.0)
    }
}