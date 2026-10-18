"""Prosperity drives: gate, taste scaling, consumption, and brain wiring."""

import math
from pathlib import Path
from unittest.mock import patch

import pytest

from spacesim2.analysis.summary import compute_summary
from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import SURPLUS_DISCOUNT_FLOOR, SURPLUS_REFERENCE_DAYS
from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.drives import ClothingDrive, FoodDrive, HealthDrive, ShelterDrive
from spacesim2.core.drives.food_drive import (
    DEBT_DECAY_FACTOR as FOOD_DEBT_DECAY_FACTOR,
)
from spacesim2.core.drives.food_drive import (
    PANTRY_MAX,
    QUALITY_DEBT_DECAY_FACTOR,
)
from spacesim2.core.drives.prosperity_drive import (
    CATEGORY_NAMES,
    DEBT_DECAY_FACTOR,
    DEBT_MISS_PENALTY,
    GATE_MAX_DEBT,
    GATE_MIN_BUFFER,
    PROSPERITY_CATEGORIES,
    TASTE_BASE,
    TASTE_FAVORITE,
    ProsperityDrive,
    ProsperityDriveMetrics,
    needs_are_met,
    prosperity_drives,
    prosperity_index,
    random_tastes,
)
from spacesim2.core.market import Market
from spacesim2.core.planet import Planet
from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.view_model import planet_wellbeing
from tests.helpers import get_actor

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def registry() -> CommodityRegistry:
    reg = CommodityRegistry()
    reg.load_from_file(DATA_DIR / "commodities.yaml")
    return reg


def _category(name: str):
    return next(c for c in PROSPERITY_CATEGORIES if c.name == name)


def _actor_with_needs(registry: CommodityRegistry, tastes=None):
    actor = get_actor("Prosperous")
    actor.drives = [
        Drive(registry)
        for Drive in (FoodDrive, ClothingDrive, ShelterDrive, HealthDrive)
    ]
    actor.drives.extend(prosperity_drives(registry, tastes or {}))
    return actor


def _satisfy_needs(actor) -> None:
    for drive in actor.drives:
        if drive.WELLBEING:
            drive.metrics.debt = 0.0
            drive.metrics.buffer = 1.0


class TestTastes:
    def test_random_tastes_has_one_favorite(self):
        """Every category is weighted, exactly one at the favorite weight."""
        tastes = random_tastes()
        assert set(tastes) == set(CATEGORY_NAMES)
        assert sorted(tastes.values())[:-1] == [TASTE_BASE] * (len(CATEGORY_NAMES) - 1)
        assert max(tastes.values()) == TASTE_FAVORITE

    def test_taste_scales_rate_and_target(self, registry):
        """The favorite good is used faster and stocked deeper."""
        cat = _category("clothing")
        plain = ProsperityDrive(registry, cat, TASTE_BASE)
        keen = ProsperityDrive(registry, cat, TASTE_FAVORITE)
        assert keen.event_prob == pytest.approx(plain.event_prob * TASTE_FAVORITE)
        assert keen.target_units() == plain.target_units() * TASTE_FAVORITE

    def test_taste_does_not_change_penalty(self, registry):
        """Preference works through the buffer, never through the stake."""
        cat = _category("luxury")
        assert (
            ProsperityDrive(registry, cat, TASTE_FAVORITE).deprivation_stake()
            == ProsperityDrive(registry, cat, TASTE_BASE).deprivation_stake()
        )

    def test_non_positive_taste_rejected(self, registry):
        """A zero taste is a configuration error, not a silent no-op."""
        with pytest.raises(ValueError):
            ProsperityDrive(registry, _category("food"), 0.0)


