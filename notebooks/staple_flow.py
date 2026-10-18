"""Does processed_food move by ship, and what does the destination book look like?

uv run spacesim2 dev analyze staple_flow.py --run data/runs/<dir>
"""

import polars as pl

from spacesim2.analysis.loading import load_run

pl.Config.set_tbl_cols(-1)
pl.Config.set_tbl_width_chars(200)

SHIP_RE = r"^Trader-\d+$"
GOODS = ["food", "processed_food", "quality_clothing", "medicine"]

r = load_run()
tx = r.market_transactions
snap = r.market_snapshots
last = int(tx["turn"].max())
print(f"run {r.simulation_id} turns 1..{last}")

# Ship-moved units per 100-turn bucket, last bucket first.
sub = tx.filter(pl.col("commodity_id").is_in(GOODS)).with_columns(
    ((pl.col("turn") - 1) // 100 * 100).alias("bucket"),
    pl.col("seller_name").str.contains(SHIP_RE).alias("ship_sells"),
    pl.col("buyer_name").str.contains(SHIP_RE).alias("ship_buys"),
)
print(
    sub.group_by("bucket", "commodity_id")
    .agg(
        pl.col("quantity").sum().alias("total"),
        pl.col("quantity").filter(pl.col("ship_buys")).sum().alias("ship_bought"),
        pl.col("quantity").filter(pl.col("ship_sells")).sum().alias("ship_sold"),
        pl.col("planet_name").filter(pl.col("ship_sells")).n_unique().alias("dests"),
        pl.col("price").filter(pl.col("ship_buys")).mean().round(2).alias("buy_px"),
        pl.col("price").filter(pl.col("ship_sells")).mean().round(2).alias("sell_px"),
    )
    .sort("commodity_id", "bucket")
)

# Destination book for processed_food, last 100 turns: bid depth by planet.
pf = snap.filter(
    (pl.col("commodity_id") == "processed_food") & (pl.col("turn") > last - 100)
)
book = (
    pf.group_by("planet_name")
    .agg(
        pl.col("best_bid").mean().round(1).alias("bid"),
        pl.col("best_ask").mean().round(1).alias("ask"),
        pl.col("num_buy_orders").mean().round(1).alias("n_bids"),
        pl.col("num_sell_orders").mean().round(1).alias("n_asks"),
        pl.col("volume").mean().round(1).alias("vol"),
        pl.col("scarcity_pressure").mean().round(2).alias("press"),
    )
    .sort("bid", descending=True)
)
print("\nprocessed_food book, last 100 turns, top 12 by best bid")
print(book.head(12))
print(
    "planets with mean best_bid >= 3:",
    book.filter(pl.col("bid") >= 3).height,
    " mean n_bids there:",
    round(book.filter(pl.col("bid") >= 3)["n_bids"].mean() or 0, 1),
)
