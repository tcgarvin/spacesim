package ships.strategies

import markets.MarketAction
import ships.Ship
import ships.actions.ShipAction

data class ShipStrategyOutput(val shipAction: ShipAction, val marketActions: Collection<MarketAction>)

interface PersonStrategy {
    fun pickNextActions(ship: Ship) : ShipStrategyOutput
}