class TestGate:
    def test_needs_met_when_all_needs_healthy(self, registry):
        """Low debt and adequate buffer on every need opens the gate."""
        actor = _actor_with_needs(registry)
        _satisfy_needs(actor)
        assert needs_are_met(actor)
        assert all(d.can_purchase(actor) for d in actor.drives)

    def test_one_indebted_need_closes_gate(self, registry):
        """A single need at the debt threshold blocks every prosperity bid."""
        actor = _actor_with_needs(registry)
        _satisfy_needs(actor)
        actor.drives[2].metrics.debt = GATE_MAX_DEBT
        assert not needs_are_met(actor)
        assert not any(d.can_purchase(actor) for d in actor.drives if not d.WELLBEING)

    def test_thin_buffer_closes_gate(self, registry):
        """A need below the buffer floor blocks prosperity bids."""
        actor = _actor_with_needs(registry)
        _satisfy_needs(actor)
        actor.drives[0].metrics.buffer = GATE_MIN_BUFFER - 0.01
        assert not needs_are_met(actor)

    def test_need_drives_always_may_purchase(self, registry):
        """The gate never applies to needs themselves."""
        actor = _actor_with_needs(registry)
        for drive in actor.drives:
            drive.metrics.debt = 1.0
        assert all(d.can_purchase(actor) for d in actor.drives if d.WELLBEING)

    def test_full_food_pantry_passes_buffer_floor(self, registry):
        """An actor holding the food target passes the gate on food.

        tick() consumes one of the six units before computing buffer, so this
        is log_norm_ratio(5, 7.0, 30.0) ~= 0.324 against the 0.20 gate floor.
        """
        actor = _actor_with_needs(registry)
        food = actor.drives[0]
        actor.inventory.add_commodity(food.staple_commodity, food.target_units())
        food.tick(actor)
        assert food.metrics.buffer >= GATE_MIN_BUFFER

    def test_half_pantry_passes_and_two_units_fail_buffer_floor(self, registry):
        """The food floor is half the pantry target: 3 units pass, 2 do not.

        tick() eats one unit first, so 4 held -> 3 counted (0.214 >= 0.2)
        and 3 held -> 2 counted (0.151 < 0.2).
        """
        actor = _actor_with_needs(registry)
        food = actor.drives[0]
        actor.inventory.add_commodity(food.staple_commodity, 4)
        food.tick(actor)
        assert food.metrics.buffer >= GATE_MIN_BUFFER
        actor.inventory.remove_commodity(food.staple_commodity, 1)
        food.tick(actor)
        assert food.metrics.buffer < GATE_MIN_BUFFER


class TestTick:
    def test_not_a_need(self, registry):
        """Prosperity drives are excluded from wellbeing."""
        drive = ProsperityDrive(registry, _category("food"))
        assert drive.WELLBEING is False
        assert drive.metrics.get_name() == "prosperity_food"

    @patch("spacesim2.core.drives.prosperity_drive.random.random", return_value=0.0)
    def test_served_event_consumes_and_raises_coverage(self, _rand, registry):
        """An event with stock removes one unit and moves coverage toward 1."""
        drive = ProsperityDrive(registry, _category("luxury"))
        actor = get_actor("Rich")
        actor.inventory.add_commodity(drive.good, 2)
        metrics = drive.tick(actor)
        assert isinstance(metrics, ProsperityDriveMetrics)
        assert actor.inventory.get_available_quantity(drive.good) == 1
        assert metrics.health == 1.0
        assert metrics.debt == 0.0
        assert 0.0 < metrics.coverage < 1.0

    @patch("spacesim2.core.drives.prosperity_drive.random.random", return_value=0.0)
    def test_missed_event_adds_debt_and_lowers_coverage(self, _rand, registry):
        """An event with no stock adds the miss penalty and decays coverage."""
        drive = ProsperityDrive(registry, _category("health"))
        drive.metrics.coverage = 1.0
        drive.metrics.debt = 0.5
        actor = get_actor("Poor")
        metrics = drive.tick(actor)
        assert metrics.health == 0.0
        assert metrics.debt == pytest.approx(
            0.5 * DEBT_DECAY_FACTOR + DEBT_MISS_PENALTY
        )
        assert metrics.coverage < 1.0

    @patch("spacesim2.core.drives.prosperity_drive.random.random", return_value=1.0)
    def test_quiet_turn_holds_debt_and_coverage(self, _rand, registry):
        """Between events nothing is consumed and the EMA does not move."""
        drive = ProsperityDrive(registry, _category("health"))
        drive.metrics.coverage = 0.4
        drive.metrics.debt = 0.2
        actor = get_actor("Waiting")
        actor.inventory.add_commodity(drive.good, 1)
        metrics = drive.tick(actor)
        assert actor.inventory.get_available_quantity(drive.good) == 1
        assert metrics.coverage == 0.4
        assert metrics.debt == 0.2
        assert metrics.buffer > 0.0

    @patch("spacesim2.core.drives.prosperity_drive.random.random", return_value=1.0)
    def test_taste_cancels_in_buffer(self, _rand, registry):
        """Target stock at any taste gives the same buffer reading."""
        cat = _category("clothing")
        readings = []
        for taste in (TASTE_BASE, TASTE_FAVORITE):
            drive = ProsperityDrive(registry, cat, taste)
            actor = get_actor("Any")
            actor.inventory.add_commodity(drive.good, drive.target_units())
            readings.append(drive.tick(actor).buffer)
        assert readings[0] == pytest.approx(readings[1])


