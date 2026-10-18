"""Tests for the idle-ship livelock fixes in ``TraderBrain``.

Mechanisms that used to keep a ship with cargo docked forever:

- the "selling locally" travel veto, which was meant to protect the sell
  orders placed this turn but never expired,
- the hold-or-sell comparison judging destinations on the fuel in the tank
  alone, which rejected almost every destination for a ship running near its
  survival fuel target, and
- committing to only the outbound leg of fuel, which the departure gate then
  refused for the arrival reserve the ship had never funded.

Plus the hazards the escape from that livelock opened: replanning in the
same turn as a local sale must not have the ship bidding for its own cargo,
and the staleness clock must turn on fills, not on the sellable total.
"""

from types import SimpleNamespace

from spacesim2.core.commodity import CommodityDefinition
from spacesim2.core.ship import (
    DISTRESS_PATIENCE,
    ShipStatus,
    TradePlan,
    _RepositionIntent,
    milli_to_units_ceil,
    units_to_milli,
)
from tests.test_ship_fuel import _give_repair_kit, _make_ship, _make_world


def _add_commodity(sim, commodity_id, name):
    """Register one more tradeable good in a test world."""
    commodity = CommodityDefinition(
        id=commodity_id, name=name, transportable=True, description=name
    )
    sim.commodity_registry.add_commodity(commodity)
    return commodity


# ---------------------------------------------------------------------------
# The local-sale veto lasts one turn
# ---------------------------------------------------------------------------


def test_local_sale_veto_lifts_after_an_unfilled_turn():
    """Asks that sat a whole turn untouched stop pinning the ship in place."""
    sim, _fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, b, money=5000, name="RemoteBuyer")
    # Only 10% better, so the hold-or-sell test lists the cargo here, but
    # decide_travel's own comparison still nets out in B's favor.
    b.market.place_buy_order(remote_buyer, food, 20, 11)

    ship = _make_ship(sim, a, fuel_units=10, name="Seller")
    ship.cargo.add_commodity(food, 10)

    # Turn 1: the sale is fresh, so the ship stays to see it fill.
    ship.brain.decide_trade_actions()
    assert any(o.actor is ship for o in a.market.sell_orders[food])
    assert ship.brain.decide_travel() is None

    # Turn 2, with nothing matched in between: the ask went a full turn
    # unfilled, so it no longer earns the ship's patience.
    ship.brain.decide_trade_actions()
    assert ship.brain._local_sale_stale
    assert ship.brain.decide_travel() is b


def test_local_sale_veto_returns_after_a_fill():
    """A sale that actually fills restarts the one-turn grace period."""
    sim, _fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(buyer, food, 4, 10)  # takes part of the load
    # B pays too little to be worth the trip, so the load is sold here.
    remote_buyer = _make_ship(sim, b, money=5000, name="RemoteBuyer")
    b.market.place_buy_order(remote_buyer, food, 20, 3)

    ship = _make_ship(sim, a, fuel_units=10, name="Seller")
    ship.cargo.add_commodity(food, 10)

    ship.brain.decide_trade_actions()
    a.market.match_orders()
    assert 0 < ship.cargo.get_quantity(food) < 10

    ship.brain.decide_trade_actions()
    assert not ship.brain._local_sale_stale
    assert ship.brain.decide_travel() is None


