# Simulation Architecture and Development Guide

Internal architecture, turn order, and how to test and debug simulation
behavior.

## Turn execution order

1. Actors take turns in random order. `actor.take_turn()` runs one economic
   action (production or work), market actions (buy and sell orders), and
   drive processing (consumption). With `--workers N` this phase runs on a
   thread pool; see `docs/performance.md`.
2. Ships take turns in random order. `ship.take_turn()` advances any journey,
   then calls `brain.decide_trade_actions()` and `brain.decide_travel()`.
3. Every market matches its orders. All buy and sell orders match at once
   with price-time priority, so results do not depend on actor order.

Consequences:

- Orders are not immediate. A buy placed this turn fills at end of turn, so
  cargo and money arrive next turn.
- All ships see the same market state when they decide.
- Prices can move between planning and execution.

## Testing simulation behavior

Start with the KPI summary and Tier-1 analysis scripts (see the
`sim-evaluation` skill):

```bash
# Compact KPI summary with PASS/WARN/FAIL verdict, on the free-threaded
# side venv with a threaded actor phase (setup in docs/performance.md)
.venv-ft/bin/python -m spacesim.cli.main run --workers 12 --turns 200 --no-export --quiet --summary

# Run an ad-hoc analysis script against the latest exported run
uv run spacesim dev analyze my_probe.py
```

For interactive human-facing exploration, the marimo dashboard is available:

```bash
uv run spacesim run --notebook   # exports data and opens notebooks/analysis_template.py
```

See `notebooks/README.md` for notebook patterns.

Run 50 or more turns before judging behavior. Price differentials appear
only after actors have produced and consumed, market makers have set price
bands, and supply and demand have diverged between planets.

## Core components

| Component | File | Description |
|-----------|------|-------------|
| Simulation | `core/simulation.py` | Main loop, setup, orchestration |
| Actor | `core/actor.py` | Economic agents with inventory, money, skills |
| Market | `core/market.py` | Order matching, price discovery |
| Ship | `core/ship.py` | Interplanetary trade vessels |
| Planet | `core/planet.py` | Locations with markets and actors |
| Process | `core/process.py` | Production recipes |
| Commodity | `core/commodity.py` | Tradeable goods definitions |

## Brains

Actors and ships delegate decisions to pluggable brain classes.

Actor brains (`core/brains/`):

- `ColonistBrain`: generalist, meets basic needs first.
- `IndustrialistBrain`: specializes in one production recipe.
- `MarketMakerBrain`: provides liquidity using statistical pricing.

Ship brains (`core/ship.py`):

- `TraderBrain`: plan-driven interplanetary arbitrage. See
  `docs/dev-guide-ships.md`.

## Market mechanics

A buy order reserves money; a sell order reserves the commodity. Both wait
for the end-of-turn match.

### Price discovery

```python
# Get current bid/ask spread
bid, ask = market.get_bid_ask_spread(commodity)

# bid = highest buy order (what buyers will pay)
# ask = lowest sell order (what sellers want)

# Historical average (last 10 trades)
avg = market.get_avg_price(commodity)

# 30-day moving average
avg_30 = market.get_30_day_average_price(commodity)
```

`get_avg_price` returns the last recorded price when there are no recent
trades, and 10 when the commodity has never traded.

### Matching

For each commodity, buy orders sort highest price first then oldest first;
sell orders sort lowest price first then oldest first. Orders match while
the buy price is at or above the sell price. The transaction price is the
seller's ask.
