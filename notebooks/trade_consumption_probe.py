"""Trade and commodity-consumption probe: which cylinders fire, which don't.

Run: uv run spacesim dev analyze notebooks/trade_consumption_probe.py
"""

import polars as pl

from spacesim.analysis.loading import load_run

r = load_run()
tx = r.market_transactions
snap = r.market_snapshots
drives = r.actor_drives

max_turn = int(snap["turn"].max())
late_lo = max_turn - 99
print(f"run: {r.simulation_id}  turns: {max_turn}  late window: {late_lo}-{max_turn}")


def agg_tx(frame: pl.DataFrame, tag: str) -> pl.DataFrame:
    return frame.group_by("commodity_id").agg(
        pl.col("quantity").sum().alias(f"units_{tag}"),
        pl.col("total_amount").sum().round(0).alias(f"value_{tag}"),
        (pl.col("total_amount").sum() / pl.col("quantity").sum())
        .round(2)
        .alias(f"px_{tag}"),
        pl.col("planet_name").n_unique().alias(f"planets_{tag}"),
        pl.col("buyer_name").n_unique().alias(f"buyers_{tag}"),
    )


all_tx = agg_tx(tx, "all")
late_tx = agg_tx(tx.filter(pl.col("turn") >= late_lo), "late")

universe = snap.select("commodity_id").unique()
book = (
    snap.filter(pl.col("turn") >= late_lo)
    .group_by("commodity_id")
    .agg(
        pl.col("num_buy_orders").mean().round(1).alias("bids"),
        pl.col("num_sell_orders").mean().round(1).alias("asks"),
        pl.col("avg_price").mean().round(2).alias("mean_px"),
        pl.col("scarcity_pressure").mean().round(2).alias("scarcity"),
    )
)

table = (
    universe.join(all_tx, on="commodity_id", how="left")
    .join(late_tx, on="commodity_id", how="left")
    .join(book, on="commodity_id", how="left")
    .fill_null(0)
    .sort("value_all", descending=True)
)

print("\n=== ALL COMMODITIES: trade over run, late window, late order book ===")
with pl.Config(tbl_rows=60, tbl_cols=14, tbl_width_chars=210):
    print(
        table.select(
            "commodity_id",
            "units_all",
            "value_all",
            "px_all",
            "units_late",
            "px_late",
            "planets_late",
            "buyers_late",
            "bids",
            "asks",
            "scarcity",
        )
    )

dead = table.filter(pl.col("units_late") == 0)
print(f"\n=== NOT FIRING in late window: {dead.height} commodities ===")
with pl.Config(tbl_rows=60, tbl_width_chars=210):
    print(
        dead.select(
            "commodity_id", "units_all", "bids", "asks", "mean_px", "scarcity"
        ).sort("units_all", descending=True)
    )

q = max(1, max_turn // 4)
byq = (
    tx.with_columns(((pl.col("turn") - 1) // q).clip(0, 3).alias("quarter"))
    .group_by(["commodity_id", "quarter"])
    .agg(pl.col("quantity").sum().alias("units"))
    .pivot(values="units", index="commodity_id", on="quarter")
    .fill_null(0)
)
print("\n=== UNITS TRADED BY QUARTER (0=early .. 3=late) ===")
with pl.Config(tbl_rows=60, tbl_width_chars=210):
    print(byq.sort("commodity_id"))

dq = (
    drives.with_columns(((pl.col("turn") - 1) // q).clip(0, 3).alias("quarter"))
    .group_by(["drive_name", "quarter"])
    .agg(
        pl.col("health").mean().round(3).alias("health"),
        pl.col("debt").mean().round(3).alias("debt"),
        pl.col("buffer").mean().round(3).alias("buffer"),
    )
    .sort(["drive_name", "quarter"])
)
print("\n=== DRIVE METRICS BY QUARTER (consumption satisfaction) ===")
with pl.Config(tbl_rows=80, tbl_width_chars=210):
    print(dq)

late_dep = (
    drives.filter(pl.col("turn") >= late_lo)
    .group_by("drive_name")
    .agg(
        pl.col("health").mean().round(3).alias("health"),
        (pl.col("health") < 0.5).mean().round(3).alias("deprived_share"),
    )
    .sort("health")
)
print("\n=== LATE-WINDOW DRIVE HEALTH + DEPRIVATION ===")
print(late_dep)

sellers = (
    tx.filter(pl.col("turn") >= late_lo)
    .group_by("seller_name")
    .agg(pl.col("total_amount").sum().round(0).alias("sold"))
    .sort("sold", descending=True)
    .head(10)
)
buyers = (
    tx.filter(pl.col("turn") >= late_lo)
    .group_by("buyer_name")
    .agg(pl.col("total_amount").sum().round(0).alias("bought"))
    .sort("bought", descending=True)
    .head(10)
)
print("\n=== TOP 10 SELLERS / BUYERS (late window, by value) ===")
print(sellers)
print(buyers)

breadth = (
    tx.filter(pl.col("turn") >= late_lo)
    .group_by("commodity_id")
    .agg(pl.col("planet_name").n_unique().alias("planets"))
    .sort("planets", descending=True)
)
print("\n=== PLANETS TRADING EACH GOOD (late window; 50 = universal) ===")
with pl.Config(tbl_rows=60):
    print(breadth)
