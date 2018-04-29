using System;
using System.Collections.Generic;
using System.Text;

namespace Toy.Simulation
{
    class SimRunner
    {
        public Universe universe;

        public SimRunner(Universe universe)
        {
            this.universe = universe;
        }

        public void Run()
        {
            for (int i = 0; i < 1000000; i++)
            {
                universe.tick();
            }
        }
    }
}
