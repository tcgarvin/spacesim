package ships.actions

import ships.Ship

class ImproperAction(message : String) : Exception(message)

interface ShipAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(ship: Ship)
}
