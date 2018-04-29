namespace Toy.Simulation
{
    internal class InSystemLocation : ILocation
    {
        private StarSystem starSystem;

        public InSystemLocation(StarSystem starSystem)
        {
            this.starSystem = starSystem;
        }

        public int X => starSystem.X;
        public int Y => starSystem.Y;
    }
}