def test_stale_local_sale_allows_replanning():
    """Cargo nobody will buy no longer blocks adopting a new trade plan."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    # Food is for sale at A and pays well at B: a plan exists to be found.
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    buyer_b = _make_ship(sim, b, money=9000, name="BuyerB")
    b.market.place_buy_order(buyer_b, food, 50, 40)

    # The trader holds a load of an unrelated good with no demand anywhere,
    # so it lists it here and it never fills.
    junk = _add_commodity(sim, "junk", "Junk")
    ship = _make_ship(sim, a, fuel_units=20, money=4000, name="Trader")
    ship.cargo.add_commodity(junk, 5)

    ship.brain.decide_trade_actions()
    ship.brain.decide_trade_actions()

    assert ship.brain._local_sale_stale
    assert ship.brain._current_plan is not None
    assert ship.brain._current_plan.destination is b


# ---------------------------------------------------------------------------
# Reachability counts fuel the ship could buy here
# ---------------------------------------------------------------------------


def test_hold_considers_destinations_reachable_on_purchasable_fuel():
    """A destination out of reach on tank fuel alone still counts if fuel sells here."""
    sim, fuel, food, (a, _b, c) = _make_world(
        [("A", 0, 0), ("B", 20, 0), ("C", 200, 0)]
    )
    # Fuel is for sale at both ends, so C is never a fuel dead end.
    supplier_a = _make_ship(sim, a, hold_fuel=400, name="SupplierA")
    a.market.place_sell_order(supplier_a, fuel, 200, 5)
    supplier_c = _make_ship(sim, c, hold_fuel=400, name="SupplierC")
    c.market.place_sell_order(supplier_c, fuel, 200, 5)

    local_buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, c, money=20000, name="RemoteBuyer")
    c.market.place_buy_order(remote_buyer, food, 20, 100)

    ship = _make_ship(sim, a, fuel_units=5, money=2000, name="Holder")
    ship.cargo.add_commodity(food, 10)
    leg = ship.fuel_required(ship.route_distance(a, c))
    assert leg > 5  # out of reach on what is in the tank

    ship.brain.decide_trade_actions()

    # It holds the cargo for C instead of dumping it into the local book...
    assert not any(o.actor is ship for o in a.market.sell_orders[food])
    required = ship.brain._departure_fuel_requirement(c)
    assert required >= leg
    assert ship.brain._committed_fuel_need == required
    # ...and buys enough fuel to actually fly there.
    bought = sum(o.quantity for o in a.market.buy_orders[fuel] if o.actor is ship)
    assert ship.fuel + bought >= required


def test_committed_destination_fuel_is_bought_at_spike_prices():
    """Fuel for a committed profitable trip is not speculative bunkering."""
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    # Cheap fuel at B sets the galaxy reference near 10; the local ask spikes.
    remote_supplier = _make_ship(sim, b, hold_fuel=200, name="RemoteSupplier")
    b.market.place_sell_order(remote_supplier, fuel, 100, 10)
    local_supplier = _make_ship(sim, a, hold_fuel=200, name="LocalSupplier")
    a.market.place_sell_order(local_supplier, fuel, 100, 60)

    ship = _make_ship(sim, a, fuel_units=0, money=5000, name="Trader")
    committed = ship.brain._fuel_survival_target() + units_to_milli(3)
    ship.brain._committed_fuel_need = committed

    assert ship.brain._opportunistic_fuel_topup() is not None
    buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert len(buys) == 1
    # The commitment is millifuel; the bid is for the whole units covering it.
    assert buys[0].quantity == milli_to_units_ceil(committed)


def test_committed_fuel_need_covers_the_departure_gate_and_the_ship_leaves():
    """What the hold commits to is what decide_travel demands, so it departs."""
    sim, fuel, food, (a, c) = _make_world([("A", 0, 0), ("C", 100, 0)])
    # Fuel sells at A only, so C's escape route has to be carried there.
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 5)
    local_buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, c, money=20000, name="RemoteBuyer")
    c.market.place_buy_order(remote_buyer, food, 20, 100)

    ship = _make_ship(sim, a, fuel_units=0, money=3000, name="Holder")
    ship.cargo.add_commodity(food, 10)
    leg = ship.fuel_required(ship.route_distance(a, c))

    ship.brain.decide_trade_actions()

    # The commitment carries the leg plus the escape fuel C will demand on
    # arrival, which is what the departure gate checks.
    assert ship.brain._committed_fuel_need > leg
    assert ship.brain._committed_fuel_need == ship.brain._departure_fuel_requirement(c)
    assert ship.brain.decide_travel() is None  # the fuel has not arrived yet

    a.market.match_orders()
    ship.pump_fuel()
    assert ship.fuel >= ship.brain._committed_fuel_need

    # Next turn, with the fuel aboard, it goes.
    ship.brain.decide_trade_actions()
    assert ship.brain.decide_travel() is c


def test_committed_fuel_need_is_cleared_once_the_ship_moves_on():
    """A commitment never outlives the turn that made it."""
    sim, fuel, food, (a, c) = _make_world([("A", 0, 0), ("C", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 5)
    local_buyer = _make_ship(sim, a, money=2000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, food, 20, 10)
    remote_buyer = _make_ship(sim, c, money=20000, name="RemoteBuyer")
    c.market.place_buy_order(remote_buyer, food, 20, 100)

    ship = _make_ship(sim, a, fuel_units=0, money=3000, name="Holder")
    ship.cargo.add_commodity(food, 10)

    ship.brain.decide_trade_actions()
    assert ship.brain._committed_fuel_need > 0

    a.market.match_orders()
    ship.pump_fuel()
    assert ship.start_journey(c)
    # Arrive.
    a.ships.remove(ship)
    ship.status = ShipStatus.DOCKED
    ship.planet = c
    ship.destination = None
    c.add_ship(ship)

    ship.brain.decide_trade_actions()
    # Arrived and selling into C's book: nothing is committed here.
    assert ship.brain._committed_fuel_need == 0


# ---------------------------------------------------------------------------
# Replanning never bids for the ship's own cargo
# ---------------------------------------------------------------------------


def test_replan_does_not_bid_for_the_good_it_is_listing():
    """A ship listing wood here must not adopt a plan to buy wood here."""
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    wood = _add_commodity(sim, "wood", "Wood")
    supplier = _make_ship(sim, a, hold_fuel=400, name="Supplier")
    supplier.cargo.add_commodity(wood, 40)
    a.market.place_sell_order(supplier, fuel, 200, 5)
    a.market.place_sell_order(supplier, wood, 40, 10)  # a cheap export exists
    local_buyer = _make_ship(sim, a, money=5000, name="LocalBuyer")
    a.market.place_buy_order(local_buyer, wood, 20, 35)
    remote_buyer = _make_ship(sim, b, money=20000, name="RemoteBuyer")
    b.market.place_buy_order(remote_buyer, wood, 40, 40)  # too thin a margin to hold

    ship = _make_ship(sim, a, fuel_units=20, money=5000, name="Trader")
    ship.cargo.add_commodity(wood, 5)

    # Turn 1 lists the wood here; turn 2 finds it stale and replans.
    ship.brain.decide_trade_actions()
    assert any(o.actor is ship for o in a.market.sell_orders[wood])
    ship.brain.decide_trade_actions()
    assert ship.brain._local_sale_stale

    # It may plan anything but buying back what it is selling.
    assert not [o for o in a.market.buy_orders[wood] if o.actor is ship]
    plan = ship.brain._current_plan
    assert plan is None or plan.commodity is not wood

    a.market.match_orders()
    assert not any(t.buyer is t.seller for t in a.market.transaction_history)


# ---------------------------------------------------------------------------
# Staleness follows fills, not the sellable total
# ---------------------------------------------------------------------------


def test_a_rescue_fuel_bid_does_not_reset_sale_staleness():
    """Tank fuel turning sellable, and back, is not a fill."""
    sim, fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, fuel_units=60, money=1000, name="Trader")
    ship.cargo.add_commodity(food, 10)

    # Turn 1: only the food is sellable, and nobody takes it.
    ship.brain.decide_trade_actions()
    assert any(o.actor is ship for o in a.market.sell_orders[food])

    # Turn 2: a stranded ship posts a rescue bid for fuel, which makes the
    # tank sellable and swells the sellable total.
    stranded = _make_ship(sim, a, money=50000, name="Stranded")
    rescue_bid = a.market.place_buy_order(stranded, fuel, 20, 900)
    ship.brain.decide_trade_actions()
    assert ship.brain._local_sale_stale

    # Turn 3: the rescue bid is gone, so the sellable total collapses again.
    # Nothing was ever bought from this ship, so the sale is still stale.
    a.market.cancel_order(rescue_bid)
    ship.brain.decide_trade_actions()
    assert ship.brain._local_sale_stale


def test_accumulating_plan_still_lists_unrelated_cargo():
    """A plan's waiting turns do not un-list the cargo the plan is not about."""
    sim, fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    junk = _add_commodity(sim, "junk", "Junk")
    seller = _make_ship(sim, a, hold_fuel=200, name="Seller")
    seller.cargo.add_commodity(food, 50)
    a.market.place_sell_order(seller, food, 50, 10)
    a.market.place_sell_order(seller, fuel, 100, 5)
    buyer_b = _make_ship(sim, b, money=9000, name="BuyerB")
    b.market.place_buy_order(buyer_b, food, 50, 40)

    ship = _make_ship(sim, a, fuel_units=20, money=4000, name="Trader")
    ship.cargo.add_commodity(junk, 5)

    ship.brain.decide_trade_actions()  # lists the junk
    ship.brain.decide_trade_actions()  # junk is stale: adopt a food plan
    assert ship.brain._current_plan is not None
    ship.brain.decide_trade_actions()  # accumulating

    assert ship.brain._current_plan is not None
    assert not ship.brain._plan_loaded
    assert any(o.actor is ship for o in a.market.sell_orders[junk])
    assert [o for o in a.market.buy_orders[food] if o.actor is ship]


