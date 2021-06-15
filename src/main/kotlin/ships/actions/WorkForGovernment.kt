package ships.actions

import ships.Ship

object WorkForGovernment : ShipAction {
    override fun apply(ship: Ship) {
        if (!ship.location.isOnPlanet()) {
            throw ImproperAction("To work for the government you must be on a planet")
        }

        ship.addMoney(10)
    }
}