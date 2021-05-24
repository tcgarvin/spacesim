package ships.actions

import Planet
import ships.Ship

class LandOnPlanet(planet: Planet) : ShipAction {
    override fun apply(ship: Ship) {
        if (!ship.location.isInStarSystem()) {

        }
    }
}