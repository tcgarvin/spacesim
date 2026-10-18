"""Brain-side migration reasoning: pressure, destinations, intent, liquidation."""

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, cast

import pytest

from spacesim.core.actor import Actor, ActorType
from spacesim.core.brains import migration as mig
from spacesim.core.brains.colonist import ColonistBrain
from spacesim.core.brains.industrialist import IndustrialistBrain
from spacesim.core.commands import PlaceBuyOrderCommand, PlaceSellOrderCommand
from spacesim.core.contracts import Contract, ContractStatus, PassengerPayload
from spacesim.core.drives.food_drive import FoodDrive
from spacesim.core.land import Land
from spacesim.core.market import Market
from spacesim.core.migration import MigrationRequest, NoMigration, PlanetStats
from spacesim.core.planet import Planet
from spacesim.core.planet_attributes import PlanetAttributes
from spacesim.core.simulation import Simulation

if TYPE_CHECKING:
    from spacesim.core.navigation import Navigator


class StubNavigator:
    """Fixed lane distances and fuel price, so these tests need no galaxy.

    Both are what ``passage_fare`` reads: a leg's fuel at 1 unit per 20 lane
    units, valued at the reference.
    """

    def __init__(
        self,
        distances: Optional[Dict[Tuple[str, str], float]] = None,
        default: float = 10.0,
        fuel_reference: float = 40.0,
    ) -> None:
        self._distances: Dict[Tuple[str, str], float] = distances or {}
        self._default = default
        self._fuel_reference = fuel_reference

    def distance(self, a: Planet, b: Planet) -> float:
        return self._distances.get((a.name, b.name), self._default)

    def fuel_value_reference(self) -> Optional[float]:
        return self._fuel_reference


def _navigator(default: float = 10.0, fuel_reference: float = 40.0) -> "Navigator":
    """A StubNavigator where the real type is asked for."""
    return cast(
        "Navigator", StubNavigator(default=default, fuel_reference=fuel_reference)
    )


def _world(num_planets: int = 3, num_lands: int = 5) -> Simulation:
    """A simulation with hand-built planets and no actors or ships."""
    sim = Simulation()
    for index in range(num_planets):
        sim.planets.append(Planet(f"P{index}", Market(), num_lands=num_lands))
    return sim


def _actor(sim: Simulation, planet: Planet, brain, money: int = 1000, drives=None):
    actor = Actor("Migrant", sim, ActorType.REGULAR, drives or [], brain, None, money)
    planet.add_actor(actor)
    return actor


def _stats(
    free_land_count: int = 5,
    free_land_mean: Optional[Dict[str, float]] = None,
    median_need_debt: float = 0.0,
    median_prosperity: float = 0.0,
) -> PlanetStats:
    return PlanetStats(
        population=10,
        free_land_count=free_land_count,
        free_land_mean=free_land_mean or {"biomass": 0.5},
        median_need_debt=median_need_debt,
        median_prosperity=median_prosperity,
        median_money=100.0,
    )


def _stats_with_worse_origin(sim: Simulation) -> Dict[Planet, PlanetStats]:
    """Stats where the first planet is starving and every other one is not."""
    stats = {planet: _stats() for planet in sim.planets}
    stats[sim.planets[0]] = _stats(median_need_debt=0.8)
    return stats


def _check_turn(actor: Actor, interval_index: int = 0) -> int:
    """A turn on which ``actor`` runs its migration check."""
    return mig._check_offset(actor) + interval_index * mig.MIGRATION_CHECK_INTERVAL


def _quiet_turn(actor: Actor) -> int:
    """A turn past MIN_INTENT_TURNS on which ``actor`` runs no check."""
    return _check_turn(actor, 5) + 1


def _passage(sim: Simulation, actor: Actor, status: ContractStatus, advance: int = 60):
    """A passage contract in ``status``, built without going through a board."""
    contract = Contract(
        poster=actor,
        origin=sim.planets[0],
        destination=sim.planets[1],
        payload=PassengerPayload(actor=actor),
        advance=advance,
        on_delivery=0,
        posted_turn=0,
        expires_turn=sim.current_turn,
    )
    contract.status = status
    return contract


