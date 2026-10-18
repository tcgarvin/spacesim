"""Two-track food chain: does the industrial track reach biomass-poor planets,
and does processed_food surplus ever leave the planet that made it?

    uv run spacesim2 dev analyze notebooks/food_geography.py
    uv run spacesim2 dev analyze notebooks/food_geography.py --run data/runs/<dir>

H1: biomass-poor planets build farms and chemical plants and feed themselves
    locally instead of importing.
H2: planets holding a large processed_food stock are not exporting it.

Needs a run exported with --log-actors all for the inventory tables; with a
single logged actor the per-planet stock and facility columns are near-empty
and the script says so instead of failing.
"""

import json

import polars as pl

from spacesim2.analysis.loading import load_run

pl.Config.set_tbl_cols(-1)
pl.Config.set_tbl_rows(20)
pl.Config.set_tbl_width_chars(200)
pl.Config.set_fmt_str_lengths(20)

FOOD_GOODS = ("food", "processed_food")
FACILITIES = ("farm", "chemical_plant")
SHIP_NAME = r"^Trader-\d+$"  # ships trade as actors named Trader-N (id actor-Trader-N)
RECENT_TX_TURNS = 100
RECENT_DRIVE_TURNS = 50
RECENT_SNAP_TURNS = 50
TOP_N = 15

r = load_run()
at = r.actor_turns
tx = r.market_transactions
snap = r.market_snapshots
drives = r.actor_drives

last_turn = int(tx["turn"].max())
final_at_turn = int(at["turn"].max()) if at.height else 0
n_logged = at["actor_id"].n_unique() if at.height else 0
print(f"run: {r.simulation_id}  turns 1..{last_turn}  actors logged: {n_logged}")
if n_logged < 10:
    print("WARNING: few actors logged; inventory/facility/stock columns are thin.")

# --- planet attributes -------------------------------------------------------
with open(r.run_path / "planet_attributes.json") as fh:
    attrs = json.load(fh)
bio = pl.DataFrame(
    {
        "planet_name": list(attrs.keys()),
        "biomass_attr": [float(v.get("biomass", 1.0)) for v in attrs.values()],
    }
)
print(f"planets: {bio.height}  biomass attr mean {bio['biomass_attr'].mean():.2f}")

# --- inventories at the final logged turn -----------------------------------
final_inv = at.filter(pl.col("turn") == final_at_turn)
rows: list[dict[str, object]] = []
for planet, blob in zip(final_inv["planet_name"], final_inv["inventory_json"]):
    for commodity, qty in json.loads(blob).items():
        rows.append({"planet_name": planet, "commodity_id": commodity, "qty": int(qty)})
inv = (
    pl.DataFrame(
        rows, schema={"planet_name": pl.Utf8, "commodity_id": pl.Utf8, "qty": pl.Int64}
    )
    if rows
    else pl.DataFrame(
        schema={"planet_name": pl.Utf8, "commodity_id": pl.Utf8, "qty": pl.Int64}
    )
)
inv_wide = (
    inv.group_by("planet_name", "commodity_id")
    .agg(pl.col("qty").sum())
    .pivot(on="commodity_id", index="planet_name", values="qty")
    if inv.height
    else pl.DataFrame(schema={"planet_name": pl.Utf8})
)
for col in (*FACILITIES, "processed_food", "food", "biomass"):
    if col not in inv_wide.columns:
        inv_wide = inv_wide.with_columns(pl.lit(0, dtype=pl.Int64).alias(col))
inv_wide = inv_wide.with_columns(
    pl.col(*FACILITIES, "processed_food", "food", "biomass").fill_null(0)
)

# --- transactions, last RECENT_TX_TURNS -------------------------------------
recent_tx = tx.filter(
    (pl.col("turn") > last_turn - RECENT_TX_TURNS)
    & pl.col("commodity_id").is_in(FOOD_GOODS)
).with_columns(
    pl.col("seller_name").str.contains(SHIP_NAME).alias("seller_is_ship"),
    pl.col("buyer_name").str.contains(SHIP_NAME).alias("buyer_is_ship"),
)


