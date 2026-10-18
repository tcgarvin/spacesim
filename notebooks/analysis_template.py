import marimo

__generated_with = "0.18.1"
app = marimo.App()


@app.cell
def _():
    import os
    from pathlib import Path

    import marimo as mo
    import plotly.express as px
    import polars as pl

    from spacesim2.analysis.loading.loader import SimulationData

    return Path, SimulationData, mo, os, pl, px


@app.cell
def _(mo, os, Path):
    from spacesim2.analysis.loading import (
        NoRunsFoundError,
        get_run_path_with_fallback,
    )

    try:
        auto_run_path = get_run_path_with_fallback()
        run_path_str = str(auto_run_path)
        status_msg = f"✓ Using run: **{auto_run_path.name}**"

        if os.getenv("SPACESIM_RUN_PATH"):
            status_msg += " (from SPACESIM_RUN_PATH)"
        else:
            status_msg += " (auto-detected)"

    except NoRunsFoundError as e:
        run_path_str = ""
        status_msg = f"⚠️ **No runs found**\n\n```\n{str(e)}\n```"

    run_selector = mo.ui.text(
        value=run_path_str,
        label="Run Path (edit to override):",
        full_width=True,
    )

    mo.md(f"""
    # SpaceSim2 Analysis

    {status_msg}

    {run_selector}
    """)

    return (run_selector,)


@app.cell
def _(Path, SimulationData, mo, run_selector):
    if not run_selector.value:
        mo.md("⚠️ No run path specified. Run `spacesim2 run` first.")
        data = None
    else:
        try:
            data = SimulationData(Path(run_selector.value))
            mo.md("✓ Data loaded successfully")
        except Exception as e:
            mo.md(f"❌ Error loading data: {e}")
            data = None

    return (data,)


@app.cell
def _(data, mo):
    if data is None:
        mo.md("## Simulation Overview\n\nNo data loaded")
    else:
        mo.md(f"""
        ## Simulation Overview
        - **Turns:** {data.actor_turns["turn"].max() if len(data.actor_turns) > 0 else "N/A"}
        - **Actors:** {data.actor_turns["actor_id"].n_unique() if len(data.actor_turns) > 0 else "N/A"}
        - **Transactions:** {len(data.market_transactions)}
        - **Market Snapshots:** {len(data.market_snapshots)}
        """)
    return


@app.cell
def _(mo):
    mo.md("""
    ## Actor Behavior Over Time
    """)
    return


@app.cell
def _(data, px):
    if data is None:
        "No data loaded"
    elif len(data.actor_turns) > 0:
        actor_money = data.actor_turns.select(
            ["turn", "actor_id", "actor_name", "money"]
        )
        fig_money = px.line(
            actor_money.to_pandas(),
            x="turn",
            y="money",
            color="actor_name",
            title="Actor Money Over Time",
            labels={"money": "Money ($)", "turn": "Turn"},
        )
        fig_money
    else:
        "No actor turn data available"
    return


@app.cell
def _(mo):
    mo.md("""
    ## Market Dynamics
    """)
    return


@app.cell
def _(data, px):
    if data is None:
        "No data loaded"
    elif len(data.market_snapshots) > 0:
        price_trends = data.market_snapshots.select(
            ["turn", "commodity_id", "avg_price"]
        )
        fig_prices = px.line(
            price_trends.to_pandas(),
            x="turn",
            y="avg_price",
            color="commodity_id",
            title="Commodity Prices Over Time",
            labels={"avg_price": "Average Price ($)", "turn": "Turn"},
        )
        fig_prices
    else:
        "No market snapshot data available"
    return


@app.cell
def _(data, pl, px):
    if data is None:
        "No data loaded"
    elif len(data.market_transactions) > 0:
        volume_by_commodity = (
            data.market_transactions.group_by("commodity_id")
            .agg(pl.col("quantity").sum().alias("total_volume"))
            .sort("total_volume", descending=True)
        )

        fig_volume = px.bar(
            volume_by_commodity.to_pandas(),
            x="commodity_id",
            y="total_volume",
            title="Total Transaction Volume by Commodity",
            labels={"total_volume": "Total Quantity", "commodity_id": "Commodity"},
        )
        fig_volume
    else:
        "No transaction data available"
    return


@app.cell
def _(mo):
    mo.md("""
    ## Economic Health Metrics
    """)
    return


@app.cell
def _(data, pl, px):
    if data is None:
        "No data loaded"
    elif len(data.actor_drives) > 0:
        drive_health = data.actor_drives.select(["turn", "drive_name", "health"])
        avg_health = drive_health.group_by(["turn", "drive_name"]).agg(
            pl.col("health").mean().alias("avg_health")
        )
        fig_drives = px.line(
            avg_health.to_pandas(),
            x="turn",
            y="avg_health",
            color="drive_name",
            title="Average Drive Health Over Time",
            labels={
                "avg_health": "Average Health",
                "turn": "Turn",
                "drive_name": "Drive",
            },
        )
        fig_drives
    else:
        "No drive data available"
    return


@app.cell
def _(data, pl, px):
    if data is None:
        "No data loaded"
    elif len(data.actor_drives) > 0:
        drive_debt = data.actor_drives.select(["turn", "drive_name", "debt"])
        avg_debt = drive_debt.group_by(["turn", "drive_name"]).agg(
            pl.col("debt").mean().alias("avg_debt")
        )
        fig_debt = px.line(
            avg_debt.to_pandas(),
            x="turn",
            y="avg_debt",
            color="drive_name",
            title="Average Drive Debt Over Time",
            labels={"avg_debt": "Average Debt", "turn": "Turn", "drive_name": "Drive"},
        )
        fig_debt
    else:
        "No drive data available"
    return


if __name__ == "__main__":
    app.run()
