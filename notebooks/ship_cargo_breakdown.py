"""What goods did ships move, in what volume, and at what margin?

uv run spacesim2 dev analyze ship_cargo_breakdown.py --run data/runs/<dir>
"""

import polars as pl

from spacesim2.analysis.loading import load_run

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_cols(-1)
pl.Config.set_tbl_width_chars(200)

SHIP_RE = r"^Trader-\d+$"

r = load_run()
tx = r.market_transactions
last = int(tx["turn"].max())
print(f"run {r.simulation_id}, turns 1..{last}")

tx = tx.with_columns(
    pl.col("buyer_name").str.contains(SHIP_RE).alias("ship_buys"),
    pl.col("seller_name").str.contains(SHIP_RE).alias("ship_sells"),
)

# Per commodity: what ships bought, what they sold, and the gross spread.
per_good = (
    tx.filter(pl.col("ship_buys") | pl.col("ship_sells"))
    .group_by("commodity_id")
    .agg(
        pl.col("quantity").filter(pl.col("ship_buys")).sum().alias("bought_u"),
        pl.col("quantity").filter(pl.col("ship_sells")).sum().alias("sold_u"),
        pl.col("total_amount").filter(pl.col("ship_buys")).sum().alias("spent"),
        pl.col("total_amount").filter(pl.col("ship_sells")).sum().alias("earned"),
        pl.col("planet_name").filter(pl.col("ship_buys")).n_unique().alias("origins"),
        pl.col("planet_name").filter(pl.col("ship_sells")).n_unique().alias("dests"),
        pl.col("buyer_name")
        .filter(pl.col("ship_buys"))
        .n_unique()
        .alias("ships_buying"),
    )
    .with_columns(
        (pl.col("spent") / pl.col("bought_u")).round(2).alias("buy_px"),
        (pl.col("earned") / pl.col("sold_u")).round(2).alias("sell_px"),
        (pl.col("earned") - pl.col("spent")).alias("gross"),
    )
    .sort("bought_u", descending=True)
)
print("\n=== ship purchases and sales by commodity (whole run)")
print(
    per_good.select(
        "commodity_id",
        "bought_u",
        "sold_u",
        "buy_px",
        "sell_px",
        "spent",
        "earned",
        "gross",
        "origins",
        "dests",
        "ships_buying",
    )
)

# Cargo only: fuel bought for the tank is consumption, not freight.
cargo = per_good.filter(pl.col("commodity_id") != "nova_fuel")
print(
    "\ncargo units bought (ex-fuel):",
    int(cargo["bought_u"].sum()),
    " sold:",
    int(cargo["sold_u"].sum()),
    " gross:",
    int(cargo["gross"].sum()),
)
fuel = per_good.filter(pl.col("commodity_id") == "nova_fuel")
if fuel.height:
    print("fuel bought:", int(fuel["bought_u"][0]), " spend:", int(fuel["spent"][0]))

# Freight proper: a sale on a planet the ship did not buy that good on this turn
# is the interesting case, so split ship sales by whether the ship also bought
# the same good on the same planet.
buys = (
    tx.filter(pl.col("ship_buys"))
    .select(
        pl.col("buyer_name").alias("ship"),
        "commodity_id",
        pl.col("planet_name").alias("origin"),
    )
    .unique()
)
sells = tx.filter(pl.col("ship_sells")).select(
    pl.col("seller_name").alias("ship"),
    "commodity_id",
    pl.col("planet_name").alias("origin"),
    "quantity",
    "total_amount",
)
same_planet = sells.join(buys, on=["ship", "commodity_id", "origin"], how="semi")
print(
    "\nship sale units on a planet that ship also bought that good on:",
    int(same_planet["quantity"].sum()),
    "of",
    int(sells["quantity"].sum()),
)

# Volume over time, 100-turn buckets, cargo only.
buckets = (
    tx.filter(
        (pl.col("ship_buys") | pl.col("ship_sells"))
        & (pl.col("commodity_id") != "nova_fuel")
    )
    .with_columns(((pl.col("turn") - 1) // 100 * 100).alias("bucket"))
    .group_by("bucket")
    .agg(
        pl.col("quantity").filter(pl.col("ship_buys")).sum().alias("bought_u"),
        pl.col("quantity").filter(pl.col("ship_sells")).sum().alias("sold_u"),
        pl.col("commodity_id").filter(pl.col("ship_buys")).n_unique().alias("goods"),
    )
    .sort("bucket")
)
print("\n=== cargo volume by 100-turn bucket (ex-fuel)")
print(buckets)

# Share of each commodity's total market volume that a ship was on one side of.
totals = tx.group_by("commodity_id").agg(pl.col("quantity").sum().alias("market_u"))
share = (
    per_good.join(totals, on="commodity_id", how="left")
    .with_columns(
        (100 * pl.col("bought_u") / pl.col("market_u")).round(1).alias("pct_of_market")
    )
    .select("commodity_id", "bought_u", "market_u", "pct_of_market")
    .sort("pct_of_market", descending=True)
)
print("\n=== ship buying as a share of all trade in that good")
print(share)
