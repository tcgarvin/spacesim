from good import Good
from market import Market
from universe import Universe
from simulation import Simulation

universe = Universe()
simulation = Simulation(universe)
simulation.run()

# Some basic market stuff
foodMarket = Market(Good.food, 100.0, 1.0, 0)
print("Current food price: " + str(foodMarket.current_price()))
print("Sold food for: " + str(foodMarket.sell_one()))
print("Sold food for: " + str(foodMarket.sell_one()))
print("Sold food for: " + str(foodMarket.sell_one()))
print("Bought 3 food for: " + str(foodMarket.buy(3)))
print("Sold 10 food for: " + str(foodMarket.sell(10)))
print("Current food price: " + str(foodMarket.current_price()))
print("Done")