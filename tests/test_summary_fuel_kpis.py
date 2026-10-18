"""Tests for the fleet KPIs in the summary's `trade` and `fleet` sections."""

from typing import Dict

from spacesim.analysis.summary import _ACTIVITY_WINDOW_TURNS, compute_summary
from spacesim.core.ship import INDEPENDENT, ShipStatus
from spacesim.core.simulation import Simulation


def test_fleet_fuel_kpis_present_and_in_range() -> None:
    sim = Simulation()
    sim.setup_simple(
        num_planets=3, num_regular_actors=6, num_market_makers=1, num_ships=2
    )
    for _ in range(20):
        sim.run_turn()

    trade_obj = compute_summary(sim)["trade"]
    assert isinstance(trade_obj, dict)
    trade: Dict[str, object] = trade_obj

    for key in (
        "fuel_ask_planets",
        "stranded_ships",
        "stranded_ship_share",
        "idle_ships",
        "idle_ship_share",
        "departures_window",
        "service_fuel_stock",
        "industrialist_fuel_stock",
        "fuel_sold_by_service_window",
        "fuel_sold_by_service_price",
    ):
        assert key in trade, f"missing trade key: {key}"

    assert isinstance(trade["fuel_ask_planets"], int)
    assert 0 <= trade["fuel_ask_planets"] <= len(sim.planets)

    assert isinstance(trade["stranded_ships"], int)
    assert 0 <= trade["stranded_ships"] <= len(sim.ships)

    assert isinstance(trade["stranded_ship_share"], float)
    assert 0.0 <= trade["stranded_ship_share"] <= 1.0

    assert isinstance(trade["service_fuel_stock"], int)
    assert trade["service_fuel_stock"] >= 0

    assert isinstance(trade["industrialist_fuel_stock"], int)
    assert trade["industrialist_fuel_stock"] >= 0

    assert isinstance(trade["idle_ships"], int)
    assert 0 <= trade["idle_ships"] <= len(sim.ships)

    assert isinstance(trade["idle_ship_share"], float)
    assert 0.0 <= trade["idle_ship_share"] <= 1.0

    assert isinstance(trade["departures_window"], int)
    assert trade["departures_window"] >= 0

    assert isinstance(trade["fuel_sold_by_service_window"], int)
    assert trade["fuel_sold_by_service_window"] >= 0

    assert isinstance(trade["fuel_sold_by_service_price"], float)
    assert trade["fuel_sold_by_service_price"] >= 0.0


def test_idle_ships_counts_never_departed_but_not_recently_departed() -> None:
    sim = Simulation()
    sim.setup_simple(
        num_planets=1, num_regular_actors=2, num_market_makers=1, num_ships=2
    )
    sim.current_turn = _ACTIVITY_WINDOW_TURNS + 10
    assert len(sim.ships) == 2

    never_departed, recently_departed = sim.ships
    for ship in sim.ships:
        ship.status = ShipStatus.DOCKED
    never_departed.last_departure_turn = 0
    recently_departed.last_departure_turn = sim.current_turn - 1

    trade_obj = compute_summary(sim)["trade"]
    assert isinstance(trade_obj, dict)
    trade: Dict[str, object] = trade_obj

    assert trade["idle_ships"] == 1
    assert trade["idle_ship_share"] == 0.5


def test_fleet_block_reports_money_quartiles_and_idle_share() -> None:
    """The `fleet` block carries fleet size, money quartiles, and idle share."""
    sim = Simulation()
    sim.setup_simple(
        num_planets=3, num_regular_actors=6, num_market_makers=1, num_ships=2
    )
    for _ in range(5):
        sim.run_turn()

    summary = compute_summary(sim)
    fleet_obj = summary["fleet"]
    assert isinstance(fleet_obj, dict)
    fleet: Dict[str, object] = fleet_obj

    assert fleet["fleet_size"] == len(sim.ships) == summary["ships"]
    p25 = float(fleet["ship_money_p25"])  # type: ignore[arg-type]
    median = float(fleet["ship_money_median"])  # type: ignore[arg-type]
    p75 = float(fleet["ship_money_p75"])  # type: ignore[arg-type]
    idle_share = float(fleet["ship_idle_share"])  # type: ignore[arg-type]
    assert p25 <= median <= p75
    assert 0.0 <= idle_share <= 1.0
    for key in (
        "owned_ships",
        "independent_ships",
        "ships_built",
        "remitted_total",
        "hull_bids_open",
        "hulls_built",
        "shipyards",
        "company_money_median",
    ):
        assert key in fleet, f"missing fleet key: {key}"
    owned = int(fleet["owned_ships"])  # type: ignore[call-overload]
    independent = int(fleet["independent_ships"])  # type: ignore[call-overload]
    assert owned + independent == fleet["fleet_size"]
    assert isinstance(fleet["ships_built"], int) and fleet["ships_built"] >= 0
    assert isinstance(fleet["remitted_total"], int) and fleet["remitted_total"] >= 0
    assert isinstance(fleet["hull_bids_open"], int) and fleet["hull_bids_open"] >= 0
    assert isinstance(fleet["hulls_built"], int) and fleet["hulls_built"] >= 0
    assert isinstance(fleet["shipyards"], int) and fleet["shipyards"] >= 0
    assert isinstance(fleet["company_money_median"], float)
    assert fleet["company_money_median"] >= 0.0


class _FakeOwner:
    """Minimal ShipOwner protocol implementation for tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.money = 0

    def receive_remittance(self, ship: object, amount: int) -> None:
        self.money += amount


def test_fleet_block_counts_owned_ships_and_remittances() -> None:
    """`owned_ships`/`independent_ships` split on the owner sentinel, and
    `remitted_total` sums `Ship.remitted_total` across the fleet."""
    sim = Simulation()
    sim.setup_simple(
        num_planets=2, num_regular_actors=4, num_market_makers=1, num_ships=2
    )

    owner = _FakeOwner("Test Trading Co")
    owned_ship = sim.ships[0]
    owned_ship.owner = owner
    owned_ship.remitted_total += 250

    expected_owned = sum(1 for ship in sim.ships if ship.owner is not INDEPENDENT)
    expected_remitted = sum(ship.remitted_total for ship in sim.ships)

    summary = compute_summary(sim)
    fleet_obj = summary["fleet"]
    assert isinstance(fleet_obj, dict)
    fleet: Dict[str, object] = fleet_obj

    assert fleet["owned_ships"] == expected_owned
    assert fleet["independent_ships"] == len(sim.ships) - expected_owned
    assert fleet["remitted_total"] == expected_remitted