def _open_contract(sim: Simulation, actor: Actor, advance: int = 60):
    return _passage(sim, actor, ContractStatus.OPEN, advance)


def _expired_contract(sim: Simulation, actor: Actor):
    return _passage(sim, actor, ContractStatus.EXPIRED)


class TestMigrationPressure:
    def test_content_actor_has_no_pressure(self) -> None:
        sim = _world(1)
        brain = ColonistBrain()
        actor = _actor(
            sim, sim.planets[0], brain, drives=[FoodDrive(sim.commodity_registry)]
        )

        assert mig.migration_pressure(actor) == 0.0

    def test_full_drive_debt_saturates_pressure(self) -> None:
        sim = _world(1)
        drive = FoodDrive(sim.commodity_registry)
        actor = _actor(sim, sim.planets[0], ColonistBrain(), drives=[drive])
        drive.metrics.debt = 1.0

        assert mig.migration_pressure(actor) == 1.0

    def test_partial_debt_passes_through(self) -> None:
        sim = _world(1)
        drive = FoodDrive(sim.commodity_registry)
        actor = _actor(sim, sim.planets[0], ColonistBrain(), drives=[drive])
        drive.metrics.debt = 0.5

        assert mig.migration_pressure(actor) == pytest.approx(0.5)

    def test_land_worse_than_planet_mean_adds_pressure(self) -> None:
        sim = _world(1)
        actor = _actor(sim, sim.planets[0], ColonistBrain())
        # The planet mean for every resource is 1.0 by default.
        actor.land = Land({"biomass": 0.0})

        assert mig.migration_pressure(actor) == pytest.approx(mig.PRESSURE_LAND_WEIGHT)

    def test_land_at_planet_mean_adds_nothing(self) -> None:
        attributes = PlanetAttributes(biomass=0.4)
        sim = Simulation()
        sim.planets.append(Planet("P0", Market(), attributes=attributes, num_lands=5))
        actor = _actor(sim, sim.planets[0], ColonistBrain())
        actor.land = Land.at_mean(attributes)

        assert mig.migration_pressure(actor) == 0.0

    def test_actor_without_planet_has_no_pressure(self) -> None:
        sim = _world(1)
        actor = Actor("Drifter", sim, ActorType.REGULAR, [], ColonistBrain())

        assert mig.migration_pressure(actor) == 0.0


class TestChooseDestination:
    def test_excludes_current_and_full_planets(self) -> None:
        sim = _world(3)
        home, full, open_planet = sim.planets
        actor = _actor(sim, home, ColonistBrain())
        stats = {
            home: _stats(median_need_debt=1.0),
            full: _stats(free_land_count=0, free_land_mean={"biomass": 1.0}),
            open_planet: _stats(free_land_count=1, free_land_mean={"biomass": 0.5}),
        }

        picks = {mig.choose_destination(actor, stats, _navigator()) for _ in range(50)}
        assert picks == {open_planet}

    def test_no_candidates_returns_sentinel(self) -> None:
        sim = _world(2)
        home, other = sim.planets
        actor = _actor(sim, home, ColonistBrain())
        stats = {home: _stats(median_need_debt=1.0), other: _stats(free_land_count=0)}

        assert mig.choose_destination(actor, stats, _navigator()) is mig.NO_DESTINATION

    def test_no_gain_over_staying_returns_sentinel(self) -> None:
        """A destination no better than home is not worth a fare and a land."""
        sim = _world(2)
        home, other = sim.planets
        actor = _actor(sim, home, ColonistBrain())
        stats = {home: _stats(), other: _stats()}

        assert mig.choose_destination(actor, stats, _navigator()) is mig.NO_DESTINATION

    def test_prefers_better_land_and_wellbeing(self) -> None:
        sim = _world(3)
        home, good, bad = sim.planets
        actor = _actor(sim, home, ColonistBrain())
        stats = {
            home: _stats(median_need_debt=1.0),
            good: _stats(free_land_mean={"biomass": 0.9}, median_need_debt=0.0),
            bad: _stats(free_land_mean={"biomass": 0.2}, median_need_debt=0.8),
        }

        draws = [mig.choose_destination(actor, stats, _navigator()) for _ in range(500)]
        assert draws.count(good) / len(draws) > 0.7

    def test_both_planets_are_reachable(self) -> None:
        """The draw is a softmax, not an argmax: near-ties split."""
        sim = _world(3)
        home, first, second = sim.planets
        actor = _actor(sim, home, ColonistBrain())
        stats = {
            home: _stats(median_need_debt=1.0),
            first: _stats(free_land_mean={"biomass": 0.50}),
            second: _stats(free_land_mean={"biomass": 0.51}),
        }

        draws = [mig.choose_destination(actor, stats, _navigator()) for _ in range(500)]
        assert draws.count(first) > 0 and draws.count(second) > 0


