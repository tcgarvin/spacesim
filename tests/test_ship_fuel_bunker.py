"""Bunker buying: bid price, budget tiers, and lingering to fill the tank.

- A fuel bid is posted at max(local ask, recent clearing price), so it wins
  units out of the turn's flow instead of only the resting book.
- The larger bunker budget applies only when the ask is at or below the
  galaxy fuel reference.
- A ship ready to depart may stay one extra docked turn to buy cheap fuel,
  but not with a full-enough tank, not at a spiked ask, and not twice at the
  same stop.
"""

from spacesim.core.ship import (
    ACCUMULATION_PATIENCE,
    FUEL_BUNKER_BUDGET_FRACTION,
    FUEL_BUNKER_BUDGET_FRACTION_CHEAP,
    FUEL_BUNKER_TARGET_FLOOR,
    FUEL_LINGER_TARGET_FRACTION,
    TradePlan,
)
from tests.test_ship_fuel import _make_ship, _make_world

THREE_PLANETS = [("A", 0, 0), ("B", 100, 0), ("C", 200, 0)]
# Bunkering is sized against the bunker target, not the tank. Three mean
# round trips of this chain come to 4002 millifuel, under the floor, so the
# target here is the floor: 6 market units.
BUNKER_TARGET = FUEL_BUNKER_TARGET_FLOOR
# A third of the target: below FUEL_LINGER_TARGET_FRACTION, so there is room
# worth lingering for.
LOW_TANK = BUNKER_TARGET // 3
# Two thirds: above it.
FULL_ENOUGH_TANK = 2 * BUNKER_TARGET // 3
# Purse for the budget-tier tests. A leg of 100 costs 500 millifuel, so the
# survival target of two round trips is two market units and the rest of the
# 6-unit target is room the budget, not the target, has to ration: at an ask
# of 10 both tiers must afford fewer than the 4 units of room left.
BUNKER_TEST_MONEY = 60


def _sell_fuel(sim, planet, price, quantity=100, name=None):
    """Rest a fuel ask of ``quantity`` units at ``planet``."""
    fuel = sim.commodity_registry.get_commodity("nova_fuel")
    supplier = _make_ship(
        sim, planet, hold_fuel=quantity * 2, name=name or f"Supplier{planet.name}"
    )
    planet.market.place_sell_order(supplier, fuel, quantity, price)
    return supplier


def _fuel_buys(planet, fuel, ship):
    return [o for o in planet.market.buy_orders[fuel] if o.actor is ship]


# ---------------------------------------------------------------------------
# Bid price
# ---------------------------------------------------------------------------


def test_bunker_bid_posts_at_the_flow_price_above_the_ask():
    """The order rests at the recent clearing price when it beats the ask.

    Matching executes at the seller's ask and refunds the difference, so the
    higher bid costs nothing on the units resting now and also wins units out
    of the turn's flow of fresh asks.
    """
    sim, fuel, _, (a, b, c) = _make_world(THREE_PLANETS)
    a.market.last_traded_prices[fuel] = [20] * 5
    _sell_fuel(sim, a, 8)
    _sell_fuel(sim, b, 20)
    _sell_fuel(sim, c, 20)

    ship = _make_ship(sim, a, fuel_units=0, money=500, name="Trader")
    ship.brain._nav.refresh_market_facts(turn=0)
    assert ship.brain._flow_value(a.market, fuel) == 20

    assert ship.brain._opportunistic_fuel_topup() is not None
    buys = _fuel_buys(a, fuel, ship)
    assert len(buys) == 1
    assert buys[0].price == 20


def test_bunker_bid_stays_at_the_ask_when_the_ask_is_higher():
    """A stale low clearing price never drags the bid below the live ask."""
    sim, fuel, _, (a, b, c) = _make_world(THREE_PLANETS)
    a.market.last_traded_prices[fuel] = [4] * 5
    _sell_fuel(sim, a, 10)
    _sell_fuel(sim, b, 10)
    _sell_fuel(sim, c, 10)

    ship = _make_ship(sim, a, fuel_units=0, money=500, name="Trader")
    ship.brain._nav.refresh_market_facts(turn=0)

    assert ship.brain._opportunistic_fuel_topup() is not None
    buys = _fuel_buys(a, fuel, ship)
    assert len(buys) == 1
    assert buys[0].price == 10


# ---------------------------------------------------------------------------
# Two-tier bunker budget
# ---------------------------------------------------------------------------


def _bunker_quantity_at_reference(remote_ask):
    """Units bought at a local ask of 10 with the galaxy asking ``remote_ask``."""
    sim, fuel, _, (a, b, c) = _make_world(THREE_PLANETS)
    _sell_fuel(sim, a, 10)
    _sell_fuel(sim, b, remote_ask)
    _sell_fuel(sim, c, remote_ask)

    ship = _make_ship(sim, a, fuel_units=0, money=BUNKER_TEST_MONEY, name="Trader")
    ship.brain._nav.refresh_market_facts(turn=0)
    # Two round trips of the 100-unit lane: 4 * 500 millifuel, so two whole
    # market units.
    assert ship.brain._fuel_survival_target() == 2000
    assert ship.brain._opportunistic_fuel_topup() is not None
    buys = _fuel_buys(a, fuel, ship)
    assert len(buys) == 1
    assert buys[0].price == 10
    return buys[0].quantity


