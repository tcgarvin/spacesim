"""Why interplanetary trade volume is a rounding error.

Parts 1, 2, 5: ship share of volume over time, per-trip economics
reconstructed by FIFO lot matching, and what the fleet actually carries.

Run: uv run spacesim2 dev analyze notebooks/ship_trade_volume_probe.py
"""

from collections import defaultdict, deque

import polars as pl

from spacesim2.analysis.loading import load_run

SHIP_PAT = r"^Trader-\d+$"
CARGO_CAPACITY = 100  # Ship.cargo_capacity default, spacesim2/core/ship.py

run = load_run()
tx = run.market_transactions
max_turn = int(tx["turn"].max())
late_lo = max_turn - 99
print(f"run={run.simulation_id} turns={max_turn} late_window={late_lo}-{max_turn}")

tx = tx.with_columns(
    pl.col("seller_name").str.contains(SHIP_PAT).alias("ship_sell"),
    pl.col("buyer_name").str.contains(SHIP_PAT).alias("ship_buy"),
)

# --- Part 1: trade share per 100-turn window -------------------------------
win = tx.with_columns(((pl.col("turn") - 1) // 100).alias("w"))
share = (
    win.group_by("w")
    .agg(
        pl.col("quantity").sum().alias("units_total"),
        pl.col("total_amount").sum().alias("value_total"),
        pl.col("quantity").filter(pl.col("ship_sell")).sum().alias("units_ship_sold"),
        pl.col("total_amount")
        .filter(pl.col("ship_sell"))
        .sum()
        .alias("value_ship_sold"),
        pl.col("quantity").filter(pl.col("ship_buy")).sum().alias("units_ship_bought"),
    )
    .with_columns(
        (100 * pl.col("units_ship_sold") / pl.col("units_total"))
        .round(3)
        .alias("pct_units"),
        (100 * pl.col("value_ship_sold") / pl.col("value_total"))
        .round(3)
        .alias("pct_value"),
    )
    .sort("w")
)
print("\n=== PART 1: ship share by 100-turn window (pct = ship sales / all) ===")
with pl.Config(tbl_rows=20, tbl_width_chars=200):
    print(share)

# --- FIFO lot matching: reconstruct cargo legs -----------------------------
ship_tx = (
    tx.filter(pl.col("ship_sell") | pl.col("ship_buy"))
    .with_columns(
        pl.when(pl.col("ship_buy"))
        .then(pl.col("buyer_name"))
        .otherwise(pl.col("seller_name"))
        .alias("ship")
    )
    .select(
        "ship",
        "turn",
        "planet_name",
        "commodity_id",
        "quantity",
        "price",
        "total_amount",
        "ship_buy",
    )
    .sort(["ship", "turn"])
)

lots: dict[tuple[str, str], deque] = defaultdict(deque)
legs: list[dict] = []
unmatched_sold = 0
for row in ship_tx.iter_rows(named=True):
    key = (row["ship"], row["commodity_id"])
    if row["ship_buy"]:
        lots[key].append(
            [row["quantity"], row["price"], row["turn"], row["planet_name"]]
        )
        continue
    remaining = row["quantity"]
    while remaining > 0 and lots[key]:
        lot = lots[key][0]
        take = min(remaining, lot[0])
        legs.append(
            {
                "ship": row["ship"],
                "commodity_id": row["commodity_id"],
                "units": take,
                "buy_planet": lot[3],
                "sell_planet": row["planet_name"],
                "buy_turn": lot[2],
                "sell_turn": row["turn"],
                "cost": take * lot[1],
                "revenue": take * row["price"],
            }
        )
        lot[0] -= take
        remaining -= take
        if lot[0] == 0:
            lots[key].popleft()
    unmatched_sold += remaining

legs_df = pl.DataFrame(legs).with_columns(
    (pl.col("sell_planet") != pl.col("buy_planet")).alias("interplanetary"),
    (pl.col("sell_turn") - pl.col("buy_turn")).alias("hold_turns"),
    (pl.col("revenue") - pl.col("cost")).alias("gross"),
)
print(f"\nmatched legs={legs_df.height} unmatched_ship_sold_units={unmatched_sold}")

# --- Part 2: per-trip economics -------------------------------------------
# A trip = one ship selling at one destination planet on one turn-cluster.
inter = legs_df.filter(
    pl.col("interplanetary") & (pl.col("commodity_id") != "nova_fuel")
)
trips = (
    inter.group_by(["ship", "sell_planet", "sell_turn"])
    .agg(
        pl.col("units").sum().alias("units"),
        pl.col("revenue").sum().alias("revenue"),
        pl.col("cost").sum().alias("cost"),
        pl.col("gross").sum().alias("gross"),
        pl.col("hold_turns").mean().alias("hold_turns"),
        pl.col("commodity_id").n_unique().alias("goods"),
    )
    .with_columns(
        (100 * pl.col("gross") / pl.col("revenue")).alias("margin_pct"),
        (100 * pl.col("units") / CARGO_CAPACITY).alias("hold_fill_pct"),
    )
)
print(f"\n=== PART 2: interplanetary cargo trips (n={trips.height}) ===")
with pl.Config(tbl_rows=20, tbl_width_chars=200, float_precision=2):
    print(
        trips.select(
            "units", "revenue", "cost", "gross", "margin_pct", "hold_turns", "goods"
        ).describe()
    )

late_trips = trips.filter(pl.col("sell_turn") >= late_lo)
print(
    f"\nlate-window trips={late_trips.height}, "
    f"units median={late_trips['units'].median()}, "
    f"mean={round(late_trips['units'].mean() or 0, 2)}, "
    f"max={late_trips['units'].max()}"
)
print(
    f"late-window mean hold fill = "
    f"{round(late_trips['hold_fill_pct'].mean() or 0, 2)}% of {CARGO_CAPACITY} units"
)

# Same-planet (round-trip) churn: bought and sold on one planet, moved nothing.
same = legs_df.filter(~pl.col("interplanetary"))
print(
    f"\nsame-planet legs (moved nothing): {same.height} legs, "
    f"{same['units'].sum()} units vs interplanetary {inter['units'].sum()} units"
)

# Trips per ship and how many ships trade at all.
per_ship = (
    trips.group_by("ship")
    .agg(
        pl.len().alias("trips"),
        pl.col("units").sum().alias("units"),
        pl.col("gross").sum().alias("gross"),
    )
    .sort("units", descending=True)
)
print(f"\nships with >=1 cargo trip: {per_ship.height} / 50")
with pl.Config(tbl_rows=8, tbl_width_chars=160):
    print(per_ship.head(5))
    print(per_ship.tail(3))

# Visit reconstruction: planet changes visible in ship transactions.
visits = (
    ship_tx.select("ship", "turn", "planet_name")
    .unique()
    .sort(["ship", "turn"])
    .with_columns(
        (pl.col("planet_name") != pl.col("planet_name").shift(1).over("ship")).alias(
            "moved"
        )
    )
)
moves_late = visits.filter(pl.col("moved") & (pl.col("turn") >= max_turn - 49)).height
print(
    f"\nplanet changes visible in tx, last 50 turns: {moves_late} "
    f"(summary departures_window=153); trips selling cargo in same window: "
    f"{trips.filter(pl.col('sell_turn') >= max_turn - 49).height}"
)

# --- Part 5: what ships carry ---------------------------------------------
by_good = (
    inter.filter(pl.col("sell_turn") >= late_lo)
    .group_by("commodity_id")
    .agg(
        pl.col("units").sum().alias("units"),
        pl.col("revenue").sum().alias("revenue"),
        pl.col("gross").sum().alias("gross"),
        pl.len().alias("legs"),
    )
    .with_columns((pl.col("units") / pl.col("legs")).round(2).alias("units_per_leg"))
    .sort("revenue", descending=True)
)
print("\n=== PART 5: ship-delivered goods, late 100 turns ===")
with pl.Config(tbl_rows=30, tbl_width_chars=200):
    print(by_good)
