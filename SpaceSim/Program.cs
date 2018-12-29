using System;
using Toy.Simulation;

namespace Toy
{
    class Program
    {
        static void Main()
        {
            Console.WriteLine("Let's see how this goes.");

            Universe uni = Universe.GenerateUniverse();
            Simulation.SimRunner sim = new Simulation.SimRunner(uni);

            // Some basic market stuff
            Market foodMarket = new Market(Good.Food, 10.0, 1.0, 0);
            Console.WriteLine("Current food price: " + foodMarket.CurrentPrice());
            Console.WriteLine("Sold food for: " + foodMarket.Sell());
            Console.WriteLine("Sold food for: " + foodMarket.Sell());
            Console.WriteLine("Sold food for: " + foodMarket.Sell());
            Console.WriteLine("Sold food for: " + foodMarket.Sell());
            Console.WriteLine("Bought 3 food for: " + foodMarket.Buy(3));
            Console.WriteLine("Sold 10 food for: " + foodMarket.Sell(10));
            Console.WriteLine("Current food price: " + foodMarket.CurrentPrice());
            Console.WriteLine("Done");
        }
    }
}