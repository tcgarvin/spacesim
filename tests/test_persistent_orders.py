import pytest

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet

from .helpers import get_actor


@pytest.fixture
def food_commodity():
    """Food commodity."""
    return CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )


def test_order_reservation_system(food_commodity, mock_sim) -> None:
    """Orders reserve money and goods, and matching settles the reservations."""
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food_commodity

    buyer = get_actor("Buyer", mock_sim, initial_money=100)

    order_id = market.place_buy_order(buyer, food_commodity, 5, 10)

    assert buyer.money == 50  # 100 - 5 * 10
    assert buyer.reserved_money == 50
    assert order_id is not None and order_id != ""

    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food_commodity, 10)

    assert seller.inventory.get_quantity(food_commodity) == 10
    assert seller.inventory.get_available_quantity(food_commodity) == 10
    assert seller.inventory.get_reserved_quantity(food_commodity) == 0

    order_id = market.place_sell_order(seller, food_commodity, 5, 8)

    assert seller.inventory.get_quantity(food_commodity) == 10
    assert seller.inventory.get_available_quantity(food_commodity) == 5
    assert seller.inventory.get_reserved_quantity(food_commodity) == 5
    assert order_id is not None and order_id != ""

    market.match_orders()

    assert buyer.money == 60  # 50 + refund of 5 * (10 - 8)
    assert buyer.reserved_money == 0
    assert buyer.inventory.get_quantity(food_commodity) == 5

    assert seller.money == 90  # default 50 + 5 * 8
    assert seller.inventory.get_quantity(food_commodity) == 5
    assert seller.inventory.get_available_quantity(food_commodity) == 5
    assert seller.inventory.get_reserved_quantity(food_commodity) == 0


def test_cancel_order(food_commodity, mock_sim) -> None:
    """Cancelling an order removes it and releases the reserved resources."""
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food_commodity

    buyer = get_actor("Buyer", mock_sim, initial_money=100)

    order_id = market.place_buy_order(buyer, food_commodity, 5, 10)

    assert buyer.money == 50
    assert buyer.reserved_money == 50

    assert market.cancel_order(order_id)

    assert buyer.money == 100
    assert buyer.reserved_money == 0

    assert len(market.buy_orders.get(food_commodity, [])) == 0
    assert order_id not in market.orders_by_id

    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food_commodity, 10)

    order_id = market.place_sell_order(seller, food_commodity, 5, 8)

    assert seller.inventory.get_available_quantity(food_commodity) == 5
    assert seller.inventory.get_reserved_quantity(food_commodity) == 5

    assert market.cancel_order(order_id)

    assert seller.inventory.get_available_quantity(food_commodity) == 10
    assert seller.inventory.get_reserved_quantity(food_commodity) == 0


def test_order_persistence(food_commodity, mock_sim) -> None:
    """Unmatched orders rest in the book until a later match fills them."""
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food_commodity

    buyer = get_actor("Buyer", mock_sim, initial_money=100)
    seller = get_actor("Seller", mock_sim)
    seller.inventory.add_commodity(food_commodity, 10)

    market.place_buy_order(buyer, food_commodity, 5, 7)
    market.place_sell_order(seller, food_commodity, 5, 10)

    market.match_orders()

    assert len(market.buy_orders[food_commodity]) == 1
    assert len(market.sell_orders[food_commodity]) == 1

    # Repost the buyer's order at the matching price.
    buy_order = market.buy_orders[food_commodity][0]
    market.cancel_order(buy_order.order_id)
    market.place_buy_order(buyer, food_commodity, 5, 10)

    market.match_orders()

    assert len(market.transaction_history) == 1
    assert market.transaction_history[0].price == 10

    assert len(market.buy_orders.get(food_commodity, [])) == 0
    assert len(market.sell_orders.get(food_commodity, [])) == 0


def test_actor_order_tracking(food_commodity, mock_sim) -> None:
    """Actor and market both track an order until it is cancelled."""
    market = Market()
    market.commodity_registry = CommodityRegistry()
    market.commodity_registry._commodities["food"] = food_commodity

    buyer = get_actor("Buyer", mock_sim, initial_money=100)

    order_id = market.place_buy_order(buyer, food_commodity, 5, 10)

    assert order_id in buyer.active_orders
    assert buyer.active_orders[order_id] == f"buy {food_commodity.id}"

    actor_orders = market.get_actor_orders(buyer)
    assert len(actor_orders["buy"]) == 1
    assert len(actor_orders["sell"]) == 0

    market.cancel_order(order_id)

    assert order_id not in buyer.active_orders

    actor_orders = market.get_actor_orders(buyer)
    assert len(actor_orders["buy"]) == 0


def test_integrated_market_simulation(mock_sim) -> None:
    """Reservations clear and goods and money move after a matched trade."""
    market = Market()

    commodity_registry = CommodityRegistry()
    food_commodity = CommodityDefinition(
        id="food",
        name="Food",
        transportable=True,
        description="Basic nourishment required by actors.",
    )
    fuel_commodity = CommodityDefinition(
        id="nova_fuel",
        name="NovaFuel",
        transportable=True,
        description="High-density energy source for starship travel.",
    )
    commodity_registry._commodities["food"] = food_commodity
    commodity_registry._commodities["nova_fuel"] = fuel_commodity
    market.commodity_registry = commodity_registry

    planet = Planet("Test Planet", market)

    seller = get_actor("Seller", mock_sim, planet=planet)
    seller.inventory.add_commodity(food_commodity, 20)
    seller.sim = type(  # type: ignore[assignment]  # stand-in sim
        "obj",
        (object,),
        {
            "commodity_registry": commodity_registry,
        },
    )

    buyer = get_actor("Buyer", mock_sim, planet=planet, initial_money=200)
    buyer.sim = type(  # type: ignore[assignment]  # stand-in sim
        "obj",
        (object,),
        {
            "commodity_registry": commodity_registry,
        },
    )

    sell_order_id = market.place_sell_order(seller, food_commodity, 5, 10)
    buy_order_id = market.place_buy_order(buyer, food_commodity, 5, 10)

    assert sell_order_id is not None
    assert buy_order_id is not None

    assert seller.inventory.get_reserved_quantity(food_commodity) == 5

    assert buyer.reserved_money == 5 * 10

    market.match_orders()

    assert len(market.transaction_history) > 0

    assert seller.inventory.get_reserved_quantity(food_commodity) == 0
    assert buyer.reserved_money == 0

    assert buyer.inventory.get_quantity(food_commodity) == 5

    assert seller.money == 50 + 50  # default 50 + 5 * 10 from the sale
