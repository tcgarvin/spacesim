package ships

import BagOfGoods
import Location
import LocationFactory
import StarLaneLocation
import StarSystem
import Tickable
import getLogger
import markets.MarketAction
import markets.MarketParticipant
import markets.NoNegativeMoney
import markets.NoOpMarketAction
import ships.strategies.AimlessTrader
import ships.strategies.ShipStrategy
import java.util.*

const val TRAVEL_SPEED = 5.0

class Ship(override var location: Location, val strategy: ShipStrategy) : Tickable, MarketParticipant {
    val id = UUID.randomUUID()
    var money = 0
        private set
    override val goods = BagOfGoods()
    val pendingActions = mutableSetOf<MarketAction>()

    override fun tick() {
        pendingActions.forEach { it.cancel(this) }
        pendingActions.clear()

        when (val currentLocation = location) {
            is StarLaneLocation -> location = currentLocation.advance(TRAVEL_SPEED)
            else -> invokeStrategy()
        }
    }

    fun invokeStrategy() {
        val strategyOutput = strategy.pickNextActions(this)
        getLogger().logShipTurn(this, strategyOutput)
        strategyOutput.shipAction.apply(this)
        for (action in strategyOutput.marketActions) {
            action.apply(this)
            if (action != NoOpMarketAction) {
                pendingActions.add(action)
            }
        }
    }

    override fun addMoney(amount: Int) {
        if (money + amount < 0) {
            throw NoNegativeMoney()
        }

        money += amount
    }

    override fun removeMoney(amount: Int) {
        addMoney(-amount)
    }

}

fun generateShip(locationFactory : LocationFactory, starSystem: StarSystem) : Ship {
    return Ship(locationFactory.getPlanetLocation(starSystem.planets.random()), AimlessTrader())
}