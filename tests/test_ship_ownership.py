"""Ship ownership and the remittance of surplus cash to the owner.

- Every ship has an owner; the default is the INDEPENDENT sentinel, which
  never receives money and leaves ship behavior unchanged.
- An owned ship remits money above its retained capital at the end of a
  docked turn, excluding money reserved for resting bids, and never while
  distressed.
"""

from spacesim2.core.ship import (
    INDEPENDENT,
    SHIP_RETAINED_CAPITAL_MULTIPLE,
    IndependentOwner,
    Ship,
    ShipStatus,
    retained_capital_for,
)
from tests.test_ship_fuel import _make_ship, _make_world


class RecordingOwner:
    """Test double for a ship owner; records what each ship remitted."""

    def __init__(self, name: str = "Owner", money: int = 0) -> None:
        self.name = name
        self.money = money
        self.remittances: list[tuple[str, int]] = []

    def receive_remittance(self, ship: Ship, amount: int) -> None:
        self.remittances.append((ship.name, amount))


def _owned_ship(money: int = 10_000, retained_capital: int = 1_000):
    """Dock an owned ship with a stocked purse at a single-planet world."""
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0), ("Mars", 50, 0)])
    ship = _make_ship(sim, planets[0], money=money)
    owner = RecordingOwner()
    ship.owner = owner
    ship.retained_capital = retained_capital
    return ship, owner


# ---------------------------------------------------------------------------
# The sentinel owner
# ---------------------------------------------------------------------------


def test_a_ship_is_independent_by_default():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0), ("Mars", 50, 0)])
    ship = _make_ship(sim, planets[0], money=10_000)
    assert ship.owner is INDEPENDENT
    assert isinstance(ship.owner, IndependentOwner)


def test_an_independent_ship_never_remits():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0), ("Mars", 50, 0)])
    ship = _make_ship(sim, planets[0], money=10_000)
    ship.retained_capital = 100

    assert ship.remit_surplus() == 0
    assert ship.money == 10_000
    assert ship.remitted_total == 0
    assert INDEPENDENT.money == 0


def test_the_sentinel_owner_ignores_a_remittance():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0)])
    ship = _make_ship(sim, planets[0])
    INDEPENDENT.receive_remittance(ship, 500)
    assert INDEPENDENT.money == 0


# ---------------------------------------------------------------------------
# Remitting
# ---------------------------------------------------------------------------


def test_an_owned_ship_remits_cash_above_its_retained_capital():
    ship, owner = _owned_ship(money=10_000, retained_capital=1_000)

    assert ship.remit_surplus() == 9_000
    assert ship.money == 1_000
    assert owner.money == 9_000
    assert ship.remitted_total == 9_000


def test_remittance_conserves_money():
    ship, owner = _owned_ship(money=7_500, retained_capital=2_000)
    total_before = ship.money + owner.money

    ship.remit_surplus()

    assert ship.money + owner.money == total_before


def test_remittance_accumulates_over_turns():
    ship, owner = _owned_ship(money=3_000, retained_capital=1_000)
    ship.remit_surplus()
    ship.money = 2_500
    ship.remit_surplus()

    assert ship.remitted_total == 3_500
    assert owner.money == 3_500
    assert owner.remittances == [(ship.name, 2_000), (ship.name, 1_500)]


def test_an_owner_records_which_ship_remitted():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0), ("Mars", 50, 0)])
    owner = RecordingOwner()
    first = _make_ship(sim, planets[0], money=5_000, name="First")
    second = _make_ship(sim, planets[1], money=4_000, name="Second")
    for ship in (first, second):
        ship.owner = owner
        ship.retained_capital = 1_000

    first.remit_surplus()
    second.remit_surplus()

    assert owner.remittances == [("First", 4_000), ("Second", 3_000)]
    assert owner.money == 7_000


def test_no_remittance_below_the_retained_capital():
    ship, owner = _owned_ship(money=900, retained_capital=1_000)

    assert ship.remit_surplus() == 0
    assert ship.money == 900
    assert owner.money == 0


def test_no_remittance_exactly_at_the_retained_capital():
    ship, owner = _owned_ship(money=1_000, retained_capital=1_000)

    assert ship.remit_surplus() == 0
    assert owner.money == 0


def test_reserved_money_is_not_surplus():
    ship, owner = _owned_ship(money=5_000, retained_capital=1_000)
    ship.reserved_money = 3_500

    assert ship.remit_surplus() == 500
    assert ship.money == 4_500
    assert owner.money == 500


def test_a_traveling_ship_does_not_remit():
    ship, owner = _owned_ship(money=10_000, retained_capital=1_000)
    ship.status = ShipStatus.TRAVELING

    assert ship.remit_surplus() == 0
    assert owner.money == 0


def test_a_ship_awaiting_maintenance_does_not_remit():
    ship, owner = _owned_ship(money=10_000, retained_capital=1_000)
    ship.status = ShipStatus.NEEDS_MAINTENANCE

    assert ship.remit_surplus() == 0
    assert owner.money == 0


def test_distress_suppresses_remittance():
    ship, owner = _owned_ship(money=10_000, retained_capital=1_000)
    ship.brain._distress_turns = 99

    assert ship.brain.is_distressed
    assert ship.remit_surplus() == 0
    assert ship.money == 10_000
    assert owner.money == 0


# ---------------------------------------------------------------------------
# Retained capital
# ---------------------------------------------------------------------------


def test_retained_capital_is_a_multiple_of_starting_capital():
    assert retained_capital_for(1_000) == SHIP_RETAINED_CAPITAL_MULTIPLE * 1_000
    assert retained_capital_for(0) == 0


def test_a_ship_defaults_its_retained_capital_from_its_purse():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0)])
    ship = Ship("Trader", sim, planets[0], initial_money=2_000)
    assert ship.retained_capital == retained_capital_for(2_000)


def test_retained_capital_can_be_set_at_construction():
    sim, _fuel, _food, planets = _make_world([("Earth", 0, 0)])
    ship = Ship("Trader", sim, planets[0], initial_money=2_000, retained_capital=0)
    assert ship.retained_capital == 0


def test_take_turn_remits_at_the_end_of_a_docked_turn():
    ship, owner = _owned_ship(money=10_000, retained_capital=1_000)
    ship.take_turn()

    assert ship.status is ShipStatus.DOCKED
    assert owner.money == ship.remitted_total > 0
    assert ship.money + ship.reserved_money + owner.money == 10_000