class TestIndex:
    def test_index_is_mean_coverage(self, registry):
        """The index averages coverage over prosperity drives only."""
        actor = _actor_with_needs(registry)
        prosperous = [d for d in actor.drives if not d.WELLBEING]
        half = len(prosperous) // 2
        for i, drive in enumerate(prosperous):
            assert isinstance(drive.metrics, ProsperityDriveMetrics)
            drive.metrics.coverage = 1.0 if i < half else 0.0
        assert prosperity_index(actor) == pytest.approx(0.5)

    def test_index_zero_without_prosperity_drives(self, registry):
        actor = get_actor("Plain")
        assert prosperity_index(actor) == 0.0


class TestBrainWiring:
    @pytest.fixture(scope="class")
    def sim(self) -> Simulation:
        sim = Simulation()
        sim.setup_simple(
            num_planets=1, num_regular_actors=4, num_market_makers=1, num_ships=0
        )
        return sim

    def _regular(self, sim: Simulation):
        return next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)

    def test_setup_attaches_tastes_and_prosperity_drives(self, sim):
        """Every regular actor gets a taste vector and one drive per category."""
        actor = self._regular(sim)
        assert set(actor.tastes) == set(CATEGORY_NAMES)
        names = [d.metrics.get_name() for d in actor.drives]
        assert names[:4] == ["food", "clothing", "shelter", "health"]
        assert names[4:] == [f"prosperity_{c}" for c in CATEGORY_NAMES]

    def test_priority_puts_prosperity_last(self, sim):
        """Prosperity drives sort behind every need regardless of welfare."""
        actor = self._regular(sim)
        for drive in actor.drives:
            drive.metrics.buffer = 1.0 if drive.WELLBEING else 0.0
        ordered = actor.brain._drives_by_priority(actor)
        assert ordered[0].metrics.get_name() == "food"
        flags = [d.WELLBEING for d in ordered]
        assert flags == sorted(flags, reverse=True)

    def test_gate_blocks_prosperity_bids(self, sim):
        """With a need in debt, no buy order names a prosperity good."""
        actor = self._regular(sim)
        actor.money = 10_000
        _satisfy_needs(actor)
        actor.drives[1].metrics.debt = 0.9
        prosperity_ids = {c.commodity_id for c in PROSPERITY_CATEGORIES}
        commands = actor.brain._drive_buy_commands(actor, actor.planet.market)
        assert not any(c.commodity_type.id in prosperity_ids for c in commands)

    def test_met_needs_unlock_prosperity_bids(self, sim):
        """A rich actor with met needs bids for at least one prosperity good."""
        actor = self._regular(sim)
        actor.money = 10_000
        _satisfy_needs(actor)
        for drive in actor.drives:
            if drive.WELLBEING:
                for material in drive.materials():
                    actor.inventory.add_commodity(material, drive.target_units())
        prosperity_ids = {c.commodity_id for c in PROSPERITY_CATEGORIES}
        commands = actor.brain._drive_buy_commands(actor, actor.planet.market)
        assert any(c.commodity_type.id in prosperity_ids for c in commands)

    def test_service_actors_have_no_prosperity_drives(self, sim):
        """Market makers and spaceport operators are not part of the design."""
        service_actors = [a for a in sim.actors if a.actor_type == ActorType.SERVICE]
        assert service_actors
        for actor in service_actors:
            assert not any(not d.WELLBEING for d in actor.drives)


