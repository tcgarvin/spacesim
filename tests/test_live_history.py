"""Tests for the live view's per-turn history recorder."""

from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.history import HistoryRecorder


def _small_sim() -> Simulation:
    sim = Simulation()
    sim.setup_simple(
        num_planets=2,
        num_regular_actors=8,
        num_market_makers=1,
        num_ships=2,
    )
    return sim


def test_recorder_baseline_sample_on_construction() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    # One baseline point exists for every series before any turn runs.
    assert recorder.turn_axis() == [sim.current_turn]
    assert len(recorder.wellbeing()) == 1
    assert recorder.commodities, "expected at least one transportable commodity"
    for commodity in recorder.commodities:
        assert len(recorder.prices(commodity.id)) == 1
        assert len(recorder.volumes(commodity.id)) == 1


def test_recorder_appends_one_point_per_sample() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    for _ in range(5):
        sim.run_turn()
        recorder.sample()

    assert len(recorder.turn_axis()) == 6  # baseline + 5 turns
    assert len(recorder.wellbeing()) == 6
    sample_commodity = recorder.commodities[0]
    assert len(recorder.prices(sample_commodity.id)) == 6
    assert len(recorder.volumes(sample_commodity.id)) == 6


def test_recorder_window_bounds_series_length() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim, window=4)
    for _ in range(10):
        sim.run_turn()
        recorder.sample()

    # Ring buffer caps every series at the window size.
    assert len(recorder.turn_axis()) == 4
    assert len(recorder.wellbeing()) == 4
    assert len(recorder.prices(recorder.commodities[0].id)) == 4


def test_recorder_values_in_expected_ranges() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    for _ in range(20):
        sim.run_turn()
        recorder.sample()

    for w in recorder.wellbeing():
        assert 0.0 <= w <= 1.0
    for commodity in recorder.commodities:
        assert all(p >= 0.0 for p in recorder.prices(commodity.id))
        assert all(v >= 0.0 for v in recorder.volumes(commodity.id))


def test_unknown_commodity_id_returns_empty() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    assert recorder.prices("does_not_exist") == []
    assert recorder.volumes("does_not_exist") == []


def test_per_planet_series_sampled_in_lockstep_with_galaxy() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    for _ in range(5):
        sim.run_turn()
        recorder.sample()

    expected_len = len(recorder.turn_axis())
    commodity = recorder.commodities[0]
    for planet in sim.planets:
        assert len(recorder.planet_wellbeing_series(planet.name)) == expected_len
        assert len(recorder.planet_prices(planet.name, commodity.id)) == expected_len
        assert len(recorder.planet_volumes(planet.name, commodity.id)) == expected_len
        for w in recorder.planet_wellbeing_series(planet.name):
            assert 0.0 <= w <= 1.0
        assert all(p >= 0.0 for p in recorder.planet_prices(planet.name, commodity.id))


def test_unknown_planet_returns_empty_series() -> None:
    sim = _small_sim()
    recorder = HistoryRecorder(sim)
    assert recorder.planet_prices("Nowhere", recorder.commodities[0].id) == []
    assert recorder.planet_volumes("Nowhere", recorder.commodities[0].id) == []
    assert recorder.planet_wellbeing_series("Nowhere") == []
