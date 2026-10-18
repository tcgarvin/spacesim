"""Per-ship profit-and-loss decomposition from market transactions.

Ships appear as actors named ``Trader-N``. Every credit a ship spends or
earns moves through a market transaction (maintenance consumes goods, not
cash: ``Ship.perform_maintenance`` removes inventory), so transactions
should explain the money deltas in ``actor_turns``. The residual table
below tests that.

Run:
    uv run spacesim2 dev analyze notebooks/ship_pnl.py --run data/runs/<dir>
"""

import json

import polars as pl

from spacesim2.analysis.loading.utils import get_run_path_with_fallback

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_cols(20)
pl.Config.set_tbl_width_chars(200)

RUN = get_run_path_with_fallback()
BUCKET = 100
print(f"run: {RUN}")

tx = pl.read_parquet(f"{RUN}/market_transactions.parquet").with_columns(
    (pl.col("turn") // BUCKET * BUCKET).alias("bucket")
)
is_ship_buy = pl.col("buyer_name").str.starts_with("Trader-")
is_ship_sell = pl.col("seller_name").str.starts_with("Trader-")

buys = tx.filter(is_ship_buy).select(
    "turn",
    "bucket",
    "commodity_id",
    pl.col("buyer_name").alias("ship"),
    "quantity",
    "price",
    "total_amount",
)
sells = tx.filter(is_ship_sell).select(
    "turn",
    "bucket",
    "commodity_id",
    pl.col("seller_name").alias("ship"),
    "quantity",
    "price",
    "total_amount",
)
n_ships = pl.concat([buys["ship"], sells["ship"]]).n_unique()
print(f"ships trading: {n_ships}  buy rows: {len(buys)}  sell rows: {len(sells)}")


def by_bucket(frame: pl.DataFrame, label: str) -> pl.DataFrame:
    return frame.group_by("bucket", "commodity_id").agg(
        pl.col("total_amount").sum().alias(f"{label}_credits"),
        pl.col("quantity").sum().alias(f"{label}_qty"),
    )


# --- 1. fleet spend / revenue / margin by commodity and bucket -------------
spend = by_bucket(buys, "spend")
rev = by_bucket(sells, "rev")
flow = (
    spend.join(rev, on=["bucket", "commodity_id"], how="full", coalesce=True)
    .fill_null(0)
    .with_columns((pl.col("rev_credits") - pl.col("spend_credits")).alias("net"))
    .sort("bucket", "spend_credits", descending=[False, True])
)
print("\n== FLEET CASH FLOW BY COMMODITY AND BUCKET (top 5 spends per bucket) ==")
print(
    flow.filter(pl.col("spend_credits") > 0)
    .group_by("bucket")
    .head(5)
    .sort("bucket", "spend_credits", descending=[False, True])
)

print("\n== FLEET TOTALS BY BUCKET ==")
totals = (
    flow.group_by("bucket")
    .agg(
        pl.col("spend_credits").sum().alias("spend"),
        pl.col("rev_credits").sum().alias("revenue"),
        pl.col("net").sum().alias("net"),
    )
    .sort("bucket")
)
print(totals)

# --- 2. realized gross margin, matched per ship+commodity+bucket -----------
# Approximation: within a bucket, margin = sold_qty*mean_sell_price
# - bought_qty*mean_buy_price, only for (ship, commodity) pairs that both
# bought and sold in that bucket. Ignores inventory carried across buckets.
b = buys.group_by("ship", "commodity_id", "bucket").agg(
    pl.col("quantity").sum().alias("bq"),
    (pl.col("total_amount").sum() / pl.col("quantity").sum()).alias("bp"),
)
s = sells.group_by("ship", "commodity_id", "bucket").agg(
    pl.col("quantity").sum().alias("sq"),
    (pl.col("total_amount").sum() / pl.col("quantity").sum()).alias("sp"),
)
matched = b.join(s, on=["ship", "commodity_id", "bucket"], how="inner").with_columns(
    (pl.col("sq") * pl.col("sp") - pl.col("bq") * pl.col("bp")).alias("margin")
)
print("\n== REALIZED GROSS MARGIN (matched ship+commodity within bucket) ==")
print(
    matched.group_by("bucket", "commodity_id")
    .agg(
        pl.len().alias("pairs"),
        pl.col("margin").sum().round(0).alias("margin_total"),
        pl.col("margin").median().round(1).alias("margin_median"),
        (pl.col("sp") / pl.col("bp")).median().round(2).alias("sell_buy_ratio_med"),
    )
    .sort("bucket", "margin_total")
    .group_by("bucket")
    .head(6)
    .sort("bucket", "margin_total")
)

# --- 3. fuel ---------------------------------------------------------------
fuel_buys = buys.filter(pl.col("commodity_id") == "nova_fuel")
galaxy_fuel = (
    tx.filter(pl.col("commodity_id") == "nova_fuel")
    .group_by("bucket")
    .agg(
        (pl.col("total_amount").sum() / pl.col("quantity").sum())
        .round(2)
        .alias("galaxy_fuel_price"),
        pl.col("quantity").sum().alias("galaxy_fuel_qty"),
    )
)
fuel_tbl = (
    fuel_buys.group_by("bucket")
    .agg(
        pl.col("total_amount").sum().alias("ship_fuel_spend"),
        pl.col("quantity").sum().alias("ship_fuel_qty"),
        (pl.col("total_amount").sum() / pl.col("quantity").sum())
        .round(2)
        .alias("ship_price_paid"),
    )
    .join(galaxy_fuel, on="bucket", how="left")
    .sort("bucket")
)
print("\n== FUEL: SHIP PURCHASES VS GALAXY MEAN ==")
print(fuel_tbl)

# --- 4. residual: money deltas not explained by transactions --------------
at = (
    pl.scan_parquet(f"{RUN}/actor_turns.parquet")
    .filter(pl.col("actor_name").str.starts_with("Trader-"))
    .select("turn", "actor_name", "money", "reserved_money", "inventory_json")
    .collect()
)
money = (
    at.select("turn", pl.col("actor_name").alias("ship"), "money", "reserved_money")
    .with_columns((pl.col("money") + pl.col("reserved_money")).alias("total_money"))
    .sort("ship", "turn")
)
edges = (
    money.group_by("ship", (pl.col("turn") // BUCKET * BUCKET).alias("bucket"))
    .agg(
        pl.col("total_money").first().alias("m_first"),
        pl.col("total_money").last().alias("m_last"),
    )
    .with_columns((pl.col("m_last") - pl.col("m_first")).alias("delta"))
)
cash = (
    pl.concat(
        [
            buys.select("ship", "bucket", (-pl.col("total_amount")).alias("cf")),
            sells.select("ship", "bucket", pl.col("total_amount").alias("cf")),
        ]
    )
    .group_by("ship", "bucket")
    .agg(pl.col("cf").sum().alias("tx_net"))
)
res = (
    edges.join(cash, on=["ship", "bucket"], how="left")
    .fill_null(0)
    .with_columns((pl.col("delta") - pl.col("tx_net")).alias("residual"))
)
print("\n== MONEY DELTA VS TRANSACTION CASH FLOW (fleet, per bucket) ==")
print(
    res.group_by("bucket")
    .agg(
        pl.col("delta").sum().round(0).alias("money_delta"),
        pl.col("tx_net").sum().round(0).alias("tx_net"),
        pl.col("residual").sum().round(0).alias("residual_total"),
        pl.col("residual").median().round(1).alias("residual_median"),
    )
    .sort("bucket")
)

# --- 5. turns 0-200: worst losers -----------------------------------------
early = res.filter(pl.col("bucket") < 200)
loss = (
    early.group_by("ship")
    .agg(pl.col("delta").sum().alias("delta_0_200"))
    .sort("delta_0_200")
    .head(10)
)
print("\n== TOP 5 SPEND CATEGORIES, TURNS 0-199 and 100-199 ==")
print(
    flow.filter(pl.col("bucket") < 200)
    .group_by("commodity_id")
    .agg(
        pl.col("spend_credits").sum().alias("spend"),
        pl.col("rev_credits").sum().alias("revenue"),
        pl.col("net").sum().alias("net"),
        pl.col("spend_qty").sum().alias("bought_qty"),
        pl.col("rev_qty").sum().alias("sold_qty"),
    )
    .sort("spend", descending=True)
    .head(6)
)
print("\n== 10 BIGGEST MONEY LOSERS, TURNS 0-199: what they bought/sold ==")
losers = loss["ship"].to_list()
detail = (
    pl.concat(
        [
            buys.filter(pl.col("bucket") < 200).select(
                "ship",
                "commodity_id",
                pl.lit("buy").alias("side"),
                "quantity",
                "total_amount",
            ),
            sells.filter(pl.col("bucket") < 200).select(
                "ship",
                "commodity_id",
                pl.lit("sell").alias("side"),
                "quantity",
                "total_amount",
            ),
        ]
    )
    .filter(pl.col("ship").is_in(losers))
    .group_by("ship", "commodity_id", "side")
    .agg(
        pl.col("quantity").sum().alias("qty"),
        pl.col("total_amount").sum().alias("credits"),
        (pl.col("total_amount").sum() / pl.col("quantity").sum())
        .round(1)
        .alias("unit_price"),
    )
)
pivot = (
    detail.pivot(
        values=["qty", "credits", "unit_price"],
        index=["ship", "commodity_id"],
        on="side",
        aggregate_function="first",
    )
    .join(loss, on="ship")
    .sort("delta_0_200", "credits_buy", descending=[False, True])
    .group_by("ship", maintain_order=True)
    .head(3)
)
print(loss)
print(pivot)

# --- 6. buy-high-sell-low: next sale after each purchase -------------------
bl = buys.select("ship", "commodity_id", "turn", pl.col("price").alias("buy_price"))
sl = sells.select("ship", "commodity_id", "turn", pl.col("price").alias("sell_price"))
rt = (
    bl.sort("turn")
    .join_asof(
        sl.sort("turn"),
        on="turn",
        by=["ship", "commodity_id"],
        strategy="forward",
        suffix="_s",
    )
    .drop_nulls("sell_price")
    .with_columns((pl.col("sell_price") / pl.col("buy_price")).alias("ratio"))
)
print("\n== ROUND TRIPS: next-sale price / purchase price ==")
print(
    rt.group_by("commodity_id")
    .agg(
        pl.len().alias("n"),
        pl.col("ratio").median().round(2).alias("p50"),
        pl.col("ratio").quantile(0.1).round(2).alias("p10"),
        pl.col("ratio").quantile(0.9).round(2).alias("p90"),
        (pl.col("ratio") < 1).mean().round(3).alias("share_lt_1"),
    )
    .filter(pl.col("n") >= 20)
    .sort("n", descending=True)
    .head(12)
)

# --- 7. inventory snapshots -----------------------------------------------
print("\n== SHIP INVENTORY AT TURNS 200 / 400 / 600 ==")
rows = []
for t in (200, 400, 599):
    snap = at.filter(pl.col("turn") == t)
    if snap.is_empty():
        continue
    holdings: dict[str, list[int]] = {}
    cargo_no_fuel = 0
    total = 0
    for name, inv_json in zip(snap["actor_name"], snap["inventory_json"]):
        inv = json.loads(inv_json) if inv_json else {}
        total += 1
        fuel = inv.get("nova_fuel", 0)
        cargo = sum(q for c, q in inv.items() if c != "nova_fuel" and q > 0)
        if cargo > 0 and fuel == 0:
            cargo_no_fuel += 1
        for c, q in inv.items():
            holdings.setdefault(c, []).append(q)
    for c, qs in sorted(holdings.items(), key=lambda kv: -sum(kv[1]))[:6]:
        qs_sorted = sorted(qs)
        rows.append(
            {
                "turn": t,
                "commodity_id": c,
                "ships_holding": len(qs),
                "median_qty": qs_sorted[len(qs_sorted) // 2],
                "total_qty": sum(qs),
            }
        )
    print(f"turn {t}: ships={total} holding cargo but zero fuel={cargo_no_fuel}")
print(pl.DataFrame(rows))
