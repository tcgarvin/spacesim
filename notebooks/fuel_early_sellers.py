"""Who sells nova_fuel in the first 50 turns, and at what price."""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()
tx = r.market_transactions
print("tx columns:", tx.columns)
early = tx.filter((pl.col("commodity_id") == "nova_fuel") & (pl.col("turn") < 50))
for side in ("seller_name", "buyer_name"):
    if side not in tx.columns:
        continue
    kind = (
        pl.when(pl.col(side).str.starts_with("Trader-"))
        .then(pl.lit("ship"))
        .otherwise(pl.lit("actor"))
        .alias("kind")
    )
    print(
        early.with_columns(kind)
        .group_by("kind")
        .agg(
            pl.col("quantity").sum().alias("units"),
            pl.col("price").mean().round(1).alias("mean_px"),
            pl.col("price").median().alias("med_px"),
            pl.len().alias("fills"),
        )
        .sort("units", descending=True)
        .with_columns(pl.lit(side).alias("side"))
    )