class TestNeedDriveMaterialsBasicGoodOnly:
    """Slow need drives list only their basic good; the upgrade is a fallback.

    Food is the exception: it bids for both the staple and the premium good.
    """

    NEED_DRIVES = [
        (ClothingDrive, "clothing"),
        (HealthDrive, "medicine"),
    ]

    def test_materials_lists_only_the_basic_good(self, registry):
        for Drive, basic_id in self.NEED_DRIVES:
            mats = Drive(registry).materials()
            assert [m.id for m in mats] == [basic_id]

    def test_shelter_materials_list_the_consumable_then_the_dwelling(self, registry):
        """Prefab housing is a durable the shelter need owns, not a
        prosperity good."""
        mats = ShelterDrive(registry).materials()
        assert [m.id for m in mats] == ["simple_building_materials", "prefab_housing"]

    def test_food_materials_list_staple_then_premium(self, registry):
        mats = FoodDrive(registry).materials()
        assert [m.id for m in mats] == ["processed_food", "food"]

    def test_staple_consumed_before_premium(self, registry):
        """With both goods on hand, the staple is drawn down first."""
        drive = FoodDrive(registry)
        actor = get_actor("Stocked")
        actor.inventory.add_commodity(drive.staple_commodity, 2)
        actor.inventory.add_commodity(drive.quality_commodity, 2)
        drive.tick(actor)
        assert actor.inventory.get_available_quantity(drive.staple_commodity) == 1
        assert actor.inventory.get_available_quantity(drive.quality_commodity) == 2

    def test_premium_good_used_as_fallback_when_staple_is_out(self, registry):
        """A hungry actor holding only hand-cooked food still eats."""
        drive = FoodDrive(registry)
        actor = get_actor("QualityOnly")
        actor.inventory.add_commodity(drive.quality_commodity, 1)
        drive.tick(actor)
        assert actor.food_consumed_this_turn is True
        assert actor.inventory.get_available_quantity(drive.quality_commodity) == 0

    def test_premium_meal_decays_debt_faster(self, registry):
        """Eating the premium good uses QUALITY_DEBT_DECAY_FACTOR."""
        staple_drive = FoodDrive(registry)
        premium_drive = FoodDrive(registry)
        staple_drive.metrics.debt = 0.4
        premium_drive.metrics.debt = 0.4
        staple_actor = get_actor("Staple")
        premium_actor = get_actor("Premium")
        staple_actor.inventory.add_commodity(staple_drive.staple_commodity, 1)
        premium_actor.inventory.add_commodity(premium_drive.quality_commodity, 1)
        staple_drive.tick(staple_actor)
        premium_drive.tick(premium_actor)
        assert staple_drive.metrics.debt == pytest.approx(0.4 * FOOD_DEBT_DECAY_FACTOR)
        assert premium_drive.metrics.debt == pytest.approx(
            0.4 * QUALITY_DEBT_DECAY_FACTOR
        )


class TestGateControlsPurchaseNotConsumption:
    @patch("spacesim2.core.drives.prosperity_drive.random.random", return_value=0.0)
    def test_consumption_events_fire_when_gate_is_closed(self, _rand, registry):
        """The gate blocks buy orders only; the drive's own tick still fires."""
        drive = ProsperityDrive(registry, _category("luxury"))
        food = FoodDrive(registry)
        food.metrics.debt = 1.0  # closes the gate
        actor = get_actor("Gated")
        actor.drives = [food, drive]
        actor.inventory.add_commodity(drive.good, 1)
        assert not needs_are_met(actor)
        assert not drive.can_purchase(actor)
        metrics = drive.tick(actor)
        assert actor.inventory.get_available_quantity(drive.good) == 0
        assert metrics.coverage > 0.0


