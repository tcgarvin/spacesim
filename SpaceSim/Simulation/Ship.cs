namespace Toy.Simulation
{
    internal class Ship
    {
        private ILocation location;

        public static Ship generateShip(ILocation location)
        {
            return new Ship(location);
        }

        public Ship(ILocation location)
        {
            this.location = location;
        }

    }
}