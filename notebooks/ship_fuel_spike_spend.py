"""How much of the fleet's fuel spend happens at spike prices, and what it costs.

Reference price per turn approximates ``Navigator.fuel_value_reference``:
the median over planets of each planet's trailing 30-turn volume-weighted
fuel price. A ship fuel purchase is "spiked" when its price exceeds
FUEL_BUNKER_PREMIUM (1.3) times that reference, the same line the bunker
rule draws. The premium is quantity times (price - reference): what the
ship paid over the galaxy-typical price.

Run:
    uv run spacesim2 dev analyze notebooks/ship_fuel_spike_spend.py
"""

import polars as pl

from spacesim2.analysis.loading.utils import get_run_path_with_fallback

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_cols(20)
pl.Config.set_tbl_width_chars(200)

RUN = get_run_path_with_fallback()
BUCKET = 100
REF_WINDOW = 30
PREMIUM = 1.3
print(f"run: {RUN}")

tx = pl.read_parquet(f"{RUN}/market_transactions.parquet")
fuel_tx = tx.filter(pl.col("commodity_id") == "nova_fuel")

# --- reference price per turn -------------------------------------------
per_planet_turn = (
    fuel_tx.group_by("planet_name", "turn")
    .agg(pl.col("total_amount").sum().alias("amt"), pl.col("quantity").sum().alias("q"))
    .sort("planet_name", "turn")
)
turns = pl.DataFrame(
    {"turn": pl.arange(1, tx["turn"].max() + 1, eager=True).cast(pl.Int32)}
)
planets = per_planet_turn.select("planet_name").unique()
grid = (
    planets.join(turns, how="cross")
    .join(per_planet_turn, on=["planet_name", "turn"], how="left")
    .fill_null(0)
)
rolled = (
    grid.sort("planet_name", "turn")
    .with_columns(
        pl.col("amt")
        .rolling_sum(REF_WINDOW, min_samples=1)
        .over("planet_name")
        .alias("ramt"),
        pl.col("q")
        .rolling_sum(REF_WINDOW, min_samples=1)
        .over("planet_name")
        .alias("rq"),
    )
    .filter(pl.col("rq") > 0)
    .with_columns((pl.col("ramt") / pl.col("rq")).alias("planet_ref"))
)
reference = rolled.group_by("turn").agg(
    pl.col("planet_ref").median().alias("ref"),
    pl.len().alias("planets_with_ref"),
)
print("\n== GALAXY FUEL REFERENCE BY 100-TURN BUCKET ==")
print(
    reference.with_columns((pl.col("turn") // BUCKET * BUCKET).alias("bucket"))
    .group_by("bucket")
    .agg(
        pl.col("ref").median().round(1).alias("ref_median"),
        pl.col("ref").max().round(1).alias("ref_max"),
    )
    .sort("bucket")
)

# --- ship fuel purchases against the reference ---------------------------
is_ship = pl.col("buyer_name").str.starts_with("Trader-")
buys = (
    fuel_tx.filter(is_ship)
    .join(reference, on="turn", how="left")
    .with_columns(
        (pl.col("turn") // BUCKET * BUCKET).alias("bucket"),
        (pl.col("price") / pl.col("ref")).alias("ratio"),
        (pl.col("quantity") * (pl.col("price") - pl.col("ref"))).alias("premium"),
        pl.when(pl.col("seller_name").str.contains("Operator"))
        .then(pl.lit("operator"))
        .when(pl.col("seller_name").str.contains("Industrialist"))
        .then(pl.lit("industrialist"))
        .when(pl.col("seller_name").str.starts_with("Trader-"))
        .then(pl.lit("ship"))
        .otherwise(pl.lit("other"))
        .alias("seller_kind"),
    )
    .with_columns(
        pl.when(pl.col("ratio") <= PREMIUM)
        .then(pl.lit("a<=1.3"))
        .when(pl.col("ratio") <= 2)
        .then(pl.lit("b1.3-2"))
        .when(pl.col("ratio") <= 4)
        .then(pl.lit("c2-4"))
        .otherwise(pl.lit("d>4"))
        .alias("band")
    )
)
print(
    f"\nship fuel buy rows: {len(buys)}  units: {buys['quantity'].sum()}  credits: {buys['total_amount'].sum()}"
)

print("\n== SHIP FUEL SPEND BY PRICE BAND (ratio = price / reference) ==")
band = (
    buys.group_by("band")
    .agg(
        pl.len().alias("rows"),
        pl.col("quantity").sum().alias("units"),
        pl.col("total_amount").sum().alias("credits"),
        pl.col("premium").sum().round(0).alias("premium_over_ref"),
        pl.col("ratio").median().round(2).alias("ratio_med"),
    )
    .sort("band")
    .with_columns(
        (pl.col("credits") / pl.col("credits").sum()).round(3).alias("credit_share")
    )
)
print(band)

print("\n== SPIKED SHARE OF SHIP FUEL SPEND BY BUCKET ==")
by_bucket = (
    buys.group_by("bucket")
    .agg(
        pl.col("total_amount").sum().alias("fuel_credits"),
        pl.col("total_amount")
        .filter(pl.col("ratio") > PREMIUM)
        .sum()
        .alias("spiked_credits"),
        pl.col("premium")
        .filter(pl.col("ratio") > PREMIUM)
        .sum()
        .round(0)
        .alias("spike_premium"),
        pl.col("quantity").sum().alias("units"),
        pl.col("quantity")
        .filter(pl.col("ratio") > PREMIUM)
        .sum()
        .alias("spiked_units"),
    )
    .with_columns(
        (pl.col("spiked_credits") / pl.col("fuel_credits"))
        .round(3)
        .alias("spiked_share")
    )
    .sort("bucket")
)
print(by_bucket)

print("\n== WHO SELLS SPIKED FUEL TO SHIPS ==")
print(
    buys.filter(pl.col("ratio") > PREMIUM)
    .group_by("seller_kind")
    .agg(
        pl.col("quantity").sum().alias("units"),
        pl.col("total_amount").sum().alias("credits"),
        pl.col("ratio").median().round(2).alias("ratio_med"),
    )
    .sort("credits", descending=True)
)

# --- what the premium costs relative to trading margin --------------------
non_fuel = tx.filter(pl.col("commodity_id") != "nova_fuel").with_columns(
    (pl.col("turn") // BUCKET * BUCKET).alias("bucket")
)
cargo_spend = (
    non_fuel.filter(is_ship)
    .group_by("bucket")
    .agg(pl.col("total_amount").sum().alias("cargo_spend"))
)
cargo_rev = (
    non_fuel.filter(pl.col("seller_name").str.starts_with("Trader-"))
    .group_by("bucket")
    .agg(pl.col("total_amount").sum().alias("cargo_rev"))
)
fuel_rev = (
    fuel_tx.filter(pl.col("seller_name").str.starts_with("Trader-"))
    .with_columns((pl.col("turn") // BUCKET * BUCKET).alias("bucket"))
    .group_by("bucket")
    .agg(pl.col("total_amount").sum().alias("fuel_rev"))
)
pnl = (
    cargo_spend.join(cargo_rev, on="bucket", how="full", coalesce=True)
    .join(fuel_rev, on="bucket", how="full", coalesce=True)
    .join(
        by_bucket.select("bucket", "fuel_credits", "spike_premium"),
        on="bucket",
        how="full",
        coalesce=True,
    )
    .fill_null(0)
    .with_columns(
        (pl.col("cargo_rev") - pl.col("cargo_spend")).alias("cargo_gross"),
        (
            pl.col("cargo_rev")
            - pl.col("cargo_spend")
            + pl.col("fuel_rev")
            - pl.col("fuel_credits")
        ).alias("net_cash"),
    )
    .sort("bucket")
)
print("\n== FLEET CASH: cargo gross margin vs fuel spend vs spike premium ==")
print(pnl)

# --- per-ship: does spike premium predict ending poor? -------------------
actor_turns = pl.read_parquet(f"{RUN}/actor_turns.parquet").filter(
    pl.col("actor_name").str.starts_with("Trader-")
)
last_turn = actor_turns["turn"].max()
end_money = actor_turns.filter(pl.col("turn") == last_turn).select(
    pl.col("actor_name").alias("ship"),
    (pl.col("money") + pl.col("reserved_money")).alias("end_money"),
)
start_money = actor_turns.filter(pl.col("turn") == 1).select(
    pl.col("actor_name").alias("ship"),
    (pl.col("money") + pl.col("reserved_money")).alias("start_money"),
)
per_ship = (
    buys.group_by(pl.col("buyer_name").alias("ship"))
    .agg(
        pl.col("total_amount").sum().alias("fuel_spend"),
        pl.col("premium")
        .filter(pl.col("ratio") > PREMIUM)
        .sum()
        .round(0)
        .alias("spike_premium"),
        pl.col("quantity").sum().alias("fuel_units"),
    )
    .join(end_money, on="ship", how="left")
    .join(start_money, on="ship", how="left")
    .with_columns((pl.col("end_money") - pl.col("start_money")).alias("delta"))
)
print("\n== PER-SHIP: spike premium vs money change, by end-money quartile ==")
q = per_ship.with_columns(
    pl.col("end_money")
    .qcut(4, labels=["q1_poorest", "q2", "q3", "q4_richest"])
    .alias("quartile")
)
print(
    q.group_by("quartile")
    .agg(
        pl.len().alias("ships"),
        pl.col("end_money").median().round(0).alias("end_money_med"),
        pl.col("delta").median().round(0).alias("delta_med"),
        pl.col("fuel_spend").median().round(0).alias("fuel_spend_med"),
        pl.col("spike_premium").median().round(0).alias("spike_premium_med"),
        pl.col("fuel_units").median().round(0).alias("fuel_units_med"),
    )
    .sort("quartile")
)
print(
    "corr(spike_premium, delta) =",
    round(per_ship.select(pl.corr("spike_premium", "delta")).item(), 3),
    " corr(fuel_spend, delta) =",
    round(per_ship.select(pl.corr("fuel_spend", "delta")).item(), 3),
)

# --- are spikes local? cheaper ask elsewhere at the same turn -------------
snaps = pl.read_parquet(f"{RUN}/market_snapshots.parquet").filter(
    (pl.col("commodity_id") == "nova_fuel") & (pl.col("best_ask") > 0)
)
snaps = snaps.join(reference, on="turn", how="left").with_columns(
    (pl.col("best_ask") / pl.col("ref")).alias("ask_ratio")
)
ask_planets_per_turn = snaps.group_by("turn").agg(
    pl.len().alias("ask_planets"),
    (pl.col("ask_ratio") <= PREMIUM).sum().alias("cheap_ask_planets"),
)
spike_turns = buys.filter(pl.col("ratio") > PREMIUM).select("turn").unique()
print(
    "\n== AT TURNS WITH A SPIKED SHIP BUY: planets with any fuel ask, and with a cheap one =="
)
print(
    ask_planets_per_turn.join(spike_turns, on="turn", how="inner").select(
        pl.col("ask_planets").median().alias("ask_planets_med"),
        pl.col("cheap_ask_planets").median().alias("cheap_ask_planets_med"),
        (pl.col("cheap_ask_planets") == 0)
        .mean()
        .round(3)
        .alias("share_turns_no_cheap_ask_anywhere"),
    )
)
