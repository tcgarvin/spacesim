"""Tier-1a: early nova_fuel price and supply by 25-turn bucket."""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()
print(f"run: {r.simulation_id}")
snaps = r.market_snapshots
print("snapshot columns:", snaps.columns)

fuel = snaps.filter(
    (pl.col("commodity_id") == "nova_fuel") & (pl.col("turn") < 300)
).with_columns((pl.col("turn") // 25 * 25).alias("bucket"))

agg = (
    fuel.group_by("bucket")
    .agg(
        pl.col("best_ask").mean().round(1).alias("mean_ask"),
        pl.col("best_ask").median().alias("med_ask"),
        pl.col("best_bid").mean().round(1).alias("mean_bid"),
        pl.col("avg_price").mean().round(1).alias("mean_avg_px"),
        pl.col("volume").sum().alias("volume"),
        pl.col("best_ask").is_not_null().sum().alias("planets_with_ask"),
        pl.col("best_bid").is_not_null().sum().alias("planets_with_bid"),
        pl.len().alias("rows"),
    )
    .sort("bucket")
)
print(agg)

tx = r.market_transactions.filter(
    (pl.col("commodity_id") == "nova_fuel") & (pl.col("turn") < 300)
).with_columns((pl.col("turn") // 25 * 25).alias("bucket"))
print(
    tx.group_by("bucket")
    .agg(
        pl.col("price").mean().round(1).alias("mean_fill_px"),
        pl.col("quantity").sum().alias("units"),
        pl.len().alias("fills"),
    )
    .sort("bucket")
)

live = fuel.filter(pl.col("best_ask") > 0)
print("planets with a real ask, and ask level:")
print(
    live.group_by("bucket")
    .agg(
        (pl.len() / 25).round(1).alias("planets_with_ask_per_turn"),
        pl.col("best_ask").median().alias("med_ask"),
        pl.col("best_ask").quantile(0.9).alias("p90_ask"),
    )
    .sort("bucket")
)
bids = fuel.filter(pl.col("best_bid") > 0)
print("planets with a real bid:")
print(
    bids.group_by("bucket")
    .agg(
        (pl.len() / 25).round(1).alias("planets_with_bid_per_turn"),
        pl.col("best_bid").median().alias("med_bid"),
    )
    .sort("bucket")
)
