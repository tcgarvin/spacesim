using System;
using System.Collections.Generic;
namespace Toy.Simulation
{
    public class Market
    {
        double elasticity;
        double maxPrice;
        int supply;

        public Market(Good good, double maxPrice, double elasticity, int startingSupply) {
            this.maxPrice = maxPrice;
            this.elasticity = elasticity;
            this.supply = startingSupply;
        }

        public double CurrentPrice() {
            //P = a − bQ, a - the price at which none of the good is desired, b the elasticity, Q the quantity available
            return Math.Max(maxPrice - (elasticity * supply), 0);
        }

        public double Buy() {
            if (supply == 0) {
                throw new Exception("No available sellers");
            }
            double price = CurrentPrice();
            supply--;
            return price;
        }

        public double Sell() {
            double price = CurrentPrice();
            supply++;
            return price;
        }

        public double Buy(int quantity) {
            if (supply >= quantity)
            {
                double startingPrice = CurrentPrice();
                supply -= quantity;
                double endingPrice = CurrentPrice();

                return (startingPrice + (endingPrice + 1)) / 2.0;
            }
            else
            {
                throw new Exception("Insufficient goods");
            }
        }

        public double Sell(int quantity)
        {
            double revenue = 0.0;
            for (int i = 0; i < quantity; i++)
            {
                revenue += Sell();
            }
            return revenue;
        }
    }
}
