"""Tier-1 health check: trends over the run beyond the end-state summary."""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()
print(f"run: {r.simulation_id}")

drives = r.actor_drives
snaps = r.market_snapshots
txns = r.market_transactions

max_turn = int(snaps["turn"].max())
print(f"max_turn={max_turn}")
print(
    "drive sample turns:",
    sorted(drives["turn"].unique().to_list())[:8],
    "...",
    sorted(drives["turn"].unique().to_list())[-3:],
)


# 1. Drive health and debt trend by turn window.
def window(t):
    if t <= 100:
        return "1_early(<=100)"
    if t <= 300:
        return "2_mid(101-300)"
    return "3_late(301+)"


d = drives.with_columns(
    pl.col("turn").map_elements(window, return_dtype=pl.Utf8).alias("win")
)
dep = (
    d.group_by(["drive_name", "win"])
    .agg(
        pl.col("health").mean().round(3).alias("health"),
        pl.col("debt").mean().round(3).alias("debt"),
        (pl.col("debt") > 0.5).mean().round(3).alias("pct_deprived"),
        pl.len().alias("n"),
    )
    .sort(["drive_name", "win"])
)
print("\n=== DRIVE TRENDS (deprived = debt>0.5) ===")
print(dep)

# 2. Upper-tier transaction volume by window.
upper = [
    "chemicals",
    "refined_chemicals",
    "medicine",
    "polymers",
    "glass",
    "silica",
    "rare_earth_ore",
    "nova_fuel",
]
tw = txns.with_columns(
    pl.col("turn").map_elements(window, return_dtype=pl.Utf8).alias("win")
).filter(pl.col("commodity_id").is_in(upper))
vol = (
    tw.group_by(["commodity_id", "win"])
    .agg(
        pl.col("quantity").sum().alias("qty"),
        pl.len().alias("n_txn"),
        pl.col("price").mean().round(2).alias("avg_price"),
    )
    .sort(["commodity_id", "win"])
)
print("\n=== UPPER-TIER TRADE VOLUME BY WINDOW ===")
print(vol)

# 3. Late-window mean price and CV per commodity.
late = snaps.filter(pl.col("turn") > 300)
price_stab = (
    late.group_by("commodity_id")
    .agg(
        pl.col("avg_price").mean().round(2).alias("mean_price"),
        pl.col("avg_price").std().round(2).alias("std_price"),
        pl.col("volume").sum().alias("late_volume"),
    )
    .with_columns((pl.col("std_price") / pl.col("mean_price")).round(3).alias("cv"))
    .sort("commodity_id")
)
print("\n=== LATE-WINDOW (turn>300) PRICE STABILITY ===")
print(price_stab)

# 4. Whole-run price trajectory for key commodities.
key = ["food", "simple_tools", "medicine", "clothing", "chemicals", "refined_chemicals"]
traj = (
    snaps.filter(pl.col("commodity_id").is_in(key))
    .with_columns(
        pl.col("turn").map_elements(window, return_dtype=pl.Utf8).alias("win")
    )
    .group_by(["commodity_id", "win"])
    .agg(pl.col("avg_price").mean().round(2).alias("price"))
    .sort(["commodity_id", "win"])
)
print("\n=== KEY PRICE TRAJECTORY (deflation watch: food~11, tools~25) ===")
print(traj)

# 5. Ship activity. Ships move goods between planets; identify them by name.
names = pl.concat(
    [
        txns.select(pl.col("buyer_name").alias("name")),
        txns.select(pl.col("seller_name").alias("name")),
    ]
)
ship_names = names.filter(
    pl.col("name").str.contains("(?i)ship|trader|freighter|hauler")
).unique()
print("\n=== NAMES matching ship pattern ===")
print(ship_names.head(20))

# Cross-planet trade proxy: transaction counts by window.
recent = txns.filter(pl.col("turn") > 450)
print(f"\nTxns in last 50 turns: {len(recent)} (total {len(txns)})")
print("Per-window txn counts:")
print(
    txns.with_columns(
        pl.col("turn").map_elements(window, return_dtype=pl.Utf8).alias("win")
    )
    .group_by("win")
    .agg(pl.len().alias("n_txn"), pl.col("quantity").sum().alias("qty"))
    .sort("win")
)
