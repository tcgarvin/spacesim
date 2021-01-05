from good import GoodKind
from supply_curve_market import Market as SupplyMarket
from order_matching_market import Market as OrderMarket
from universe import Universe
from simulation import Simulation

universe = Universe()
simulation = Simulation(universe)
simulation.run()

# Some basic market stuff
# foodMarket = SupplyMarket(GoodKind("Food"), 100.0, 1.0, 0)
# print("Current food price: " + str(foodMarket.current_price()))
# print("Sold food for: " + str(foodMarket.sell_one()))
# print("Sold food for: " + str(foodMarket.sell_one()))
# print("Sold food for: " + str(foodMarket.sell_one()))
# print("Bought 3 food for: " + str(foodMarket.buy(3)))
# print("Sold 10 food for: " + str(foodMarket.sell(10)))
# print("Current food price: " + str(foodMarket.current_price()))
# print("Done")
#
#
## Try an order-based market
#
# def executed(order, amount):
#    print(f"{order} has {amount} filled.")
#
# print("Trying order-based market")
# foodMarket = OrderMarket()
# foodMarket.place_buy_order(10, 1, executed)
# foodMarket.execute_orders()
# foodMarket.place_sell_order(10, 1, executed)
# foodMarket.execute_orders()
# foodMarket.place_buy_order(5, 2, executed)
# foodMarket.place_buy_order(6, 1, executed)
# foodMarket.place_sell_order(3, 2, executed)
# foodMarket.execute_orders()
# print("Done")
