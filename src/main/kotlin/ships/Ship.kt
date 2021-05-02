package ships

import BagOfGoods
import Location
import LocationFactory
import StarSystem
import Tickable

class Ship(var location: Location) : Tickable {
    var money = 0
        private set
    val goods = BagOfGoods()

    override fun tick() {

    }

}

fun generateShip(locationFactory : LocationFactory, starSystem: StarSystem) : Ship {
    return Ship(locationFactory.getPlanetLocation(starSystem.planets.random()))
}