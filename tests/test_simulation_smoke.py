"""Macro behavioral smoke test for the simulation.

Asserts on population-wide outcomes via the KPI summary, so an economy change
that passes every unit test but wrecks aggregate behavior still fails here.

The simulation is stochastic and has no run-level seed, so assertions use
tolerances, not exact values. Keep the run short so the suite stays fast.
"""

from typing import Dict, List, Tuple

import pytest

from spacesim.analysis.summary import (
    _DRIVE_HEALTH_THRESHOLDS,
    _LATE_RUN_TURNS,
    compute_summary,
)
from spacesim.cli.common import create_and_setup_simulation
from spacesim.core import planet_attributes


@pytest.fixture(scope="module")
def smoke_run() -> dict:
    """Run a small simulation once; return its KPI summary and a self-trade tally.

    Self-trades are tallied as the run proceeds rather than read off the final
    books, because each market retains only its last few hundred transactions.
    """
    # Pin both planets above the hand-food break-even. With one ship and two
    # planets a barren draw (1 in 6 per planet) starves half the population
    # inside 120 turns by design; barren geography has its own tests.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(planet_attributes, "BIOMASS_DRAW_LOW", 0.2)
        sim = create_and_setup_simulation(planets=2, actors=40, makers=1, ships=1)
    highest_seen: Dict[int, int] = {}
    self_trades: List[Tuple[str, str, int]] = []
    for _ in range(120):
        sim.run_turn()
        for planet in sim.planets:
            market = planet.market
            cursor = highest_seen.get(id(market), 0)
            for tx in reversed(market.transaction_history):
                if tx.transaction_id <= cursor:
                    break
                if tx.buyer is tx.seller:
                    self_trades.append((tx.buyer.name, tx.commodity_type.id, tx.price))
            if market.transaction_history:
                highest_seen[id(market)] = market.transaction_history[-1].transaction_id
    return {"summary": compute_summary(sim), "self_trades": self_trades}


@pytest.fixture(scope="module")
def smoke_summary(smoke_run: dict) -> dict:
    """The KPI summary of the shared smoke run."""
    summary: dict = smoke_run["summary"]
    return summary


def test_no_self_trades(smoke_run: dict) -> None:
    """No participant ever trades with itself.

    A self-trade churns an actor's own money and prints a fabricated price and
    volume into the series ship planners read, so the count must be zero. The
    matcher refuses same-actor fills, and the brains are quoted so they never
    ask for one; this catches a regression in either.
    """
    self_trades = smoke_run["self_trades"]
    assert not self_trades, f"{len(self_trades)} self-trades, e.g. {self_trades[:5]}"


def test_summary_has_expected_shape(smoke_summary: dict) -> None:
    """The summary has the KPI blocks agents and notebooks read."""
    for key in (
        "turns",
        "money",
        "drives",
        "inventory_totals",
        "prices",
        "markets",
        "trade",
        "migration",
        "verdict",
    ):
        assert key in smoke_summary, f"missing summary key: {key}"
    assert smoke_summary["turns"] == 120
    # 2 planets * 40 actors. A passenger aboard a ship is in neither
    # sim.actors nor any planet, so it is counted separately.
    migration = smoke_summary["migration"]
    assert smoke_summary["regular_actors"] + migration["aboard"] == 80
    assert migration["departures"] - migration["arrivals"] == migration["aboard"]


def test_food_economy_is_alive(smoke_summary: dict) -> None:
    """Food mean health stays above the FAIL floor of 0.50 and food trades."""
    food = smoke_summary["drives"]["food"]
    assert food["mean_health"] > 0.55, f"food collapsed: {food}"
    assert "food" in smoke_summary["prices"], "no food market activity"


def test_verdict_not_failing(smoke_summary: dict) -> None:
    """A healthy short run does not trip the FAIL verdict."""
    verdict = smoke_summary["verdict"]
    assert verdict["status"] != "FAIL", f"unexpected FAIL: {verdict['flags']}"


def test_economy_produces_goods(smoke_summary: dict) -> None:
    """The bootstrap path runs: wood is harvested and food is produced."""
    totals = smoke_summary["inventory_totals"]
    assert totals.get("wood", 0) > 0, "no wood harvested (bootstrap broken)"
    assert totals.get("food", 0) > 0, "no food produced"


def test_summary_has_market_liveness_section(smoke_summary: dict) -> None:
    """The `markets` section reports a recent-volume window and traded counts."""
    markets = smoke_summary["markets"]
    assert markets["window_turns"] > 0
    volume = markets["volume_per_planet_turn"]
    assert isinstance(volume, dict)
    # Every listed commodity has traded at some point, so counts agree with it.
    assert markets["traded_ever"] == len(volume)
    assert 0 <= markets["traded_recent"] <= markets["traded_ever"]
    assert all(v >= 0.0 for v in volume.values())
    # The survival market must still be moving units, not just have a price.
    assert volume.get("food", 0.0) > 0.0, f"food market frozen: {volume}"


def test_summary_has_trade_section(smoke_summary: dict) -> None:
    """The `trade` section reports ship-delivered units and volume shares."""
    trade = smoke_summary["trade"]
    assert trade["window_turns"] > 0
    delivered = trade["ship_delivered_units"]
    assert isinstance(delivered, dict)
    assert all(units > 0 for units in delivered.values())
    assert trade["ship_delivered_total"] == sum(delivered.values())
    for commodity, share in trade["ship_share_of_volume"].items():
        assert 0.0 <= share <= 1.0, f"bad ship share for {commodity}: {share}"


def test_comfort_and_health_drives_are_gated(smoke_summary: dict) -> None:
    """Every drive is thresholded, each behind the turn its chain stands up."""
    drives = smoke_summary["drives"]
    for name in ("food", "clothing", "shelter", "health"):
        assert name in drives, f"{name} not reported"
        assert name in _DRIVE_HEALTH_THRESHOLDS, f"{name} is not gated"
    assert _DRIVE_HEALTH_THRESHOLDS["food"][0] == 0, "food must always be judged"
    for name in ("clothing", "shelter", "health"):
        min_turns, warn, fail = _DRIVE_HEALTH_THRESHOLDS[name]
        assert min_turns > 0, f"{name} must be gated on run length"
        assert 0.0 < fail < warn < 1.0, f"bad {name} thresholds: {warn}, {fail}"


def test_slow_drives_are_not_judged_on_short_runs(smoke_summary: dict) -> None:
    """Comfort and health drives ramp slowly, so this short run must not flag.

    The fixture runs far fewer turns than the comfort and chemistry tiers need;
    a flag for them here would mean a turn gate regressed.
    """
    turns = smoke_summary["turns"]
    flags = smoke_summary["verdict"]["flags"]
    for name in ("clothing", "shelter", "health"):
        assert turns < _DRIVE_HEALTH_THRESHOLDS[name][0]
        assert not any(flag.startswith(f"{name} health") for flag in flags), flags
    assert turns < _LATE_RUN_TURNS
    assert not any(flag.startswith("no recent") for flag in flags), flags