class TestIntentStateMachine:
    @pytest.fixture
    def world(self, monkeypatch):
        sim = _world(3)
        monkeypatch.setattr(mig, "get_navigator", lambda _sim: _navigator())
        drive = FoodDrive(sim.commodity_registry)
        brain = ColonistBrain()
        actor = _actor(sim, sim.planets[0], brain, money=10_000, drives=[drive])
        sim.planet_stats = _stats_with_worse_origin(sim)
        return sim, actor, brain, drive

    def test_enters_above_threshold_when_the_roll_passes(self, world, monkeypatch):
        sim, actor, brain, drive = world
        drive.metrics.debt = 0.5
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)

        sim.current_turn = _check_turn(actor)
        assert isinstance(brain.decide_migration(actor), NoMigration)
        assert brain.migration_intent.active
        assert brain.migration_intent.destination in sim.planets[1:]

    def test_stays_put_when_the_roll_fails(self, world, monkeypatch):
        sim, actor, brain, drive = world
        drive.metrics.debt = 0.5
        monkeypatch.setattr(mig.random, "random", lambda: 1.0)

        sim.current_turn = _check_turn(actor)
        brain.decide_migration(actor)
        assert not brain.migration_intent.active

    def test_below_enter_threshold_never_forms_an_intent(self, world, monkeypatch):
        sim, actor, brain, drive = world
        drive.metrics.debt = mig.ENTER_THRESHOLD - 0.05
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)

        sim.current_turn = _check_turn(actor)
        brain.decide_migration(actor)
        assert not brain.migration_intent.active

    def test_intent_only_advances_on_the_actors_own_check_turn(
        self, world, monkeypatch
    ):
        sim, actor, brain, drive = world
        drive.metrics.debt = 0.5
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)

        sim.current_turn = _check_turn(actor) + 1
        brain.decide_migration(actor)
        assert not brain.migration_intent.active

    def test_exits_below_exit_threshold(self, world, monkeypatch):
        sim, actor, brain, drive = world
        drive.metrics.debt = 0.5
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)
        sim.current_turn = _check_turn(actor)
        brain.decide_migration(actor)
        assert brain.migration_intent.active

        drive.metrics.debt = mig.EXIT_THRESHOLD - 0.05
        sim.current_turn = _check_turn(actor, 1)
        brain.decide_migration(actor)
        assert not brain.migration_intent.active
        assert brain.migration_intent.destination is None

    def test_hysteresis_band_keeps_the_intent(self, world, monkeypatch):
        sim, actor, brain, drive = world
        drive.metrics.debt = 0.5
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)
        sim.current_turn = _check_turn(actor)
        brain.decide_migration(actor)

        # Between the two thresholds: neither enters nor exits.
        drive.metrics.debt = (mig.ENTER_THRESHOLD + mig.EXIT_THRESHOLD) / 2
        sim.current_turn = _check_turn(actor, 1)
        brain.decide_migration(actor)
        assert brain.migration_intent.active


