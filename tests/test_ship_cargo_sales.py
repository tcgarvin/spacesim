"""Tests for realizing cargo revenue (R5).

- The ship tracks what its cargo cost from its own fills and floors its asks
  there, marking the floor down for every docked turn the cargo goes unsold.
- Asks ladder into every resting bid the floor allows and the remainder never
  rests above the best bid.
- Cargo is valued at what the book would actually pay for the whole load, a
  load may make one hop before it sells where it is, and a hold nobody will
  let leave is listed after HOLD_PATIENCE refused departures.
"""

from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.galaxy import StarLaneNetwork
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.ship import (
    ACCUMULATION_PATIENCE,
    HOLD_PATIENCE,
    LIQUIDATION_FLOOR,
    MAX_CARGO_HOPS,
    UNSOLD_DECAY,
    Ship,
    TradePlan,
    units_to_milli,
)


def _make_world(planet_specs):
    """Build a nova_fuel and food registry, planets with markets, and a mock sim."""
    registry = CommodityRegistry()
    fuel = CommodityDefinition(
        id="nova_fuel",
        name="NovaFuel",
        transportable=True,
        description="High-density energy source for starship travel.",
    )
    food = CommodityDefinition(
        id="food", name="Food", transportable=True, description="Basic sustenance."
    )
    registry.add_commodity(fuel)
    registry.add_commodity(food)
    planets = [Planet(name, Market(), x, y) for name, x, y in planet_specs]
    sim = type(
        "MockSim",
        (object,),
        {
            "commodity_registry": registry,
            "planets": planets,
            "star_lanes": StarLaneNetwork.complete(planets),
            "current_turn": 0,
        },
    )()
    return sim, fuel, food, planets


def _make_ship(sim, planet, fuel_units=0, money=1000, name="TestShip", hold_fuel=0):
    ship = Ship(name, sim, planet, initial_money=money, fuel_efficiency=1.0)
    planet.add_ship(ship)
    ship.fuel = units_to_milli(fuel_units)
    if hold_fuel:
        ship.cargo.add_commodity(
            sim.commodity_registry.get_commodity("nova_fuel"), hold_fuel
        )
    ship.check_maintenance = lambda: False  # deterministic departures
    return ship


def _record_flow(market, commodity, price, volume):
    """Give ``commodity`` a believable clearing price and recent volume."""
    market.last_traded_prices[commodity] = [price]
    market.price_history[commodity] = [price]
    market.volume_history[commodity] = [volume]
    market._clear_history_read_caches()


# ---------------------------------------------------------------------------
# R5.1: cost basis and the decaying floor
# ---------------------------------------------------------------------------


def test_cost_basis_follows_the_ships_own_buys_and_sells():
    """Buys raise the basis, sells remove units at the average cost."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    seller = _make_ship(sim, a, money=0, name="Seller")
    seller.cargo.add_commodity(food, 100)
    ship = _make_ship(sim, a, money=5000, name="Trader")

    a.market.place_sell_order(seller, food, 10, 20)
    a.market.place_buy_order(ship, food, 10, 20)
    a.market.match_orders()
    ship.brain._ingest_cost_basis()

    assert ship.cargo.get_quantity(food) == 10
    assert ship.brain.basis_per_unit(food) == 20.0

    # A second lot at a different price averages in.
    a.market.place_sell_order(seller, food, 10, 40)
    a.market.place_buy_order(ship, food, 10, 40)
    a.market.match_orders()
    ship.brain._ingest_cost_basis()

    assert ship.brain.basis_per_unit(food) == 30.0

    # Selling removes units at that average and leaves the rest priced the same.
    buyer = _make_ship(sim, a, money=5000, name="Buyer")
    a.market.place_buy_order(buyer, food, 5, 50)
    a.market.place_sell_order(ship, food, 5, 50)
    a.market.match_orders()
    ship.brain._ingest_cost_basis()

    assert ship.cargo.get_quantity(food) == 15
    assert ship.brain.basis_per_unit(food) == 30.0


def test_basis_is_unknown_for_cargo_the_ship_never_bought():
    """Cargo that was never filled on a market has no floor."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 5)

    assert ship.brain.basis_per_unit(food) is None
    assert ship.brain._sell_floor_price(food) == 0


