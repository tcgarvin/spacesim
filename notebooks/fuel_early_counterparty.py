"""Who buys the ships' cheap fuel in turns 0-49."""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()
tx = r.market_transactions
early = tx.filter(
    (pl.col("commodity_id") == "nova_fuel")
    & (pl.col("turn") < 50)
    & pl.col("seller_name").str.starts_with("Trader-")
)
print("buyers of ship-sold fuel, turns 0-49:")
print(
    early.with_columns(
        pl.col("buyer_name").str.replace(r"[-_ ]?\d+$", "").alias("buyer_kind")
    )
    .group_by("buyer_kind")
    .agg(
        pl.col("quantity").sum().alias("units"),
        pl.col("price").median().alias("med_px"),
        pl.col("total_amount").sum().alias("credits"),
        pl.len().alias("fills"),
    )
    .sort("units", descending=True)
    .head(8)
)
