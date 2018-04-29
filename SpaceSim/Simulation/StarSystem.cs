using System;
using System.Collections.Generic;

namespace Toy.Simulation
{
    internal class StarSystem
    {
        private int x;
        private int y;

        private HashSet<StarSystem> neighbors;

        public static double DistanceBetween(StarSystem a, StarSystem b)
        {
            return Math.Sqrt((a.X - b.X) ^ 2 + (a.Y - b.Y) ^ 2);
        }

        public StarSystem(int x, int y)
        {
            this.x = x;
            this.y = y;
            this.neighbors = new HashSet<StarSystem>();
        }

        public int X { get => x; set => x = value; }
        public int Y { get => y; set => y = value; }

        public void addNeighbor(StarSystem neighbor)
        {
            this.neighbors.Add(neighbor);
        }
    }
}