def test_cheap_tier_budget_applies_at_or_below_the_reference():
    """An ask at the reference spends the larger fraction of cash on fuel."""
    quantity = _bunker_quantity_at_reference(remote_ask=10)
    required = 2  # the survival target in whole units, bought before bunkering
    budget = int(BUNKER_TEST_MONEY * FUEL_BUNKER_BUDGET_FRACTION_CHEAP)
    assert quantity == required + (budget - required * 10) // 10
    assert quantity == 4


def test_normal_tier_budget_applies_above_the_reference():
    """The same ask above the reference, still inside the premium, spends less.

    Only the reference moves between this and the cheap-tier case: the local
    ask is 10 in both, so the difference is the budget fraction alone.
    """
    quantity = _bunker_quantity_at_reference(remote_ask=9)
    required = 2
    budget = int(BUNKER_TEST_MONEY * FUEL_BUNKER_BUDGET_FRACTION)
    assert quantity == required + (budget - required * 10) // 10
    assert quantity == 3


# ---------------------------------------------------------------------------
# Lingering to fill the tank
# ---------------------------------------------------------------------------


def _cargo_world(local_fuel_ask, tank):
    """A ship at A holding food that B bids for, with cheap fuel resting at A."""
    sim, fuel, food, (a, b, c) = _make_world(THREE_PLANETS)
    _sell_fuel(sim, a, local_fuel_ask)
    _sell_fuel(sim, b, 20)
    _sell_fuel(sim, c, 20)
    buyer = _make_ship(sim, b, money=5000, name="Buyer")
    b.market.place_buy_order(buyer, food, 10, 20)

    ship = _make_ship(sim, a, fuel_milli=tank, money=1000, name="Trader")
    ship.cargo.add_commodity(food, 10)
    ship.brain._nav.refresh_market_facts(turn=0)
    return sim, fuel, food, a, b, ship


def test_ship_lingers_when_cheap_fuel_beats_a_turn_of_the_trip():
    """Cheap fuel and a low-value trip: stay docked one more turn."""
    _, _, _, a, b, ship = _cargo_world(local_fuel_ask=5, tank=LOW_TANK)
    assert ship.fuel < FUEL_LINGER_TARGET_FRACTION * ship.brain._fuel_bunker_target()

    assert ship.brain.decide_travel() is None
    assert ship.brain._fuel_linger_pending is True
    assert ship.brain._fuel_linger_planet is a


def test_no_linger_with_a_full_enough_tank():
    """Above the fill threshold there is too little room left to be worth it."""
    _, _, _, _, b, ship = _cargo_world(local_fuel_ask=5, tank=FULL_ENOUGH_TANK)
    assert ship.fuel >= FUEL_LINGER_TARGET_FRACTION * ship.brain._fuel_bunker_target()

    assert ship.brain.decide_travel() is b
    assert ship.brain._fuel_linger_pending is False


def test_no_linger_at_a_spiked_ask():
    """A local ask outside the bunker premium is not worth waiting for."""
    _, _, _, _, b, ship = _cargo_world(local_fuel_ask=50, tank=LOW_TANK)

    assert ship.brain.decide_travel() is b
    assert ship.brain._fuel_linger_pending is False


def test_no_second_linger_at_the_same_stop():
    """One linger turn per stop, however cheap the fuel stays."""
    _, _, _, a, b, ship = _cargo_world(local_fuel_ask=5, tank=LOW_TANK)
    ship.brain._fuel_linger_planet = a

    assert ship.brain.decide_travel() is b
    assert ship.brain._fuel_linger_pending is False


def test_linger_buys_fuel_without_spending_a_loaded_plan_s_patience():
    """A lingering loaded plan re-posts its bunker bid and keeps its patience.

    The extra turn exists to buy fuel, so it must not count against the
    departure patience that releases a loaded plan's cargo, and the ship must
    actually have a fuel bid resting for the turn to be worth anything.
    """
    sim, fuel, food, a, b, ship = _cargo_world(local_fuel_ask=5, tank=LOW_TANK)
    plan = TradePlan(
        origin=a,
        destination=b,
        commodity=food,
        quantity=10,
        bid_price_per_unit=10,
        purchase_price_per_unit=10,
        expected_sell_price_per_unit=30,
        distance=100.0,
        fuel_needed_one_way=500,
        fuel_price_at_origin=5,
        fuel_units_from_tank=500,
        fuel_price_from_tank=20,
    )
    ship.brain._current_plan = plan
    ship.brain._plan_loaded = True
    ship.brain._plan_turns_left = ACCUMULATION_PATIENCE

    assert ship.brain.decide_travel() is None
    assert ship.brain._fuel_linger_pending is True

    ship.brain.decide_trade_actions()
    assert ship.brain._plan_turns_left == ACCUMULATION_PATIENCE
    assert ship.brain._current_plan is plan
    buys = _fuel_buys(a, fuel, ship)
    assert len(buys) == 1
    assert buys[0].quantity > 0

    # The linger is spent: the next travel decision departs.
    assert ship.brain._fuel_linger_pending is False
    assert ship.brain.decide_travel() is b