# ---------------------------------------------------------------------------
# A near-dry ship on a fuel-selling planet is not parked
# ---------------------------------------------------------------------------


def test_reposition_counts_fuel_the_ship_can_buy_here():
    """A ship with a millifuel of tank and cash repositions, and funds the fuel.

    Reach used to be the tank alone, so a ship sitting on a fuel ask with
    hundreds of credits could neither plan (the round-trip cash gate) nor
    move (no fuel aboard), and idled forever.
    """
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier_a = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    supplier_b = _make_ship(sim, b, hold_fuel=500, name="SupplierB")
    a.market.place_sell_order(supplier_a, fuel, 200, 10)
    b.market.place_sell_order(supplier_b, fuel, 200, 10)

    ship = _make_ship(sim, a, fuel_milli=1, money=2000, name="Trader")
    ship.brain._nav.refresh_market_facts()
    # A warm galaxy where every origin backs a lucrative plan, so fuel is the
    # only thing that can hold the ship here.
    ship.brain._nav.has_any_trade_signal = lambda: True
    ship.brain._best_plan_from = lambda origin: SimpleNamespace(
        expected_profit=1000, origin=origin, destination=b, quantity=0
    )
    # ...except right here, so the ship has a reason to leave.
    ship.brain._find_best_trade_plan = lambda: None

    assert ship.brain._find_reposition_target(ship.fuel, fuel) is b

    # It cannot fly yet, so it commits to the trip and stays this turn.
    assert ship.brain.decide_travel() is None
    assert ship.brain._reposition_intent is not None
    assert ship.brain._reposition_intent.target is b

    # Next docked turn the commitment funds the fuel the departure gate wants.
    ship.brain.decide_trade_actions()
    assert ship.brain._committed_fuel_need == ship.brain._departure_fuel_requirement(b)
    fuel_buys = [o for o in a.market.buy_orders[fuel] if o.actor is ship]
    assert fuel_buys
    bought = units_to_milli(sum(o.quantity for o in fuel_buys))
    assert ship.fuel + bought >= ship.brain._committed_fuel_need

    # With the fuel aboard the reposition departs.
    a.market.match_orders()
    assert ship.brain.decide_travel() is b
    assert ship.brain._reposition_intent is None