def test_sell_floor_decays_with_unsold_turns_down_to_the_liquidation_floor():
    """Every unfilled docked turn marks the floor down, never below 40% of cost."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 10)
    ship.brain._cost_basis["food"] = (10, 1000)

    assert ship.brain._sell_floor_price(food) == 100

    for turns, expected in ((1, 90), (3, 70), (6, 40)):
        ship.brain._unsold_turns["food"] = turns
        assert ship.brain._sell_floor_price(food) == expected
        assert expected >= 100 * LIQUIDATION_FLOOR

    # Past the liquidation floor the markdown stops.
    ship.brain._unsold_turns["food"] = 20
    assert ship.brain._sell_floor_price(food) == int(100 * LIQUIDATION_FLOOR)
    assert UNSOLD_DECAY == 0.1


def test_unsold_turns_count_only_listings_that_did_not_fill():
    """The clock runs while the ask sits and restarts on a fill."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 10)

    ship.brain._note_listed([food])
    ship.brain._age_unsold_listings()
    assert ship.brain._unsold_turns["food"] == 1

    ship.brain._note_listed([food])
    ship.brain._age_unsold_listings()
    assert ship.brain._unsold_turns["food"] == 2

    # A fill: fewer units held than when the ask went in.
    ship.brain._note_listed([food])
    ship.cargo.remove_commodity(food, 4)
    ship.brain._age_unsold_listings()
    assert ship.brain._unsold_turns["food"] == 2  # not aged further

    buyer = _make_ship(sim, a, money=5000, name="Buyer")
    a.market.place_buy_order(buyer, food, 6, 50)
    a.market.place_sell_order(ship, food, 6, 50)
    a.market.match_orders()
    ship.brain._ingest_cost_basis()
    assert "food" not in ship.brain._unsold_turns


# ---------------------------------------------------------------------------
# R5.2: sell pricing
# ---------------------------------------------------------------------------


def test_sell_orders_ladder_into_every_bid_level_above_the_floor():
    """Each resting bid at or above the floor gets its own ask; the rest is skipped."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 12)
    ship.brain._cost_basis["food"] = (12, 12 * 30)  # basis 30, so the floor is 30
    _record_flow(a.market, food, 100, 5)  # flow price 90, far above every bid

    buyer = _make_ship(sim, a, money=50000, name="Buyer")
    a.market.place_buy_order(buyer, food, 2, 60)
    a.market.place_buy_order(buyer, food, 3, 40)
    a.market.place_buy_order(buyer, food, 4, 10)  # below the floor

    ship.brain._place_flow_sell_orders(a.market, food, 12)

    asks = sorted(
        (o.price, o.quantity) for o in a.market.sell_orders[food] if o.actor is ship
    )
    # Two levels taken at their own price, the remainder resting at the best
    # bid rather than at the flow price.
    assert asks == [(40, 3), (60, 2), (60, 7)]
    assert sum(quantity for _price, quantity in asks) == 12


def test_remainder_rests_at_the_best_bid_when_the_flow_price_is_higher():
    """An ask above every live bid is a forecast, not a sale."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 10)
    _record_flow(a.market, food, 100, 5)
    buyer = _make_ship(sim, a, money=50000, name="Buyer")
    a.market.place_buy_order(buyer, food, 2, 30)

    ship.brain._place_flow_sell_orders(a.market, food, 10)

    asks = [o for o in a.market.sell_orders[food] if o.actor is ship]
    assert max(o.price for o in asks) == 30
    assert sum(o.quantity for o in asks) == 10


def test_remainder_never_rests_above_the_flow_price():
    """With a bid above the flow price the remainder still rests at the flow price."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    ship.cargo.add_commodity(food, 10)
    _record_flow(a.market, food, 20, 5)  # flow price 18
    buyer = _make_ship(sim, a, money=50000, name="Buyer")
    a.market.place_buy_order(buyer, food, 2, 60)

    ship.brain._place_flow_sell_orders(a.market, food, 10)

    asks = sorted(
        (o.price, o.quantity) for o in a.market.sell_orders[food] if o.actor is ship
    )
    assert asks == [(18, 8), (60, 2)]


# ---------------------------------------------------------------------------
# R5.3: realizable value and hop discipline
# ---------------------------------------------------------------------------


def test_realizable_value_walks_bid_depth_and_prices_the_remainder():
    """Depth is paid at each level; the rest is worth the lower of flow and best bid."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")
    _record_flow(a.market, food, 20, 5)  # flow price 18
    buyer = _make_ship(sim, a, money=50000, name="Buyer")
    a.market.place_buy_order(buyer, food, 2, 60)
    a.market.place_buy_order(buyer, food, 3, 30)

    # Inside the book: the levels themselves.
    assert ship.brain._realizable_value(a.market, food, 2) == 120
    assert ship.brain._realizable_value(a.market, food, 5) == 120 + 90
    # Beyond it: the remainder at min(flow, best bid) = 18.
    assert ship.brain._realizable_value(a.market, food, 8) == 210 + 3 * 18
    # The old valuation would have paid the top bid for every unit.
    assert ship.brain._realizable_value(a.market, food, 8) < 8 * 60