class TestDepartureRequest:
    @pytest.fixture
    def leaving(self, monkeypatch):
        """A colonist holding an active intent formed on its first check."""
        sim = _world(2)
        monkeypatch.setattr(
            mig,
            "get_navigator",
            lambda _sim: _navigator(default=1000.0),
        )
        drive = FoodDrive(sim.commodity_registry)
        brain = ColonistBrain()
        actor = _actor(sim, sim.planets[0], brain, money=10_000, drives=[drive])
        sim.planet_stats = _stats_with_worse_origin(sim)
        drive.metrics.debt = 0.9
        monkeypatch.setattr(mig.random, "random", lambda: 0.0)
        sim.current_turn = _check_turn(actor)
        brain.decide_migration(actor)
        assert brain.migration_intent.active
        return sim, actor, brain

    def test_no_request_before_the_minimum_intent(self, leaving):
        sim, actor, brain = leaving
        sim.current_turn = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS - 1

        assert isinstance(brain.decide_migration(actor), NoMigration)

    def test_request_after_the_minimum_intent(self, leaving):
        sim, actor, brain = leaving
        sim.current_turn = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS

        decision = brain.decide_migration(actor)
        assert isinstance(decision, MigrationRequest)
        assert decision.destination is brain.migration_intent.destination
        # Distance 1000 is 5 fuel units; at a 40-credit reference that is 200
        # of fuel, and the opening offer is the estimate, fuel plus half.
        assert decision.fare_offer == 300
        assert brain.migration_intent.first_request_turn == sim.current_turn

    def test_the_offer_climbs_to_the_headroom_cap(self, leaving):
        sim, actor, brain = leaving
        start = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS
        offers = []
        for elapsed in (0, mig.FARE_ESCALATION_TURNS // 2, mig.FARE_ESCALATION_TURNS):
            sim.current_turn = start + elapsed
            decision = brain.decide_migration(actor)
            assert isinstance(decision, MigrationRequest)
            offers.append(decision.fare_offer)
        # From the leg's fuel times 1.5 to fuel times 2.25.
        assert offers == [300, 375, 450]

        # And no further: the ceiling is the ceiling.
        sim.current_turn = start + 4 * mig.FARE_ESCALATION_TURNS
        decision = brain.decide_migration(actor)
        assert isinstance(decision, MigrationRequest)
        assert decision.fare_offer == 450

    def test_the_offer_caps_at_the_actors_money(self, leaving):
        sim, actor, brain = leaving
        start = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS
        sim.current_turn = start
        brain.decide_migration(actor)

        sim.current_turn = start + mig.FARE_ESCALATION_TURNS
        actor.money = 400
        decision = brain.decide_migration(actor)
        assert isinstance(decision, MigrationRequest)
        assert decision.fare_offer == 400

    def test_an_actor_that_cannot_afford_the_fare_keeps_saving(self, leaving):
        sim, actor, brain = leaving
        sim.current_turn = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS
        actor.money = 299  # the fare is 300

        assert isinstance(brain.decide_migration(actor), NoMigration)
        assert brain.migration_intent.active

    def test_money_the_open_contract_holds_still_counts_as_the_budget(self, leaving):
        sim, actor, brain = leaving
        sim.current_turn = brain.migration_intent.since_turn + mig.MIN_INTENT_TURNS
        actor.passage_contract = _open_contract(sim, actor, advance=300)
        actor.money = 0

        decision = brain.decide_migration(actor)
        assert isinstance(decision, MigrationRequest)
        assert decision.fare_offer == 300


class TestPassageExpiry:
    @pytest.fixture
    def leaving(self, monkeypatch):
        sim = _world(2)
        monkeypatch.setattr(mig, "get_navigator", lambda _sim: _navigator(default=10.0))
        brain = ColonistBrain()
        actor = _actor(sim, sim.planets[0], brain, money=10_000)
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=0, destination=sim.planets[1]
        )
        # Off the actor's check turn, so the intent state machine stays put
        # and only the expiry handling is under test.
        sim.current_turn = _quiet_turn(actor)
        return sim, actor, brain

    def test_the_first_expiry_leaves_the_intent_standing(self, leaving):
        sim, actor, brain = leaving
        actor.passage_contract = _expired_contract(sim, actor)

        decision = brain.decide_migration(actor)
        assert isinstance(decision, MigrationRequest)
        assert brain.migration_intent.active
        assert brain.migration_intent.expiries == 1

    def test_one_expiry_is_counted_once(self, leaving):
        sim, actor, brain = leaving
        actor.passage_contract = _expired_contract(sim, actor)

        for _ in range(5):
            brain.decide_migration(actor)
        assert brain.migration_intent.expiries == 1
        assert brain.migration_intent.active

    def test_the_second_expiry_drops_the_intent(self, leaving):
        sim, actor, brain = leaving
        actor.passage_contract = _expired_contract(sim, actor)
        brain.decide_migration(actor)
        actor.passage_contract = _expired_contract(sim, actor)

        assert isinstance(brain.decide_migration(actor), NoMigration)
        assert not brain.migration_intent.active