def _vol(
    frame: pl.DataFrame, commodity: str, alias: str, extra: pl.Expr | None = None
) -> pl.DataFrame:
    sel = frame.filter(pl.col("commodity_id") == commodity)
    if extra is not None:
        sel = sel.filter(extra)
    return sel.group_by("planet_name").agg(pl.col("quantity").sum().alias(alias))


vol = (
    bio.join(_vol(recent_tx, "food", "food_vol"), on="planet_name", how="left")
    .join(_vol(recent_tx, "processed_food", "pfood_vol"), on="planet_name", how="left")
    .join(
        _vol(recent_tx, "food", "food_from_ship", pl.col("seller_is_ship")),
        on="planet_name",
        how="left",
    )
    .join(
        _vol(recent_tx, "processed_food", "pfood_from_ship", pl.col("seller_is_ship")),
        on="planet_name",
        how="left",
    )
    .join(
        _vol(recent_tx, "processed_food", "pfood_to_ship", pl.col("buyer_is_ship")),
        on="planet_name",
        how="left",
    )
    .join(
        _vol(recent_tx, "food", "food_to_ship", pl.col("buyer_is_ship")),
        on="planet_name",
        how="left",
    )
    .fill_null(0)
)

price = (
    recent_tx.group_by("planet_name", "commodity_id")
    .agg(pl.col("price").mean().alias("p"))
    .pivot(on="commodity_id", index="planet_name", values="p")
)
for col in FOOD_GOODS:
    if col not in price.columns:
        price = price.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
price = price.rename({"food": "food_price", "processed_food": "pfood_price"})

# --- food drive health per planet -------------------------------------------
actor_planet = (
    at.filter(pl.col("turn") == final_at_turn)
    .select("actor_id", "planet_name")
    .unique()
)
health = (
    drives.filter(
        (pl.col("drive_name") == "food")
        & (pl.col("turn") > last_turn - RECENT_DRIVE_TURNS)
    )
    .join(actor_planet, on="actor_id", how="inner")
    .group_by("planet_name")
    .agg(
        pl.col("health").mean().alias("food_health"),
        pl.col("actor_id").n_unique().alias("n_actors"),
    )
)

h1 = (
    bio.join(inv_wide.select("planet_name", *FACILITIES), on="planet_name", how="left")
    .join(vol.drop("biomass_attr"), on="planet_name", how="left")
    .join(price, on="planet_name", how="left")
    .join(health, on="planet_name", how="left")
    .with_columns(pl.col("farm", "chemical_plant").fill_null(0))
    .with_columns(
        (pl.col("food_vol") + pl.col("pfood_vol")).alias("local_vol"),
        (pl.col("food_from_ship") + pl.col("pfood_from_ship")).alias("ship_in"),
    )
    .with_columns(
        (
            pl.col("ship_in")
            / pl.when(pl.col("local_vol") > 0).then(pl.col("local_vol")).otherwise(None)
        ).alias("import_share")
    )
)

print("\n=== H1: lowest-biomass planets (per planet) ===")
print(
    h1.sort("biomass_attr")
    .select(
        "planet_name",
        pl.col("biomass_attr").round(2),
        "farm",
        "chemical_plant",
        "food_vol",
        "pfood_vol",
        pl.col("food_price").round(1),
        pl.col("pfood_price").round(1),
        pl.col("food_health").round(2),
        "ship_in",
        pl.col("import_share").round(3),
    )
    .head(TOP_N)
)

terciles = h1.with_columns(
    pl.when(pl.col("biomass_attr") <= pl.col("biomass_attr").quantile(1 / 3))
    .then(pl.lit("1-low"))
    .when(pl.col("biomass_attr") <= pl.col("biomass_attr").quantile(2 / 3))
    .then(pl.lit("2-mid"))
    .otherwise(pl.lit("3-high"))
    .alias("tercile")
)
print("\n=== H1: aggregates by biomass tercile ===")
print(
    terciles.group_by("tercile")
    .agg(
        pl.len().alias("planets"),
        pl.col("biomass_attr").mean().round(2).alias("biomass"),
        pl.col("farm").mean().round(2).alias("farms_pp"),
        pl.col("chemical_plant").mean().round(2).alias("plants_pp"),
        pl.col("food_vol").mean().round(1),
        pl.col("pfood_vol").mean().round(1),
        pl.col("import_share").mean().round(3),
        pl.col("food_health").mean().round(3),
        pl.col("food_price").mean().round(1),
        pl.col("pfood_price").mean().round(1),
    )
    .sort("tercile")
)

