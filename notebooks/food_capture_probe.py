"""Where does staple food go, and at what price, with the substitute bound off.

Tier-1 export analysis. Splits food purchases into consumption vs
processing by identifying actors who ever sold processed_food, then
reports per-planet food / processed_food prices and the top food buyers.

    uv run spacesim2 dev analyze notebooks/food_capture_probe.py
"""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()
print(f"run: {r.simulation_id}")

txn = r.market_transactions
snap = r.market_snapshots
LATE = 301

makers = set(
    txn.filter(pl.col("commodity_id") == "processed_food")["seller_name"].unique()
)
print(f"processed_food sellers: {len(makers)}")

food = txn.filter((pl.col("commodity_id") == "food") & (pl.col("turn") >= LATE))
food = food.with_columns(
    pl.when(pl.col("buyer_name").is_in(list(makers)))
    .then(pl.lit("pf_maker"))
    .when(pl.col("buyer_name").str.contains("Maker|Operator|Ship|Dealer"))
    .then(pl.lit("dealer_ship"))
    .otherwise(pl.lit("consumer"))
    .alias("klass")
)
print("\n[A] food purchases by buyer class, turns >=301")
print(
    food.group_by("klass")
    .agg(
        pl.col("quantity").sum().alias("units"),
        pl.col("total_amount").sum().alias("credits"),
        pl.col("price").mean().round(1).alias("mean_px"),
        pl.col("price").max().alias("max_px"),
        pl.len().alias("txns"),
    )
    .sort("units", descending=True)
)

print("\n[B] per-planet prices, turns >=301 (mean avg_price where volume>0)")
px = (
    snap.filter(
        pl.col("turn").ge(LATE)
        & pl.col("commodity_id").is_in(["food", "processed_food"])
        & pl.col("volume").gt(0)
    )
    .group_by("planet_name", "commodity_id")
    .agg(
        pl.col("avg_price").mean().round(1).alias("px"),
        pl.col("volume").sum().alias("vol"),
    )
    .pivot(on="commodity_id", index="planet_name", values=["px", "vol"])
    .sort("planet_name")
)
print(px)

print("\n[C] top-5 food buyers per planet by credits spent (turns >=301)")
top = (
    food.group_by("planet_name", "buyer_name", "klass")
    .agg(
        pl.col("quantity").sum().alias("units"),
        pl.col("price").mean().round(1).alias("mean_px"),
        pl.col("price").max().alias("max_px"),
    )
    .sort(["planet_name", "units"], descending=[False, True])
    .group_by("planet_name", maintain_order=True)
    .head(3)
)
with pl.Config(tbl_rows=40):
    print(top)

print("\n[D] pf_maker food-price premium per planet")
prem = (
    food.group_by("planet_name", "klass")
    .agg(pl.col("price").mean().round(1).alias("px"))
    .pivot(on="klass", index="planet_name", values="px")
    .sort("planet_name")
)
print(prem)

print("\n[E] food price vs processed_food price, galaxy mean by 50-turn block")
blk = (
    snap.filter(
        pl.col("commodity_id").is_in(["food", "processed_food"])
        & pl.col("volume").gt(0)
    )
    .with_columns((pl.col("turn") // 50 * 50).alias("blk"))
    .group_by("blk", "commodity_id")
    .agg(pl.col("avg_price").mean().round(1).alias("px"))
    .pivot(on="commodity_id", index="blk", values="px")
    .sort("blk")
)
print(blk)