class TestWellbeingExcludesProsperity:
    def test_planet_wellbeing_ignores_prosperity_score(self, registry):
        """A perfect-needs actor with wrecked prosperity scores as 1.0."""
        planet = Planet("Test", Market())
        actor = _actor_with_needs(registry)
        planet.add_actor(actor)
        _satisfy_needs(actor)
        for drive in actor.drives:
            if not drive.WELLBEING:
                assert isinstance(drive.metrics, ProsperityDriveMetrics)
                drive.metrics.coverage = 0.0
                drive.metrics.debt = 1.0
        assert planet_wellbeing(planet) == pytest.approx(1.0)

    def test_summary_drives_block_omits_prosperity_names(self):
        """The verdict-feeding drives block never lists a prosperity_* name."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=1, num_regular_actors=3, num_market_makers=1, num_ships=0
        )
        summary = compute_summary(sim)
        assert set(summary["drives"]) == {"food", "clothing", "shelter", "health"}


class TestSummaryProsperityBlock:
    def test_prosperity_block_has_finite_values_and_no_crash_before_any_gate_pass(self):
        """Every summary key is present and finite, even before anyone qualifies."""
        sim = Simulation()
        sim.setup_simple(
            num_planets=2, num_regular_actors=10, num_market_makers=1, num_ships=1
        )
        for _ in range(3):
            sim.run_turn()
        prosperity = compute_summary(sim)["prosperity"]
        assert isinstance(prosperity, dict)
        assert math.isfinite(prosperity["index_mean"])
        assert 0.0 <= prosperity["gate_pass_share"] <= 1.0
        assert set(prosperity["coverage"]) <= set(CATEGORY_NAMES)
        for value in prosperity["coverage"].values():
            assert math.isfinite(value)
        expected_goods = {c.commodity_id for c in PROSPERITY_CATEGORIES}
        assert set(prosperity["volume_per_planet_turn"]) == expected_goods
        for value in prosperity["volume_per_planet_turn"].values():
            assert math.isfinite(value)


class TestSurplusMoneyDiscount:
    """Wealth lowers the price of money for prosperity bids and nothing else."""

    @pytest.fixture(scope="class")
    def sim(self) -> Simulation:
        sim = Simulation()
        sim.setup_simple(
            num_planets=1, num_regular_actors=4, num_market_makers=1, num_ships=0
        )
        return sim

    def _regular(self, sim: Simulation):
        return next(a for a in sim.actors if a.actor_type == ActorType.REGULAR)

    def test_no_discount_within_reference_days(self, sim):
        actor = self._regular(sim)
        market = actor.planet.market
        price_food = actor.brain._numeraire_price(actor, market)
        assert price_food > 0
        actor.money = int(price_food * SURPLUS_REFERENCE_DAYS) - 1
        assert actor.brain._surplus_money_discount(actor, market) == 1.0

    def test_discount_falls_with_surplus_and_is_floored(self, sim):
        actor = self._regular(sim)
        market = actor.planet.market
        price_food = actor.brain._numeraire_price(actor, market)
        actor.money = int(price_food * SURPLUS_REFERENCE_DAYS * 4)
        assert actor.brain._surplus_money_discount(actor, market) == pytest.approx(
            0.25, rel=0.05
        )
        actor.money = int(price_food * SURPLUS_REFERENCE_DAYS * 1000)
        assert actor.brain._surplus_money_discount(actor, market) == pytest.approx(
            SURPLUS_DISCOUNT_FLOOR
        )

    def test_wealth_raises_prosperity_ceiling_but_not_need_ceiling(self, sim):
        """The same actor, poorer then richer: need WTP holds, prosperity WTP rises."""
        actor = self._regular(sim)
        market = actor.planet.market
        _satisfy_needs(actor)
        clothing = next(d for d in actor.drives if d.metrics.get_name() == "clothing")
        luxury = next(
            d for d in actor.drives if d.metrics.get_name() == "prosperity_luxury"
        )
        clothing.metrics.buffer = 0.0
        luxury.metrics.buffer = 0.0
        price_food = actor.brain._numeraire_price(actor, market)

        def ceilings(money: int):
            actor.money = money
            lam = actor.brain._value_of_money(actor, market)
            surplus_lam = lam * actor.brain._surplus_money_discount(actor, market)
            need = actor.brain._drive_willingness_to_pay(
                actor, market, clothing, clothing.materials()[0], lam
            )
            want = actor.brain._drive_willingness_to_pay(
                actor, market, luxury, luxury.materials()[0], surplus_lam
            )
            return need, want

        # Both wealth levels are food-secure, so the undiscounted lambda is
        # at its floor and the need ceiling is identical for both.
        need_lo, want_lo = ceilings(int(price_food * PANTRY_MAX * 2))
        need_hi, want_hi = ceilings(int(price_food * PANTRY_MAX * 20))
        assert need_hi == need_lo
        assert want_hi >= want_lo * 4