for col in ("farm", "chemical_plant", "food_health", "import_share"):
    pair = h1.select("biomass_attr", col).drop_nulls()
    corr = (
        pair.select(pl.corr("biomass_attr", col)).item()
        if pair.height > 2 and pair[col].std() not in (None, 0.0)
        else None
    )
    print(f"corr(biomass_attr, {col}) = {corr if corr is None else round(corr, 3)}")

# --- H2: processed_food surplus and export ----------------------------------
pf_snap = snap.filter(
    (pl.col("commodity_id") == "processed_food")
    & (pl.col("turn") > last_turn - RECENT_SNAP_TURNS)
)
book = pf_snap.group_by("planet_name").agg(
    pl.col("best_ask").filter(pl.col("best_ask") > 0).mean().round(1).alias("ask"),
    pl.col("best_bid").filter(pl.col("best_bid") > 0).mean().round(1).alias("bid"),
    ((pl.col("num_sell_orders") > 0) & (pl.col("num_buy_orders") == 0))
    .sum()
    .alias("ask_no_bid_turns"),
)

h2 = (
    bio.join(
        inv_wide.select("planet_name", pl.col("processed_food").alias("pf_stock")),
        on="planet_name",
        how="left",
    )
    .join(
        vol.select("planet_name", "pfood_vol", "pfood_to_ship", "pfood_from_ship"),
        on="planet_name",
        how="left",
    )
    .join(book, on="planet_name", how="left")
    .with_columns(
        pl.col(
            "pf_stock",
            "pfood_vol",
            "pfood_to_ship",
            "pfood_from_ship",
            "ask_no_bid_turns",
        ).fill_null(0)
    )
)
print(
    f"\n=== H2: top {TOP_N} planets by processed_food stock (final turn {final_at_turn}) ==="
)
print(
    h2.sort("pf_stock", descending=True)
    .select(
        "planet_name",
        "pf_stock",
        pl.col("biomass_attr").round(2),
        pl.col("pfood_vol").alias("local_vol"),
        pl.col("pfood_to_ship").alias("to_ship"),
        pl.col("pfood_from_ship").alias("from_ship"),
        "ask",
        "bid",
        "ask_no_bid_turns",
    )
    .head(TOP_N)
)
big = h2.filter(pl.col("pf_stock") > 1000)
exporting = big.filter(pl.col("pfood_to_ship") > 0)
print(
    f"planets holding >1000 processed_food: {big.height}; "
    f"of those, sold any to a ship in the last {RECENT_TX_TURNS} turns: {exporting.height}"
)
print(
    f"galaxy stock {int(h2['pf_stock'].sum())} units; "
    f"turns with a processed_food ask and no bid (mean per planet, last {RECENT_SNAP_TURNS}): "
    f"{h2['ask_no_bid_turns'].mean():.1f}"
)

# --- ship-moved vs local volume over time -----------------------------------
buckets = (
    tx.filter(pl.col("commodity_id").is_in(FOOD_GOODS))
    .with_columns(
        ((pl.col("turn") - 1) // 100 * 100).alias("bucket"),
        pl.col("seller_name").str.contains(SHIP_NAME).alias("seller_is_ship"),
        pl.col("buyer_name").str.contains(SHIP_NAME).alias("buyer_is_ship"),
    )
    .group_by("bucket", "commodity_id")
    .agg(
        pl.col("quantity").sum().alias("total_vol"),
        pl.col("quantity")
        .filter(pl.col("buyer_is_ship"))
        .sum()
        .alias("bought_by_ships"),
        pl.col("quantity")
        .filter(pl.col("seller_is_ship"))
        .sum()
        .alias("sold_by_ships"),
    )
    .with_columns(
        (pl.col("bought_by_ships") / pl.col("total_vol"))
        .round(3)
        .alias("ship_buy_share")
    )
    .sort("commodity_id", "bucket")
)
print("\n=== ship-moved vs local food volume, 100-turn buckets ===")
print(buckets)
