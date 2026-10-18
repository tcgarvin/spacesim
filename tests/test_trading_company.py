"""The trading company: fleet ownership, hull valuation, and ship spawning."""

from typing import List, Sequence

from spacesim.core.actor import Actor, ActorType
from spacesim.core.brains.trading_company import (
    MIN_REMITTANCE_HISTORY_TURNS,
    REMITTANCE_RATE_ALPHA,
    SHIP_RAMP_DISCOUNT,
    SHIP_VALUE_HORIZON_TURNS,
    TradingCompanyBrain,
)
from spacesim.core.commands import PlaceBuyOrderCommand
from spacesim.core.simulation import Simulation


def _company(sim: Simulation, planet_index: int = 0) -> Actor:
    planet = sim.planets[planet_index]
    for actor in planet.actors:
        if isinstance(actor.brain, TradingCompanyBrain):
            return actor
    raise AssertionError(f"{planet.name} has no trading company")


def _brain(company: Actor) -> TradingCompanyBrain:
    brain = company.brain
    assert isinstance(brain, TradingCompanyBrain)
    return brain


def _small_sim(num_ships: int = 1) -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=2,
        num_regular_actors=2,
        num_market_makers=1,
        num_spaceport_operators=1,
        num_ships=num_ships,
    )
    return sim


def _steady(brain: TradingCompanyBrain, rate: float) -> None:
    """Put the brain on a settled remittance rate with enough history to bid.

    The banked income is set to exactly one turn at ``rate``, so the EMA
    update at the top of the next ``decide_market_actions`` leaves the rate
    where it is and the hull valuation holds still across calls.
    """
    brain.accounted_turns = MIN_REMITTANCE_HISTORY_TURNS
    brain.remittance_rate = rate
    brain._income_since_accounted = int(rate * max(1, len(brain.fleet)))


def _buy_orders(commands: Sequence[object]) -> List[PlaceBuyOrderCommand]:
    return [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]


class TestSetup:
    def test_every_planet_has_one_company_owning_its_ships(self) -> None:
        sim = _small_sim()
        for planet in sim.planets:
            companies = [
                a for a in planet.actors if isinstance(a.brain, TradingCompanyBrain)
            ]
            assert len(companies) == 1
            company = companies[0]
            assert company.actor_type is ActorType.SERVICE
            assert company.name == f"{planet.name} Trading Co"
            assert company.money == 0
            assert company.drives == []

            assert len(planet.ships) == 1
            for ship in planet.ships:
                assert ship.owner is company
            assert _brain(company).fleet == list(planet.ships)

    def test_setup_records_galaxy_ship_sizing(self) -> None:
        sim = _small_sim()
        assert sim.ship_starting_capital > 0
        assert sim.ship_fuel_capacity > 0


