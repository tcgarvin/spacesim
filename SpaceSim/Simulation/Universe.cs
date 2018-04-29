using System;
using System.Collections.Generic;

namespace Toy.Simulation
{
    public class Universe
    {
        List<StarSystem> systems;
        List<Ship> ships;

        public static Universe GenerateUniverse()
        {
            Random r = new Random();
            List<StarSystem> newSystems = new List<StarSystem>();
            for (int i = 0; i < 1000; i++)
            {
                int x = r.Next();
                int y = r.Next();
                newSystems.Add(new StarSystem(x, y));
            }

            foreach (var system in newSystems)
            {
                // Find the nearest system in 4 directions and link to them
                StarSystem[] closest = new StarSystem[4];
                foreach (var candidate in newSystems)
                {
                    int offsetX = system.X - candidate.X;
                    int offsetY = system.Y - candidate.Y;
                    double direction = Math.Atan2(offsetY, offsetX) + Math.PI;
                    int quadrant = Convert.ToInt32(Math.Floor(direction / (Math.PI / 2)));

                    StarSystem closestSoFar = closest[quadrant];
                    if (closestSoFar == null)
                    {
                        closest[quadrant] = candidate;
                    }
                    else if (StarSystem.DistanceBetween(system, candidate) > StarSystem.DistanceBetween(system, closestSoFar))
                    {
                        closest[quadrant] = candidate;
                    }
                }

                foreach (var nearbySystem in closest)
                {
                    if (nearbySystem == null)
                    {
                        continue;
                    }
                    system.addNeighbor(nearbySystem);
                    nearbySystem.addNeighbor(system);
                }
            }

            List<Ship> newShips = new List<Ship>();
            for (int i = 0; i < 100; i++)
            {
                ILocation startingLocation = new InSystemLocation(newSystems[i]);
                newShips.Add(Ship.generateShip(startingLocation));
            }

            return new Universe(newSystems, newShips);
        }

        Universe(List<StarSystem> systems, List<Ship> ships)
        {
            this.systems = systems;
            this.ships = ships;
        }

        public void tick()
        {

        }
    }
}