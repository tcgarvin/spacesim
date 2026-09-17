"""Tests for the fleet KPIs in the summary's `trade` and `fleet` sections."""

from typing import Dict

from spacesim2.analysis.summary import _ACTIVITY_WINDOW_TURNS, compute_summary
from spacesim2.core.ship import ShipStatus
from spacesim2.core.simulation import Simulation


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
    assert fleet["by_owner"] == {}