class TestLiquidation:
    def _colonist_world(self):
        sim = _world(2)
        brain = ColonistBrain()
        actor = _actor(sim, sim.planets[0], brain, money=1000)
        wood = sim.commodity_registry.get_commodity("wood")
        actor.inventory.add_commodity(wood, 5)
        return sim, actor, brain, wood

    def test_staying_colonist_buys_tools_and_keeps_its_buffer(self):
        sim, actor, brain, wood = self._colonist_world()

        commands = brain.decide_market_actions(actor)
        tools = sim.commodity_registry.get_commodity("simple_tools")
        assert any(
            isinstance(c, PlaceBuyOrderCommand) and c.commodity_type is tools
            for c in commands
        )
        # NON_DRIVE_KEEP_LEVELS holds 2 wood back, so only 3 are offered.
        wood_sales = [
            c
            for c in commands
            if isinstance(c, PlaceSellOrderCommand) and c.commodity_type is wood
        ]
        assert [c.quantity for c in wood_sales] == [3]

    def test_leaving_colonist_stops_buying_tools_and_sells_everything(self):
        sim, actor, brain, wood = self._colonist_world()
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=0, destination=sim.planets[1]
        )

        commands = brain.decide_market_actions(actor)
        assert not [c for c in commands if isinstance(c, PlaceBuyOrderCommand)]
        wood_sales = [
            c
            for c in commands
            if isinstance(c, PlaceSellOrderCommand) and c.commodity_type is wood
        ]
        assert [c.quantity for c in wood_sales] == [5]

    def test_leaving_colonist_offers_its_facility(self):
        sim, actor, brain, _wood = self._colonist_world()
        facility = sim.commodity_registry.get_commodity("smelting_facility")
        assert not facility.transportable
        actor.inventory.add_commodity(facility, 1)
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=0, destination=sim.planets[1]
        )

        commands = brain.decide_market_actions(actor)
        assert any(
            isinstance(c, PlaceSellOrderCommand) and c.commodity_type is facility
            for c in commands
        )

    def test_leaving_industrialist_builds_nothing_and_adopts_nothing(self):
        sim = _world(2)
        brain = IndustrialistBrain()
        actor = _actor(sim, sim.planets[0], brain, money=1000)
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=0, destination=sim.planets[1]
        )

        brain.decide_economic_action(actor)
        assert brain.chosen_recipe_id is None


class TestRelocationHook:
    def test_colonist_relocation_clears_the_intent(self):
        sim = _world(2)
        brain = ColonistBrain()
        actor = _actor(sim, sim.planets[0], brain)
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=3, destination=sim.planets[1]
        )

        brain.on_relocated(actor)
        assert not brain.migration_intent.active
        assert brain.migration_intent.destination is None
        assert brain._cache is None

    def test_industrialist_relocation_clears_the_recipe(self):
        sim = _world(2)
        brain = IndustrialistBrain()
        actor = _actor(sim, sim.planets[0], brain)
        brain.migration_intent = mig.MigrationIntent(
            active=True, since_turn=3, destination=sim.planets[1]
        )
        brain.chosen_recipe_id = "make_food"
        brain.recipe_cooldown_until = {"mine_common_metal_ore": 99}

        brain.on_relocated(actor)
        assert not brain.migration_intent.active
        assert brain.chosen_recipe_id is None
        assert brain.recipe_cooldown_until == {}


class TestPropensity:
    def test_propensity_is_fixed_and_in_range(self):
        propensities: List[float] = []
        for _ in range(50):
            brain = ColonistBrain()
            assert (
                mig.PROPENSITY_MIN <= brain.migration_propensity <= mig.PROPENSITY_MAX
            )
            propensities.append(brain.migration_propensity)
        assert len(set(propensities)) > 1  # drawn per brain, not shared
