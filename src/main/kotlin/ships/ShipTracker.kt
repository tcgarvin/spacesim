package ships

import Tickable

class ShipTracker(val ships : Collection<Ship>) : Tickable {
    override fun tick() {
        for (ship in ships) {
            ship.tick()
        }
    }
}