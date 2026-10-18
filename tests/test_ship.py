import unittest

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.galaxy import StarLaneNetwork
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.ship import Ship, ShipStatus


class TestShip(unittest.TestCase):
    def setUp(self):
        self.earth_market = Market()
        self.mars_market = Market()
        self.earth = Planet("Earth", self.earth_market, 0, 0)
        self.mars = Planet("Mars", self.mars_market, 50, 0)  # 50 units away from Earth

        self.commodity_registry = CommodityRegistry()
        self.fuel = CommodityDefinition(
            id="nova_fuel",
            name="NovaFuel",
            transportable=True,
            description="High-density energy source for starship travel.",
        )
        self.commodity_registry._commodities["nova_fuel"] = self.fuel

        planets = [self.earth, self.mars]
        self.mock_sim = type(
            "MockSimulation",
            (object,),
            {
                "commodity_registry": self.commodity_registry,
                "planets": planets,
                "star_lanes": StarLaneNetwork.complete(planets),
                "current_turn": 0,
            },
        )()

        self.ship = Ship("TestShip", self.mock_sim, self.earth)
        self.earth.add_ship(self.ship)

        # The tank is millifuel; fill it so only the leg burn is under test.
        self.ship.fuel = self.ship.fuel_capacity

    def test_ship_initialization(self):
        self.assertEqual(self.ship.name, "TestShip")
        self.assertEqual(self.ship.planet, self.earth)
        self.assertEqual(self.ship.status, ShipStatus.DOCKED)
        self.assertEqual(self.ship.fuel, self.ship.fuel_capacity)

    def test_distance_calculation(self):
        distance = self.ship.route_distance(self.earth, self.mars)
        self.assertEqual(distance, 50.0)

    def test_fuel_calculation(self):
        distance = 50.0
        fuel_needed = Ship.calculate_fuel_needed(distance)
        # 50 / 200 = 0.25 market units, i.e. 250 millifuel.
        self.assertEqual(fuel_needed, 250)

    def test_journey_start_and_progress(self):
        self.assertEqual(self.ship.last_departure_turn, 0)
        self.mock_sim.current_turn = 7
        self.assertTrue(self.ship.start_journey(self.mars))
        self.assertEqual(self.ship.status, ShipStatus.TRAVELING)
        self.assertEqual(self.ship.destination, self.mars)
        self.assertEqual(self.ship.last_departure_turn, 7)
        self.assertEqual(self.ship.departure_turns, [7])

        fuel_consumed = 250  # 50 / 200 = 0.25 market units of millifuel
        self.assertEqual(self.ship.fuel, self.ship.fuel_capacity - fuel_consumed)

        self.assertEqual(self.ship.travel_time, 3)  # 50 / 20 = 2.5, rounded up to 3

        self.assertFalse(self.ship.update_journey())
        self.assertFalse(self.ship.update_journey())

        self.assertTrue(self.ship.update_journey())
        self.assertEqual(self.ship.status, ShipStatus.DOCKED)
        self.assertEqual(self.ship.planet, self.mars)
        self.assertIn(self.ship, self.mars.ships)
        self.assertNotIn(self.ship, self.earth.ships)

    def test_insufficient_fuel(self):
        ship2 = Ship("FuellessShip", self.mock_sim, self.earth)
        self.earth.add_ship(ship2)
        ship2.fuel = ship2.fuel_required(50.0) - 1

        # Keep the random maintenance check out of this test.
        ship2.check_maintenance = lambda: False

        self.assertFalse(ship2.start_journey(self.mars))

        # Maintenance can still be flagged at random, so either status is accepted.
        self.assertIn(ship2.status, [ShipStatus.DOCKED, ShipStatus.NEEDS_MAINTENANCE])

        # A failed departure must not be recorded as activity.
        self.assertEqual(ship2.last_departure_turn, 0)
        self.assertEqual(ship2.departure_turns, [])


if __name__ == "__main__":
    unittest.main()