def test_reposition_intent_is_dropped_when_the_ship_moves_on():
    """An intent formed at one planet never funds fuel at another."""
    sim, fuel, _food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=500, name="SupplierA")
    a.market.place_sell_order(supplier, fuel, 200, 10)
    ship = _make_ship(sim, a, fuel_milli=1, money=2000, name="Trader")
    ship.brain._reposition_intent = _RepositionIntent(origin=b, target=b)

    ship.brain.decide_trade_actions()

    assert ship.brain._reposition_intent is None
    assert ship.brain._committed_fuel_need == 0


# ---------------------------------------------------------------------------
# Distress
# ---------------------------------------------------------------------------


def _thin_margin_plan(ship, origin, destination, commodity):
    """A plan that clears its costs but misses TradePlan.MIN_MARGIN."""
    distance = ship.route_distance(origin, destination)
    return TradePlan(
        origin=origin,
        destination=destination,
        commodity=commodity,
        quantity=10,
        bid_price_per_unit=10,
        purchase_price_per_unit=10,
        expected_sell_price_per_unit=11,
        distance=distance,
        fuel_needed_one_way=ship.fuel_required(distance),
        fuel_price_at_origin=1,
        fuel_units_from_tank=0,
        fuel_price_from_tank=1,
        expected_maintenance_cost=0,
    )


def test_distressed_ship_accepts_a_haul_that_only_covers_its_costs():
    """Below MIN_MARGIN but profit-positive is good enough when parked."""
    sim, _fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=5, money=100, name="Trader")
    plan = _thin_margin_plan(ship, a, b, food)

    assert plan.expected_profit > 0
    assert not plan.is_profitable()  # under MIN_MARGIN
    assert not ship.brain._plan_acceptable(plan)

    ship.brain._distress_turns = DISTRESS_PATIENCE
    assert ship.brain.is_distressed
    assert ship.brain._plan_acceptable(plan)

    # A losing haul is still refused, distressed or not.
    plan.expected_sell_price_per_unit = 1
    assert not ship.brain._plan_acceptable(plan)