def test_realizable_value_is_zero_without_a_bid_or_a_flow_price():
    """A market with no buyer and no history is worth nothing to sell into."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, name="Trader")

    assert ship.brain._realizable_value(a.market, food, 10) == 0


def test_cargo_that_already_hopped_once_is_sold_where_it_is():
    """The second stop sells, however much a third market forecasts."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 5)
    supplier_b = _make_ship(sim, b, hold_fuel=400, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 200, 5)

    local_buyer = _make_ship(sim, a, money=20000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, b, money=20000, name="RemoteBuyer")
    b.market.place_buy_order(remote_buyer, food, 20, 100)

    ship = _make_ship(sim, a, fuel_units=6, money=2000, name="Hopper")
    ship.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts()

    # A fresh load is worth flying to B.
    assert ship.brain._cargo_disposition(a.market)[0] is False

    # After one hop it sells where it stands.
    ship.brain._cargo_hops["food"] = MAX_CARGO_HOPS
    assert ship.brain._cargo_disposition(a.market) == (True, 0)


def test_a_cargo_hop_departure_counts_against_the_hop_limit():
    """decide_travel records the hop it just chose to fly."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 5)
    supplier_b = _make_ship(sim, b, hold_fuel=400, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 200, 5)
    remote_buyer = _make_ship(sim, b, money=20000, name="RemoteBuyer")
    b.market.place_buy_order(remote_buyer, food, 20, 100)

    ship = _make_ship(sim, a, fuel_units=6, money=2000, name="Hopper")
    ship.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts()

    assert ship.brain.decide_travel() is b
    assert ship.brain._cargo_hops["food"] == 1


def test_hold_patience_lists_the_cargo_after_refused_departures():
    """Three refused departures and the cargo goes in the local book."""
    sim, fuel, food, (a, c) = _make_world([("A", 0, 0), ("C", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 5)
    local_buyer = _make_ship(sim, a, money=20000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, c, money=20000, name="RemoteBuyer")
    c.market.place_buy_order(remote_buyer, food, 20, 100)

    # Money to buy the leg, so the trip to C is worth holding for, but no
    # fuel ever reaches the tank because the orders are never matched: every
    # departure is refused and the cargo would otherwise sit unlisted.
    ship = _make_ship(sim, a, fuel_units=0, money=3000, name="Holder")
    ship.cargo.add_commodity(food, 10)

    for turn in range(HOLD_PATIENCE):
        ship.brain.decide_trade_actions()
        assert ship.brain.decide_travel() is None
        assert not [o for o in a.market.sell_orders[food] if o.actor is ship], turn

    ship.brain.decide_trade_actions()

    assert ship.brain._hold_refused_turns == HOLD_PATIENCE
    assert [o for o in a.market.sell_orders[food] if o.actor is ship]


# ---------------------------------------------------------------------------
# R5.4: always listed
# ---------------------------------------------------------------------------


def test_docked_cargo_with_no_plan_is_always_listed():
    """A ship that is neither accumulating nor holding for elsewhere lists."""
    sim, _fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, fuel_units=0, money=0, name="Idle")
    ship.cargo.add_commodity(food, 10)
    _record_flow(a.market, food, 20, 5)

    ship.brain.decide_trade_actions()

    asks = [o for o in a.market.sell_orders[food] if o.actor is ship]
    assert sum(o.quantity for o in asks) == 10
    assert ship.brain._current_plan is None


def test_a_loaded_plan_that_cannot_depart_lists_its_hold():
    """Cargo waiting on fuel it cannot buy is offered where the ship sits."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_b = _make_ship(sim, b, hold_fuel=400, name="SupplierB")
    b.market.place_sell_order(supplier_b, fuel, 200, 5)
    buyer_b = _make_ship(sim, b, money=20000, name="BuyerB")
    b.market.place_buy_order(buyer_b, food, 20, 100)
    local_buyer = _make_ship(sim, a, money=20000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 5, 30)

    ship = _make_ship(sim, a, fuel_units=0, money=0, name="Grounded")
    ship.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts()
    plan = TradePlan(
        origin=a,
        destination=b,
        commodity=food,
        quantity=10,
        bid_price_per_unit=20,
        purchase_price_per_unit=20,
        expected_sell_price_per_unit=100,
        distance=ship.route_distance(a, b),
        fuel_needed_one_way=ship.fuel_required(ship.route_distance(a, b)),
        fuel_price_at_origin=5,
        fuel_units_from_tank=0,
        fuel_price_from_tank=5,
    )
    ship.brain._current_plan = plan
    ship.brain._plan_loaded = True
    ship.brain._plan_turns_left = ACCUMULATION_PATIENCE

    assert ship.brain._plan_departure_blocked(plan)

    ship.brain.decide_trade_actions()

    assert [o for o in a.market.sell_orders[food] if o.actor is ship]
    # The ask must not pin the ship here: the plan may still fly.
    assert ship.brain._selling_locally is False
