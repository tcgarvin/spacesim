"""Why don't actors on biomass-poor planets starve?

Compares labor allocation, food stocks, money, and drive metrics on
biomass-poor planets (attribute 0.2-0.3) against biomass-rich ones (> 0.8).

    uv run spacesim2 dev analyze notebooks/poor_planet_food.py --run data/runs/run_20260907_103052
"""

import json
from pathlib import Path

import polars as pl

from spacesim2.analysis.loading import get_run_path_with_fallback

RUN = Path(get_run_path_with_fallback())
print(f"run: {RUN}")

attrs = json.loads((RUN / "planet_attributes.json").read_text())
poor = sorted(p for p, a in attrs.items() if 0.2 <= a["biomass"] <= 0.3)
rich = sorted(p for p, a in attrs.items() if a["biomass"] > 0.8)
planets = poor + rich
print(f"poor planets (biomass 0.2-0.3): {len(poor)}   rich (>0.8): {len(rich)}")

label = (
    pl.when(pl.col("planet_name").is_in(poor))
    .then(pl.lit("poor"))
    .otherwise(pl.lit("rich"))
)

INV = pl.Struct(
    [
        pl.Field("biomass", pl.Int64),
        pl.Field("food", pl.Int64),
        pl.Field("processed_food", pl.Int64),
    ]
)

turns = (
    pl.scan_parquet(RUN / "actor_turns.parquet")
    .filter(pl.col("planet_name").is_in(planets) & (pl.col("turn") >= 290))
    .select("turn", "actor_id", "money", "planet_name", "inventory_json")
    .with_columns(pl.col("inventory_json").str.json_decode(INV).alias("inv"))
    .with_columns(
        pl.col("inv").struct.field("biomass").fill_null(0).alias("biomass"),
        pl.col("inv").struct.field("food").fill_null(0).alias("food"),
        pl.col("inv").struct.field("processed_food").fill_null(0).alias("pfood"),
        label.alias("grp"),
    )
    .drop("inv", "inventory_json")
    .collect()
)

# Colonists only: actors that carry a food drive.
food_actors = (
    pl.scan_parquet(RUN / "actor_drives.parquet")
    .filter((pl.col("drive_name") == "food") & (pl.col("turn") == 600))
    .select("actor_id")
    .collect()["actor_id"]
)
turns = turns.filter(pl.col("actor_id").is_in(food_actors))
print(f"colonist actor-turns loaded: {turns.height}")

# --- 1. labor allocation from inventory deltas, turns 400-600 -------------
d = (
    turns.sort("actor_id", "turn")
    .with_columns(
        (pl.col("biomass") - pl.col("biomass").shift(1).over("actor_id")).alias("dbio"),
        (pl.col("food") - pl.col("food").shift(1).over("actor_id")).alias("dfood"),
    )
    .filter(pl.col("turn") >= 400)
    .with_columns(
        pl.when(pl.col("dbio") > 0)
        .then(pl.lit("gather"))
        .when((pl.col("dbio") < 0) & (pl.col("dfood") > 0))
        .then(pl.lit("cook"))
        .otherwise(pl.lit("other"))
        .alias("act")
    )
)
alloc = (
    d.group_by("grp", "act")
    .len()
    .with_columns(
        (pl.col("len") / pl.col("len").sum().over("grp")).round(3).alias("share")
    )
    .pivot(on="act", index="grp", values="share")
    .sort("grp")
)
print("\n[1] share of actor-turns by inferred activity (turns 400-600)")
print(alloc)

gain = (
    d.filter(pl.col("act") == "gather")
    .group_by("grp")
    .agg(pl.col("dbio").mean().round(2).alias("mean_biomass_gained_per_gather_turn"))
    .sort("grp")
)
print(gain)

# --- 2. stocks and money -------------------------------------------------
for t in (300, 600):
    snap = turns.filter(pl.col("turn") == t).with_columns(
        (pl.col("food") + pl.col("pfood")).alias("pantry")
    )
    print(f"\n[2] turn {t}: pantry units and money per colonist")
    print(
        snap.group_by("grp")
        .agg(
            pl.len().alias("actors"),
            pl.col("pantry").mean().round(2).alias("pantry_mean"),
            pl.col("pantry").median().alias("pantry_med"),
            pl.col("pantry").quantile(0.1).alias("pantry_p10"),
            pl.col("food").mean().round(2).alias("food_mean"),
            pl.col("pfood").mean().round(2).alias("pfood_mean"),
            pl.col("money").mean().round(0).alias("money_mean"),
            pl.col("money").median().alias("money_med"),
            pl.col("money").quantile(0.1).round(0).alias("money_p10"),
        )
        .sort("grp")
    )

never = (
    d.group_by("grp", "actor_id")
    .agg((pl.col("act") == "gather").sum().alias("gathers"))
    .group_by("grp")
    .agg(
        pl.len().alias("actors"),
        (pl.col("gathers") == 0).mean().round(3).alias("share_never_gathered"),
        pl.col("gathers").median().alias("median_gather_turns_of_200"),
    )
    .sort("grp")
)
print("\n[2b] actors that never gathered biomass in turns 400-600")
print(never)

# --- 3 & 4. drive metrics, last 100 turns --------------------------------
actor_planet = turns.filter(pl.col("turn") == 600).select("actor_id", "grp")
drives = (
    pl.scan_parquet(RUN / "actor_drives.parquet")
    .filter(pl.col("turn") >= 500)
    .select("turn", "actor_id", "drive_name", "health", "debt", "buffer")
    .collect()
    .join(actor_planet, on="actor_id", how="inner")
    .filter(pl.col("drive_name").is_in(["food", "clothing", "shelter", "health"]))
)
print("\n[3/4] drive metrics, turns 500-600, poor vs rich")
print(
    drives.group_by("drive_name", "grp")
    .agg(
        pl.col("health").mean().round(3).alias("health"),
        pl.col("debt").mean().round(3).alias("debt"),
        pl.col("buffer").mean().round(3).alias("buffer"),
        (pl.col("debt") > 0.80).mean().round(3).alias("pct_deprived"),
    )
    .sort("drive_name", "grp")
)

# --- 5. is the biomass locally produced or bought? ------------------------
snaps = (
    pl.scan_parquet(RUN / "market_snapshots.parquet")
    .filter(
        pl.col("planet_name").is_in(planets)
        & (pl.col("turn") >= 400)
        & pl.col("commodity_id").is_in(["biomass", "food", "processed_food"])
    )
    .select("planet_name", "commodity_id", "volume", "avg_price")
    .collect()
    .with_columns(label.alias("grp"))
)
print("\n[5] market volume per planet-turn, turns 400-600")
print(
    snaps.group_by("commodity_id", "grp")
    .agg(
        pl.col("volume").mean().round(2).alias("vol_per_planet_turn"),
        pl.col("avg_price").mean().round(1).alias("mean_price"),
    )
    .sort("commodity_id", "grp")
)