class TestRemittanceRate:
    def test_a_remittance_updates_the_rate_and_the_money(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        ship = brain.fleet[0]

        ship.money = ship.retained_capital + 400
        moved = ship.remit_surplus()

        assert moved == 400
        assert company.money == 400
        assert ship.remitted_total == 400

        brain._account_income()
        assert brain.remittance_rate == REMITTANCE_RATE_ALPHA * 400
        assert brain.accounted_turns == 1

    def test_no_bid_before_the_rate_has_history(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        company.money = 10_000_000
        _steady(brain, 100.0)
        brain.accounted_turns = MIN_REMITTANCE_HISTORY_TURNS - 2

        assert brain.hull_value() == 0
        assert _buy_orders(brain.decide_market_actions(company)) == []

    def test_hull_value_is_the_discounted_horizon_of_remittance(self) -> None:
        sim = _small_sim()
        brain = _brain(_company(sim))
        _steady(brain, 20.0)
        assert brain.hull_value() == int(
            20.0 * SHIP_VALUE_HORIZON_TURNS * SHIP_RAMP_DISCOUNT
        )


class TestHullBid:
    def test_a_bid_appears_at_the_computed_value_once_cash_covers_it(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        _steady(brain, 20.0)
        value = brain.hull_value()

        company.money = value + sim.ship_starting_capital - 1
        assert _buy_orders(brain.decide_market_actions(company)) == []

        _steady(brain, 20.0)
        company.money = value + sim.ship_starting_capital
        bids = _buy_orders(brain.decide_market_actions(company))
        assert len(bids) == 1
        assert bids[0].commodity_type.id == "ship_hull"
        assert bids[0].quantity == 1
        assert bids[0].price == value

    def test_the_bid_never_commits_the_new_ship_s_endowment(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        for money in (
            sim.ship_starting_capital,
            sim.ship_starting_capital + 4000,
            10 * sim.ship_starting_capital,
        ):
            _steady(brain, 40.0)
            company.money = money
            for bid in _buy_orders(brain.decide_market_actions(company)):
                assert bid.price <= money - sim.ship_starting_capital

    def test_a_company_holding_a_hull_does_not_bid(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        _steady(brain, 20.0)
        company.money = 10 * sim.ship_starting_capital
        hull = sim.commodity_registry["ship_hull"]
        company.inventory.add_commodity(hull, 1)

        assert _buy_orders(brain.decide_market_actions(company)) == []

    def test_an_unchanged_valuation_leaves_the_resting_bid_alone(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        brain = _brain(company)
        _steady(brain, 20.0)
        company.money = 10 * sim.ship_starting_capital

        for command in brain.decide_market_actions(company):
            command.execute(company)
        market = sim.planets[0].market
        hull = sim.commodity_registry["ship_hull"]
        resting = market.get_actor_orders(company)["buy"]
        assert len(resting) == 1
        assert resting[0].commodity_type is hull

        _steady(brain, 20.0)
        assert brain.decide_market_actions(company) == []
        assert market.get_actor_orders(company)["buy"][0].order_id == (
            resting[0].order_id
        )


class TestShipSpawn:
    def test_a_bought_hull_becomes_an_owned_ship_at_the_planet(self) -> None:
        sim = _small_sim()
        planet = sim.planets[0]
        company = _company(sim)
        brain = _brain(company)
        hull = sim.commodity_registry["ship_hull"]

        company.money = 10 * sim.ship_starting_capital
        company.inventory.add_commodity(hull, 1)
        before_money = company.money
        before_ships = len(sim.ships)

        brain.decide_market_actions(company)

        assert company.inventory.get_quantity(hull) == 0
        assert company.money == before_money - sim.ship_starting_capital
        assert len(sim.pending_ship_spawns) == 1

        sim._spawn_pending_ships()

        assert len(sim.pending_ship_spawns) == 0
        assert len(sim.ships) == before_ships + 1
        new_ship = sim.ships[-1]
        assert new_ship.owner is company
        assert new_ship.planet is planet
        assert new_ship in planet.ships
        assert new_ship in brain.fleet
        assert new_ship.money == sim.ship_starting_capital
        assert new_ship.fuel == 0
        assert new_ship.name.startswith(f"{company.name}-")
        # Money only moved: company + new ship is what the company held.
        assert company.money + new_ship.money == before_money

    def test_run_turn_drains_the_queue_before_the_ship_phase(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        hull = sim.commodity_registry["ship_hull"]
        company.money = 10 * sim.ship_starting_capital
        company.inventory.add_commodity(hull, 1)
        before_ships = len(sim.ships)

        sim.run_turn()

        assert len(sim.ships) == before_ships + 1
        assert len(sim.pending_ship_spawns) == 0

    def test_a_hull_purchase_conserves_money_across_company_ship_and_seller(
        self,
    ) -> None:
        sim = _small_sim()
        planet = sim.planets[0]
        company = _company(sim)
        brain = _brain(company)
        _steady(brain, 20.0)
        value = brain.hull_value()
        hull = sim.commodity_registry["ship_hull"]

        seller = Actor(
            name="HullYard",
            sim=sim,
            planet=planet,
            actor_type=ActorType.SERVICE,
            drives=[],
            brain=TradingCompanyBrain(),
            initial_money=0,
        )
        seller.inventory.add_commodity(hull, 1)
        planet.market.place_sell_order(seller, hull, 1, value)

        company.money = value + sim.ship_starting_capital
        total_before = company.money + seller.money

        for command in brain.decide_market_actions(company):
            command.execute(company)
        planet.market.match_orders()

        assert company.inventory.get_quantity(hull) == 1
        brain.decide_market_actions(company)
        sim._spawn_pending_ships()

        new_ship = sim.ships[-1]
        total_after = (
            company.money + company.reserved_money + seller.money + new_ship.money
        )
        assert total_after == total_before


class TestNoOtherBehavior:
    def test_the_company_never_takes_an_economic_action(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        assert _brain(company).decide_economic_action(company) is None

    def test_the_company_never_migrates(self) -> None:
        sim = _small_sim()
        company = _company(sim)
        assert company.claims_land is False