def test_distress_does_not_end_below_the_short_trip_cash_floor():
    """Winning some cargo is not an exit; only cash above the floor is."""
    sim, fuel, food, (a, _b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 40)
    ship = _make_ship(sim, a, fuel_units=5, money=10, name="Trader")
    floor = ship.brain._short_trip_cash_floor()
    assert floor > 10

    ship.brain._distress_turns = DISTRESS_PATIENCE
    ship.cargo.add_commodity(food, 5)  # trade cargo, but still no cash
    ship.brain._update_distress(idle_and_broke=False)
    assert ship.brain.is_distressed

    ship.money = floor
    ship.brain._update_distress(idle_and_broke=False)
    assert not ship.brain.is_distressed


def test_distressed_ship_refuses_a_haul_that_cannot_buy_its_return_leg():
    """Distress lowers the margin bar to round-trip honesty, not to zero.

    The haul is costed one way, but the cash gate makes the ship buy the
    round trip, so a haul earning less than the return leg leaves the ship
    poorer, still under the cash floor, and still distressed.
    """
    sim, _fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 50, 0)])
    ship = _make_ship(sim, a, fuel_units=5, money=100, name="Trader")
    ship.brain._distress_turns = DISTRESS_PATIENCE
    assert ship.brain.is_distressed

    plan = _thin_margin_plan(ship, a, b, food)
    # 24 a unit over the quarter-unit leg puts 6 credits of fuel on each leg,
    # so the 10-credit gross of the haul cannot buy the way home.
    plan.fuel_price_at_origin = 24
    assert plan.expected_profit > 0
    assert plan.expected_profit < plan.return_leg_fuel_cost
    assert not ship.brain._plan_acceptable(plan)

    # Enough to cover the return leg as well: worth flying.
    plan.expected_sell_price_per_unit = 12
    assert not plan.is_profitable()  # still under MIN_MARGIN
    assert plan.expected_profit > plan.return_leg_fuel_cost
    assert ship.brain._plan_acceptable(plan)


def test_choosing_a_plan_over_a_reposition_drops_the_intent():
    """An abandoned target must not fund fuel on the next docked turn."""
    sim, _fuel, food, (a, b) = _make_world([("A", 0, 0), ("B", 100, 0)])
    ship = _make_ship(sim, a, fuel_units=10, money=2000, name="Trader")
    ship.brain._reposition_intent = _RepositionIntent(origin=a, target=b)
    plan = _thin_margin_plan(ship, a, b, food)
    ship.brain._find_best_trade_plan = lambda: plan

    assert ship.brain.decide_travel() is None
    assert ship.brain._reposition_intent is None

    ship.brain.decide_trade_actions()
    assert ship.brain._committed_fuel_need == 0


def test_reposition_intent_is_sticky_while_it_stays_viable():
    """A ship waiting for fuel keeps its target instead of re-aiming.

    Re-surveying every turn meant the fuel committed one turn was never the
    fuel the next turn's target needed, so the ship never left.
    """
    sim, fuel, _food, (a, b, c) = _make_world(
        [("A", 0, 0), ("B", 100, 0), ("C", 200, 0)]
    )
    supplier = _make_ship(sim, a, hold_fuel=500, name="Supplier")
    a.market.place_sell_order(supplier, fuel, 200, 10)
    # A dry tank and the cash to fill it: the trip is fundable but not yet
    # flyable. The kit keeps the maintenance fuel buffer out of the arrival
    # requirement, which a six-unit tank could not cover on top of the legs.
    ship = _make_ship(sim, a, money=2000, name="Trader")
    _give_repair_kit(ship)
    ship.brain._nav.refresh_market_facts()
    ship.brain._nav.has_any_trade_signal = lambda: True
    ship.brain._find_best_trade_plan = lambda: None
    ship.brain._reposition_intent = _RepositionIntent(origin=a, target=c)
    # The survey would pick a different origin this turn.
    ship.brain._find_reposition_target = lambda *_args: b

    assert ship.brain._reposition_intent_target(ship.fuel) is c
    assert ship.brain.decide_travel() is None  # still short of the fuel
    assert ship.brain._reposition_intent is not None
    assert ship.brain._reposition_intent.target is c

    # Once the standing target is out of reach, the survey runs again.
    ship.brain._reposition_intent = _RepositionIntent(origin=a, target=c)
    ship.money = 0
    assert ship.brain._reposition_intent_target(ship.fuel) is None
    ship.brain.decide_travel()
    assert ship.brain._reposition_intent is not None
    assert ship.brain._reposition_intent.target is b
