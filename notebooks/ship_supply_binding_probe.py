"""Is thin local supply the binding limit on ship loads?

For every ship purchase, compare what the ship took against everything that
cleared in that commodity on that planet-turn, and against the planet's
whole-window throughput.

Run: uv run spacesim dev analyze notebooks/ship_supply_binding_probe.py
"""

import polars as pl

from spacesim.analysis.loading import load_run

SHIP_PAT = r"^Trader-\d+$"

run = load_run()
tx = run.market_transactions
max_turn = int(tx["turn"].max())
lo = max_turn - 99
late = tx.filter(pl.col("turn") >= lo)

pt = late.group_by(["planet_name", "turn", "commodity_id"]).agg(
    pl.col("quantity").sum().alias("planet_turn_units")
)
ship_buys = (
    late.filter(pl.col("buyer_name").str.contains(SHIP_PAT))
    .group_by(["planet_name", "turn", "commodity_id", "buyer_name"])
    .agg(pl.col("quantity").sum().alias("ship_units"))
    .join(pt, on=["planet_name", "turn", "commodity_id"], how="left")
    .with_columns(
        (100 * pl.col("ship_units") / pl.col("planet_turn_units")).alias("pct_of_clear")
    )
)
print("=== ship purchase as share of all units clearing on that planet-turn ===")
print(ship_buys.select("ship_units", "planet_turn_units", "pct_of_clear").describe())
print(
    f"\nship buy events where ship took >=90% of the planet-turn's clearing: "
    f"{ship_buys.filter(pl.col('pct_of_clear') >= 90).height} / {ship_buys.height} "
    f"({100 * ship_buys.filter(pl.col('pct_of_clear') >= 90).height / ship_buys.height:.1f}%)"
)

# Galaxy throughput per planet-turn for the goods ships actually move.
thru = (
    late.group_by("commodity_id")
    .agg(
        (pl.col("quantity").sum() / (50 * 100)).round(3).alias("units_per_planet_turn"),
        pl.col("quantity").sum().alias("galaxy_units_100t"),
        pl.col("total_amount").sum().alias("galaxy_value_100t"),
    )
    .with_columns(
        (pl.col("galaxy_value_100t") / pl.col("galaxy_units_100t"))
        .round(1)
        .alias("unit_px")
    )
    .sort("galaxy_value_100t", descending=True)
)
print("\n=== galaxy throughput per commodity, late 100 turns ===")
with pl.Config(tbl_rows=35, tbl_width_chars=200):
    print(thru)

local = ["processed_food", "biomass", "food", "wood", "fiber"]
tot_u = thru["galaxy_units_100t"].sum()
loc_u = thru.filter(pl.col("commodity_id").is_in(local))["galaxy_units_100t"].sum()
print(
    f"\nlow-value local staples ({', '.join(local)}) are "
    f"{100 * loc_u / tot_u:.1f}% of all units cleared"
)
