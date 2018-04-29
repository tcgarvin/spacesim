using System;
using Toy.Simulation;

namespace Toy
{
    class Program
    {
        static void Main(string[] args)
        {
            Console.WriteLine("Let's see how this goes.");

            Universe uni = Universe.GenerateUniverse();
            Simulation.SimRunner sim = new Simulation.SimRunner(uni);

            Console.WriteLine("Done");
        }
    }
}
