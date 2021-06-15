package ships.actions

import ships.Ship

class TakeOff() : ShipAction {
    override fun apply(ship: Ship) {
        if (!ship.location.isOnPlanet()) {
            throw ImproperAction("To take off, you must first be on a planet")
        }

        ship.location = ship.location.neighbors[0]  // No where to go but up?
    }
}