"""Parquet table schemas for simulation data export."""

import pyarrow as pa

# Actor state per turn.
ACTOR_TURNS_SCHEMA = pa.schema(
    [
        ("simulation_id", pa.string()),
        ("turn", pa.int32()),
        ("actor_id", pa.string()),
        ("actor_name", pa.string()),
        ("money", pa.int64()),
        ("reserved_money", pa.int64()),
        ("inventory_json", pa.string()),  # JSON {commodity_id: quantity}
        ("planet_name", pa.string()),
    ]
)

# Drive metrics per turn per actor.
ACTOR_DRIVES_SCHEMA = pa.schema(
    [
        ("simulation_id", pa.string()),
        ("turn", pa.int32()),
        ("actor_id", pa.string()),
        ("drive_name", pa.string()),
        ("health", pa.float64()),
        ("debt", pa.float64()),
        ("buffer", pa.float64()),
        ("urgency", pa.float64()),
    ]
)

# One row per transaction.
MARKET_TRANSACTIONS_SCHEMA = pa.schema(
    [
        ("simulation_id", pa.string()),
        ("turn", pa.int32()),
        ("planet_name", pa.string()),
        ("commodity_id", pa.string()),
        ("buyer_id", pa.string()),
        ("buyer_name", pa.string()),
        ("seller_id", pa.string()),
        ("seller_name", pa.string()),
        ("quantity", pa.int32()),
        ("price", pa.int64()),
        ("total_amount", pa.int64()),
    ]
)

# Aggregated market state per turn.
MARKET_SNAPSHOTS_SCHEMA = pa.schema(
    [
        ("simulation_id", pa.string()),
        ("turn", pa.int32()),
        ("planet_name", pa.string()),
        ("commodity_id", pa.string()),
        ("avg_price", pa.float64()),
        ("volume", pa.int32()),  # total traded this turn
        ("num_buy_orders", pa.int32()),
        ("num_sell_orders", pa.int32()),
        ("best_bid", pa.int64()),
        ("best_ask", pa.int64()),
        ("scarcity_pressure", pa.float64()),  # unmet-import escalation signal
    ]
)


# Ship state per turn, one row per ship.
SHIP_TURNS_SCHEMA = pa.schema(
    [
        ("simulation_id", pa.string()),
        ("turn", pa.int32()),
        ("ship_name", pa.string()),
        # "TRAVELING" while in flight; the docked planet otherwise.
        ("planet_name", pa.string()),
        # Where the ship is flying; empty while docked.
        ("destination_name", pa.string()),
        ("status", pa.string()),
        ("money", pa.int64()),
        ("reserved_money", pa.int64()),
        ("fuel", pa.int64()),  # millifuel; see FUEL_MILLI in core/ship.py
        ("cargo_units", pa.int32()),
        ("cargo_capacity", pa.int32()),
        ("contract_hold_units", pa.int32()),
        # Cumulative since the run started.
        ("departures", pa.int32()),
        ("refuel_stops", pa.int32()),
        ("pickup_stops", pa.int32()),
        ("last_departure_turn", pa.int32()),
        ("owner_name", pa.string()),
        ("retained_capital", pa.int64()),
        ("remitted_total", pa.int64()),
    ]
)
