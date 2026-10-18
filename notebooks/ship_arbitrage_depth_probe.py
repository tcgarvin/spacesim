"""Parts 3 and 4: unexploited cross-planet spreads, and order-book depth.

Run: uv run spacesim dev analyze notebooks/ship_arbitrage_depth_probe.py
"""

import polars as pl

from spacesim.analysis.loading import load_run

SHIP_PAT = r"^Trader-\d+$"
FUEL_PRICE = 24.0  # summary.trade.ship_fuel_price

run = load_run()
snap = run.market_snapshots
tx = run.market_transactions
max_turn = int(snap["turn"].max())
lo = max_turn - 99
print(f"late window {lo}-{max_turn}")

late = snap.filter(pl.col("turn") >= lo)

# Per-planet late-window price and resting depth (depth is ORDER COUNT; the
# snapshot schema has no resting units column).
pp = late.group_by(["commodity_id", "planet_name"]).agg(
    pl.col("avg_price").mean().alias("px"),
    pl.col("best_bid").filter(pl.col("best_bid") > 0).mean().alias("bid"),
    pl.col("best_ask").filter(pl.col("best_ask") > 0).mean().alias("ask"),
    pl.col("num_buy_orders").mean().alias("bid_orders"),
    pl.col("num_sell_orders").mean().alias("ask_orders"),
)

hi = (
    pp.filter(pl.col("bid").is_not_null())
    .sort("bid", descending=True)
    .group_by("commodity_id")
    .first()
    .select(
        "commodity_id",
        pl.col("bid").alias("max_bid"),
        pl.col("planet_name").alias("bid_planet"),
        pl.col("bid_orders").alias("bid_orders_hi"),
    )
)
lowask = (
    pp.filter(pl.col("ask").is_not_null())
    .sort("ask")
    .group_by("commodity_id")
    .first()
    .select(
        "commodity_id",
        pl.col("ask").alias("min_ask"),
        pl.col("planet_name").alias("ask_planet"),
        pl.col("ask_orders").alias("ask_orders_lo"),
    )
)
med = pp.group_by("commodity_id").agg(
    pl.col("px").median().alias("median_planet_px"),
    pl.col("px").quantile(0.9).alias("p90_px"),
    pl.col("px").quantile(0.1).alias("p10_px"),
    pl.col("planet_name").n_unique().alias("planets"),
    pl.col("bid_orders").mean().alias("bid_orders_mean"),
    pl.col("ask_orders").mean().alias("ask_orders_mean"),
)

ship_delivered = (
    tx.filter((pl.col("turn") >= lo) & pl.col("seller_name").str.contains(SHIP_PAT))
    .group_by("commodity_id")
    .agg(pl.col("quantity").sum().alias("ship_units"))
)

table = (
    med.join(hi, on="commodity_id", how="left")
    .join(lowask, on="commodity_id", how="left")
    .join(ship_delivered, on="commodity_id", how="left")
    .with_columns(
        (pl.col("max_bid") - pl.col("min_ask")).alias("spread"),
        (pl.col("p90_px") - pl.col("p10_px")).alias("p90_p10_px_spread"),
        pl.col("ship_units").fill_null(0),
    )
    .with_columns(
        (100 * pl.col("spread") / pl.col("median_planet_px")).alias("spread_pct")
    )
    .sort("spread", descending=True)
)
print(
    "\n=== PART 3: cross-planet arbitrage, late window "
    "(bid/ask are mean best quotes; spread = max-bid planet - min-ask planet) ==="
)
with pl.Config(tbl_rows=35, tbl_width_chars=230, float_precision=1):
    print(
        table.select(
            "commodity_id",
            "median_planet_px",
            "max_bid",
            "min_ask",
            "spread",
            "spread_pct",
            "p90_p10_px_spread",
            "bid_orders_hi",
            "ask_orders_lo",
            "ship_units",
        )
    )

print(
    f"\nfuel price {FUEL_PRICE}/unit; a 1-unit-per-hop burn over a 100-unit hold "
    f"is {FUEL_PRICE / 100:.2f} credits per cargo unit per hop."
)

# --- Part 4: depth, expressed in units via realised transaction sizes ------
sizes = tx.filter(pl.col("turn") >= lo)
q = (
    sizes.group_by("commodity_id")
    .agg(
        pl.col("quantity").mean().alias("mean_tx_units"),
        pl.col("quantity").median().alias("med_tx_units"),
        pl.col("quantity").quantile(0.95).alias("p95_tx_units"),
        pl.col("quantity").max().alias("max_tx_units"),
        pl.len().alias("n_tx"),
        pl.col("quantity").sum().alias("units"),
    )
    .sort("units", descending=True)
)
print("\n=== PART 4a: realised transaction sizes, late window (all actors) ===")
with pl.Config(tbl_rows=35, tbl_width_chars=200, float_precision=2):
    print(q)

ship_sizes = sizes.filter(
    pl.col("seller_name").str.contains(SHIP_PAT)
    | pl.col("buyer_name").str.contains(SHIP_PAT)
)
print("\n=== PART 4b: ship-side transaction sizes vs everyone else ===")
print(
    ship_sizes.select(
        pl.len().alias("n_ship_tx"),
        pl.col("quantity").mean().round(2).alias("mean_units"),
        pl.col("quantity").median().alias("median_units"),
        pl.col("quantity").max().alias("max_units"),
    )
)
print(
    sizes.filter(
        ~(
            pl.col("seller_name").str.contains(SHIP_PAT)
            | pl.col("buyer_name").str.contains(SHIP_PAT)
        )
    ).select(
        pl.len().alias("n_other_tx"),
        pl.col("quantity").mean().round(2).alias("mean_units"),
        pl.col("quantity").median().alias("median_units"),
        pl.col("quantity").max().alias("max_units"),
    )
)

# Book depth in orders at the extremes, and how many planets quote at all.
print("\n=== PART 4c: mean resting order COUNTS per planet-turn, late window ===")
with pl.Config(tbl_rows=35, tbl_width_chars=200, float_precision=2):
    print(
        med.select(
            "commodity_id", "planets", "bid_orders_mean", "ask_orders_mean"
        ).sort("ask_orders_mean", descending=True)
    )

# Does a ship-visited planet even have a resting ask when the ship is there?
print("\n=== PART 4d: share of planet-turns with zero resting asks / bids ===")
print(
    late.group_by("commodity_id")
    .agg(
        (pl.col("num_sell_orders") == 0).mean().round(3).alias("zero_ask_share"),
        (pl.col("num_buy_orders") == 0).mean().round(3).alias("zero_bid_share"),
    )
    .sort("zero_ask_share", descending=True)
    .head(15)
)
