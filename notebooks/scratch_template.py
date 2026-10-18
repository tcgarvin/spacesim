"""Tier-1 analysis scratch template.

Copy this, edit the analysis, and run it:

    uv run spacesim2 dev analyze notebooks/my_question.py

Contract, which keeps agent token cost bounded:
  * Print small aggregates only: .describe(), grouped means, .head(N).
    Never print a full DataFrame.
  * Save figures to tmp/ with savefig(); never display them. The agent
    reads the printed numbers; the human opens the saved PNGs.
  * This is a plain script: `print()` is the output channel.

For an interactive human-facing dashboard, build a marimo notebook instead
and open it with `marimo edit`.
"""

import polars as pl

from spacesim2.analysis.loading import load_run

r = load_run()  # most recent run, or $SPACESIM_RUN_PATH
print(f"run: {r.simulation_id}")

# Example: mean price per commodity over the whole run.
prices = (
    r.market_snapshots.group_by("commodity_id")
    .agg(pl.col("avg_price").mean().round(2).alias("mean_price"))
    .sort("commodity_id")
)
print(prices)

# Example: a figure for the human, saved rather than shown.
# import matplotlib
# matplotlib.use("Agg")  # headless backend, no window
# import matplotlib.pyplot as plt
# food = r.market_snapshots.filter(pl.col("commodity_id") == "food")
# fig, ax = plt.subplots()
# ax.plot(food["turn"], food["avg_price"])
# ax.set(title="food price", xlabel="turn", ylabel="avg price")
# fig.savefig("tmp/food_price.png")  # dev analyze will report this path
