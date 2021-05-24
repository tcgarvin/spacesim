package ships.actions

import ships.Ship

interface ShipAction {
    /**
     * Apply this action to the given person.
     */
    fun apply(ship: Ship)
}
