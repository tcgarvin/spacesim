from abc import ABC, abstractmethod
from collections import defaultdict, deque
from numpy import floor, ceil, histogram

from scipy.stats import norm

from person import Person
from person_need import Need, WealthNeed
from good import GoodKind, Recipe, basic_food_recipe, basic_wood_recipe, food, wood
from grindstone import grindstone
from order_matching_market import SellOrder
 
NUM_BUY_ORDERS = 10
NUM_SELL_ORDERS = 10

class PersonActor(ABC):
    @abstractmethod
    def tick(self):
        """
        Entrypoint for person logic.  Makes and executes decisions.
        """
        pass

class Plebeian(PersonActor):
    """
    A Plebeian is the most basic actor, making things by hand or taking jobs,
    buying things they cannot make by hand at the market.  A regular joe.
    """
    NO_OP_CALLBACK = lambda *args: None

    def __init__(self, person: Person):
        self.person = person
        self.partial_labor = {}
        self.marginal_utilities = {}

    def _estimate_wealth_gain(self, wealth_need : Need):
        best_gain = self.marginal_utilities[wealth_need] * 10
        for need in (self.person.needs.food, self.person.needs.shelter):
            market = self.person.planet.get_market(need.get_good())
            if market.has_sell_orders():
                buy_price = market.lowest_sell_offer()
                potential_gain = (10 / buy_price) * self.marginal_utilities[need]
                if potential_gain > best_gain:
                    best_gain = potential_gain

        return best_gain

    def _estimate_gain(self, need: Need, recipe: Recipe):
        estimated_fulfillment = self.person.estimate_production(recipe)
        marginal_utility = self.marginal_utilities[need]
        best_gain = estimated_fulfillment * marginal_utility / max(1, self.person.goods[need.get_good()])
        market = self.person.planet.get_market(need.get_good())
        if market.has_buy_orders():
            sell_price = market.highest_buy_offer()
            potential_gain = estimated_fulfillment * sell_price * self.marginal_utilities[self.person.needs.wealth]
            if potential_gain > best_gain:
                best_gain = potential_gain

        return best_gain

    def tick(self):
        # This logic is starting to get hairy. There's another level of
        # abstraction needed once the basic algorithm is settled on. Strategy
        # objects, maybe.
        p = self.person

        food_need, wood_need, wealth_need = p.needs.get_needs()
        for need in p.needs.get_needs():
            self.marginal_utilities[need] = need.get_marginal_utility(p)

        wealth_score_gain = self._estimate_wealth_gain(wealth_need)
        self.marginal_utilities[wealth_need] = wealth_score_gain / 10

        food_score_gain = self._estimate_gain(food_need, basic_food_recipe)
        shelter_score_gain = self._estimate_gain(wood_need, basic_wood_recipe)
        if food_score_gain > shelter_score_gain and food_score_gain > wealth_score_gain:
            p.execute_recipe(basic_food_recipe)

        elif shelter_score_gain > wealth_score_gain:
            p.execute_recipe(basic_wood_recipe)

        else:
            p.work_for_government()

        # Next, determine if we want to take a market action. For relative
        # simplicity, we'll pick at most one action.
        p.cancel_all_orders()

        # There are 4 possible actions.  Buy/sell food/wood.  See which of 
        # these are desirable, and pick the most desirable one, if any.

        food_market = p.planet.get_market(food)
        wood_market = p.planet.get_market(wood)

        buy_food_gain = 0
        buy_wood_gain = 0
        sell_food_gain = 0
        sell_wood_gain = 0

        if p.money > 0:
            if food_market.has_sell_orders():
                food_price = food_market.lowest_sell_offer()
                if p.money > food_price:
                    buy_food_gain = self.marginal_utilities[food_need] - (self.marginal_utilities[wealth_need] * food_market.lowest_sell_offer())

            if wood_market.has_sell_orders():
                wood_price = wood_market.lowest_sell_offer()
                if p.money > wood_price:
                    buy_wood_gain = self.marginal_utilities[wood_need] - (self.marginal_utilities[wealth_need] * wood_market.lowest_sell_offer())

        if p.goods[food] > 1 and food_market.has_buy_orders():
            food_price = food_market.highest_buy_offer()
            sell_food_gain = self.marginal_utilities[wealth_need] * food_price - self.marginal_utilities[food_need]

        if p.goods[wood] > 1 and wood_market.has_buy_orders():
            wood_price = wood_market.highest_buy_offer()
            sell_wood_gain = self.marginal_utilities[wealth_need] * wood_price - self.marginal_utilities[wood_need]

        best_market_option = max(buy_food_gain, buy_wood_gain, sell_food_gain, sell_wood_gain)
        if best_market_option <= 0:
            return

        if buy_food_gain == best_market_option:
            price = food_market.lowest_sell_offer()
            p.place_buy_order(food, price, int(p.money // price), self.NO_OP_CALLBACK)

        elif buy_wood_gain == best_market_option:
            price = wood_market.lowest_sell_offer()
            p.place_buy_order(wood, price, int(p.money // price), self.NO_OP_CALLBACK)

        elif sell_food_gain == best_market_option:
            p.place_sell_order(food, food_market.highest_buy_offer(), p.goods[food] - 1, self.NO_OP_CALLBACK)

        elif sell_wood_gain == best_market_option:
            p.place_sell_order(wood, wood_market.highest_buy_offer(), p.goods[wood] - 1, self.NO_OP_CALLBACK)


class MarketMaker(PersonActor):
    """
    A MarketMaker prefers to arbitrage in the market to make their living.
    """
    def __init__(self, person:Person):
        self.person = person
        self._allocations = defaultdict(int)

    def _sell_order_callback(self, good:GoodKind, order:SellOrder, filled:int):
        self._allocations[good] += order.offer_price * filled

    def _buy_order_callback(self, good:GoodKind, order:SellOrder, filled:int):
        self._allocations[good] -= order.offer_price * filled

    def tick(self):
        """
        """
        p = self.person
        p.cancel_all_orders()

        money_to_allocate = (p.money - sum(self._allocations.values())) // len([food, wood])

        for good in [food, wood]:
            self._allocations[good] += money_to_allocate
            amount_held = p.goods[good]
            amount_allocated = self._allocations[good]
            market = p.planet.get_market(good)

            # Constant: Market makers use 30 day moving averages
            # TODO: Parameterize
            avg_volume = market.get_30d_avg_volume()
            avg_price = max(market.get_30d_avg_price(), 1)
            price_sigma = max(market.get_30d_sigma_price(), 1)

            # Low-volume / boostrapping markets need to have a wider sigma, so
            # that prices can fluxuate from low to high more easily.
            # Constant: 30d volume under 3 linearly increases sigma
            # TODO: Parameterize
            if avg_volume < 3:
                price_sigma *= (1 + (3-avg_volume)/3)


            # Constant: market makers aim to keep 30 days of inventory + 1
            # TODO: Parameterize
            target_inventory = avg_volume * 30 + 1
            current_inventory_as_percent_of_target = amount_held / target_inventory

            # Determine a spread based on the average price and how much
            # inventory we have vs our target. Consider a gausian curve in
            # which x is the price point and f(x) is the portion of inventory
            # presented at that price point. As inventory grows from 0, we fill
            # the curve from the right side, until, when inventory is exactly
            # at target, we have filled half the curve. If inventory exceeds
            # our target, we extend into lower prices on the left side of the
            # graph.

            # The buy side uses the unfilled left side of the curve, such that
            # when we are not at target inventory, we are willing to pay over
            # the average price, and when we are over target, we are not
            # willing to page the average price.

            # Between the buy and sell side, seeking to be at our target
            # inventory should allow us to apply price pressure to the market

            price_dist = norm(loc=avg_price, scale=price_sigma)
            curve_percentile = 1 - (0.5 * current_inventory_as_percent_of_target)
            curve_percentile = max(0.01, min(0.99, curve_percentile))
            target_sell_price = ceil(price_dist.ppf(curve_percentile))
            target_buy_price = floor(price_dist.ppf(curve_percentile))
            if target_buy_price == target_sell_price:
                target_sell_price += 1

            if amount_held > 0:
                curve_percentile_step = (1 - curve_percentile) / amount_held
                sell_percentiles = [curve_percentile + (x * curve_percentile_step) for x in range(amount_held)]
                order_sizes, order_prices = histogram(price_dist.ppf(sell_percentiles), NUM_BUY_ORDERS)

                for order_size, order_price in zip(order_sizes, order_prices):
                    if order_size > 0:
                        if order_price < 1:
                            order_price = 1
                        p.place_sell_order(good, int(order_price), int(order_size), lambda o, f, g=good: self._sell_order_callback(g, o, f) )

            if amount_allocated > 0:
                curve_percentile_step = curve_percentile / max(amount_held, 1)
                buy_percentiles = [curve_percentile - (x * curve_percentile_step) for x in range(max(amount_held, 1))]
                order_sizes, order_prices = histogram(price_dist.ppf(buy_percentiles), NUM_BUY_ORDERS)

                if avg_volume == 0:
                    order_sizes = [1, 1, 1]
                    order_prices = [amount_allocated // 2, amount_allocated // 4, amount_allocated // 8]

                last_order = False
                for order_size, order_price in zip(order_sizes, order_prices):
                    order_price = int(order_price)
                    if order_price < 1 or order_size == 0:
                        continue

                    if amount_allocated == 0:
                        last_order = True
                        break
                    
                    if order_size * order_price > amount_allocated:
                        last_order = True
                        order_size = amount_allocated // order_price
                        if order_size == 0:
                            order_size = 1
                            order_price = amount_allocated

                    p.place_buy_order(good, int(order_price), int(order_size), lambda o, f, g=good: self._buy_order_callback(g, o, f))
                    amount_allocated -= order_price * order_size

                    if last_order:
                        break

                if not last_order:
                    profit_taking = floor(amount_allocated * 0.1)
                    self._allocations[good] -= profit_taking

        p.work_for_government()
