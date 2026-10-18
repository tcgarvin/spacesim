import math
import random

from spacesim2.core.simulation import Simulation


class TestFictionalPlanets:
    """Fictional planet generation."""

    def test_generates_correct_number_of_planets(self):
        """setup_simple creates the requested number of planets."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=5, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        assert len(sim.planets) == 5

    def test_planets_have_fictional_names(self):
        """Planet names are fictional, not Sol system names."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=3, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        sol_system_names = {
            "Earth",
            "Mars",
            "Venus",
            "Jupiter",
            "Saturn",
            "Mercury",
            "Neptune",
            "Uranus",
        }
        planet_names = {planet.name for planet in sim.planets}

        assert len(planet_names.intersection(sol_system_names)) == 0

    def test_minimum_distance_separation(self):
        """Planets are at least 10 units apart."""
        random.seed(42)
        sim = Simulation()
        sim.setup_simple(
            num_planets=4, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        min_distance = float("inf")
        for i, planet1 in enumerate(sim.planets):
            for j, planet2 in enumerate(sim.planets):
                if i < j:
                    distance = math.sqrt(
                        (planet1.x - planet2.x) ** 2 + (planet1.y - planet2.y) ** 2
                    )
                    min_distance = min(min_distance, distance)

        assert min_distance >= 10.0, (
            f"Minimum distance {min_distance} is less than 10.0"
        )

    def test_planets_within_map_bounds(self):
        """Planets lie within the galaxy bounds."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=6, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        width, height = sim.galaxy_size
        for planet in sim.planets:
            assert 0 <= planet.x <= width, (
                f"Planet {planet.name} x-coordinate {planet.x} out of bounds"
            )
            assert 0 <= planet.y <= height, (
                f"Planet {planet.name} y-coordinate {planet.y} out of bounds"
            )

    def test_unique_planet_names(self):
        """Planet names are unique."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=8, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        planet_names = [planet.name for planet in sim.planets]
        assert len(planet_names) == len(set(planet_names)), (
            "Duplicate planet names found"
        )

    def test_position_generation_with_limited_space(self):
        """Crowded placement still keeps planets 10 units apart."""
        sim = Simulation()

        # May place fewer planets than requested when space runs out.
        sim.setup_simple(
            num_planets=20, num_regular_actors=1, num_market_makers=1, num_ships=1
        )

        assert len(sim.planets) > 0

        if len(sim.planets) > 1:
            min_distance = float("inf")
            for i, planet1 in enumerate(sim.planets):
                for j, planet2 in enumerate(sim.planets):
                    if i < j:
                        distance = math.sqrt(
                            (planet1.x - planet2.x) ** 2 + (planet1.y - planet2.y) ** 2
                        )
                        min_distance = min(min_distance, distance)

            assert min_distance >= 10.0
