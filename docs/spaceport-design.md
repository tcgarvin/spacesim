# Spaceport Operators: a Service Actor Design

Status: phase 1 landed 2026-09-04 (commits 2678924..96330d2); see the
decision log entry of the same date for what was measured and what changed
after landing. Phases 2+ and the open questions below are still open.

## Why

Two probes on 2026-09-04 (see `decision-log.md` and the ship-stranding
memory) found one coordination failure seen from two sides:

- Industrialists hold ~22k units of fuel by turn 450, 99% listed at an honest
  replacement-cost floor of ~25, facing local bids of 3 or less. Only ships
  ever buy fuel, and they pay 21 to 63.
- Half the fleet sits stranded with money. Every stranded ship posts a rescue
  bid, but the bids are ~7 units at ~35, so a fuel run earns ~300 against
  ~11,000 for the best alternative plan and is never chosen.

Nobody on any planet stands ready to buy fuel at a price a producer accepts
and sell it at a price a ship pays. Ships already do this ship-to-ship
(848 units per run), so the spread exists. A spaceport operator is that
standing counterparty, gated by a facility, with an upkeep cost so it is not
free money.

## Taxonomy

Two kinds of actor, distinguished by what they are judged on:

| Kind | Judged on | Needs | Income | Brains |
|------|-----------|-------|--------|--------|
| Regular | own welfare, the drives | 4 drives | work, production, trade | colonist, industrialist |
| Service | availability of a capability | facility upkeep, once there is a facility | wage plus spread or fee | market maker, spaceport operator, later others |

A service actor exists to keep a capability available and is judged on that
availability, not on producing anything. Market makers already fit: their
capability is a two-sided market in every good. Spaceport operators are the
second service, and the first with a facility. Later services (repair,
warehousing, brokerage) follow the same pattern.

`ActorType` becomes `REGULAR` and `SERVICE`; `MARKET_MAKER` is absorbed.
Every current consumer of the type asks "population or infrastructure"
(summary drive and money stats, UI population counts, logging sampling),
so they all become one check. Role-level analysis keys on the brain class
name. The type says nothing about breadth: a market maker quotes every
good, an operator quotes one, and that is a brain-level fact.

The type is redundant with the brain class. A test asserts they agree so
they cannot drift; the enum stays because the consumers above want a cheap
check.

Intended direction, not phase 1: market makers eventually own an
"exchange" facility with upkeep, giving them the cost base they lack today.
The upkeep drive designed here is the general service need, so that step
adds data, not mechanism.

### Shared logic: a function library, not a base class

The two service brains share several algorithms: government work as the
economic action, ingesting own fills through a transaction-history cursor,
inventory skew around a midpoint, a stock target from 30-day flow, and a
randomized per-actor spread. These move out of `MarketMakerBrain` into a
module of plain functions (working name `core/brains/dealer.py`) that both
brains call. Each brain stays a separate class holding only its own state
and its own pricing rule: discovery and ladder for the market maker,
cost basis and facility gate for the operator. No shared base class with
inherited algorithms; the project prefers functions over stateful classes
and this keeps each brain readable on its own.

## Consistency with market makers

Market makers were given no drives (`_setup_planet_actors` builds a drive
list and then passes `drives=[]`, since the commit that introduced drives)
and always do government work. That was never recorded as a decision; it is
the de facto rule that "infrastructure actors do not eat." The proposal
keeps that rule and adds exactly one drive-shaped need, the facility itself:

- No food, clothing, shelter, or health drives. Same as market makers.
- One `FacilityUpkeepDrive` with the standard four metrics (health, debt,
  buffer, urgency), firing random events at a `BASE_EVENT_PROB`-style rate,
  exactly like `ClothingDrive`. Its `health` is the spaceport's condition.
- Each event rolls a *failure type* from a weighted table declared on the
  facility's yaml entry; the type names the material and quantity needed.
  An event the operator cannot cover goes unmet and condition drops, as a
  missed shelter event does. See "Upkeep failure table" below.
- The operator buys upkeep materials through the existing drive-bid path
  (`_drive_buy_commands`), targeting the last unmet material plus a small
  buffer of the common ones, so it competes at a drive-backed willingness
  to pay, not a new pricing rule.
- Consequence of neglect: service capacity scales with condition, and below
  a floor the operator withdraws its asks. Neither ships nor other actors
  are touched; the operator simply stops being a fuel source, which the
  navigator already handles as "no live ask."

Capital: none injected. Operators start with the standard 50 credits and
bootstrap on the government wage (10 per turn), exactly as market makers
do. Stock builds slowly at first and compounds from ship sales; early ships
keep buying from industrialist asks as they do today. Bid commitment is
capped at a fraction of cash, as `BUY_CAPITAL_FRACTION` does for market
makers, so an operator on a desert cannot lock its whole purse into a bid
nobody fills.

Solvency: condition decay is the only exit. No bankruptcy, no replacement,
matching market makers.

## Facility

Facilities are non-transportable commodities held in inventory, built by a
process, and checked by `facilities_required`. Levels are separate
commodities joined by upgrade processes, so no schema changes:

| Facility | Enables | Built from |
|----------|---------|------------|
| `spaceport` (level 1, ground) | fuel stocking and sale | pre-built at setup |
| `spaceport_l2` (deferred) | repair efficiency, maintenance goods | `spaceport` + building materials + tools |
| orbital / elevator | future | future |

Level 1 is pre-built at setup rather than constructed, because ships need
fuel from turn 1 and building materials take ~50 turns to exist. Upgrades
are built through the industrialist facility path, which already exists
(`_get_build_process_for_facility`, `_buy_command`).

Level 1 has no capacity limit; an operator offers all stock above its
reserve. Condition scales the offered quantity and withdraws asks below a
floor. Level 2 is deferred entirely; when it comes it is about repair
efficiency, not fuel throughput. Ships still buy from the book.

Per-facility data (upkeep failure table, later capacity or efficiency)
lives in a new `data/facilities.yaml` keyed by facility commodity id. No
such file exists today: the seven current facilities are commodities plus
build processes only.

## Upkeep failure table

What breaks is data, not code: each facility level lists failure types with
weights, a material, and a quantity. Weights favor the bootstrap tier so a
level 1 port is almost always repairable from the local economy, and the
rare exotic event is what makes a backwater operator import. The exotic
rows also create small standing demand for tier 2 and 3 goods on every
planet, which is the "tier 3 empty" gap from the 2026-09-04 survey.

| Failure type | Material | Level 1 weight | Level 2 (deferred) | Level 3 (future) |
|---|---|---|---|---|
| structural (pads, walls) | `simple_building_materials` | 0.60 | 0.45 | 0.30 |
| structural, upgraded | `advanced_building_materials` | - | - | 0.15 |
| mechanical (pumps, gantries) | `common_metal` | 0.25 | 0.20 | 0.15 |
| tooling wear | `simple_tools` / `precision_tools` | 0.10 | 0.10 | 0.10 |
| tank lining | `chemicals` / `polymers` | 0.05 | 0.10 | 0.10 |
| optics, sensors | `glass` | - | 0.05 | 0.05 |
| docking hardware | `ship_supplies` / `ship_parts` / `ship_components` | - | 0.10 | 0.10 |
| control systems | `electronics` / `computers` | - | - | 0.05 |

Quantities are 1 unit except structural (3, matching the shelter drive's
target). Exact weights are a first guess to tune from the summary; the
shape (cheap-heavy, one or two exotics per level) is the design.

## Brain: `SpaceportOperatorBrain`

Economic action, in priority order:

1. No spaceport: acquire and build (phase 3 only; level 1 is pre-built).
2. Upgrade when affordable and demand justifies it (phase 3).
3. Otherwise government work, as market makers do.

Market actions each turn, cancel-and-repost like every other brain:

**Fuel bid.** A standing ladder up to the stock target, priced so producers
actually hit it:

- On a planet that can refine fuel: `ceil(imputed unit cost)` from
  `_imputed_unit_cost`, the same number industrialists floor their asks at,
  so their listed stock clears. No new parameter.
- On a fuel desert: the delivery-viable price a stranded ship would bid,
  computed by the existing `_fuel_bid_price` logic (cheapest galaxy ask plus
  round-trip burn amortized over the bid quantity plus `FUEL_BID_MARGIN`).
  Move that function to the navigator so both callers share it.
- Inventory skew from the shared library: bid falls as stock approaches
  target, never below 1.

This turns the demand side from 7-unit rescue bids into standing bids of
hundreds of units at a delivery-viable price, which is what the ship planner
needs to prefer a fuel run.

**Fuel ask.** Cost basis (volume-weighted purchase price, tracked through
the shared fill-ingestion function)
times `1 + spread`, with `spread` drawn per operator from the same range as
`MarketMakerBrain.spread_percentage`. Skewed down toward cost when over
target, never below cost basis, which is the sell-at-or-above-cost rule
producers already follow. Quantity capped by service capacity times
condition. Two operators per planet with different spreads gives real price
competition.

**Stock target.** The shared 30-days-of-flow rule, floored at one ship tank
(`fuel_capacity_for`) so a quiet spaceport can still fill one ship.

**Maintenance goods (level 2).** Same bid/ask pattern on `ship_supplies`
and `ship_parts`, stock target one repair's worth times a small multiple.
Ships already buy these from the book in `_buy_maintenance_supplies`.

## What ships need

Almost nothing. `fuel_purchasable_at` is "a live ask exists" and
`fuel_ask_depth_at` reads the ask side, so a stocked spaceport is already a
refuel point and an escape route. Two ship-side items are prerequisites for
fuel to reach deserts by ship rather than only by local refining:

1. The tank-overflow accounting bug: `_sellable_quantity` counts fuel as
   cargo only above `fuel_capacity`, so a fuel plan of q units loads only
   when the tank holds capacity + q. Fix by tracking plan cargo separately
   from tank reserve.
2. `candidate_destinations` ranks demand by max(best bid, 30-day average),
   which lets stale averages outrank live spaceport bids. Rank by best
   resting bid when volume is zero.

The margin-denominator question (round-trip fuel charged against a one-way
plan) stays open and is separate.

## Hyperparameters added

Kept to the ones that name a physical fact about a spaceport:

| Parameter | Role | Reuses |
|-----------|------|--------|
| service capacity per level | units per turn | new |
| upkeep event probability | drive rate | `BASE_EVENT_PROB` pattern |
| operators per planet (default 2) | setup | CLI flag like `--makers` |

Everything else reuses existing constants: market-maker spread range,
`FUEL_BID_MARGIN`, `SHIP_CAPITAL_FUEL_PRICE_REFERENCE`, imputed cost,
inventory skew, 30-day stock target. Build and upgrade recipes are data in
`processes.yaml`.

## Expected effects and how to check them

Predictions, testable with the existing probes in the session scratchpad
and `--summary`:

- Planets with a resting fuel ask: ~23 of 100 today, should approach the
  number of planets with any fuel production within ~50 turns, then spread
  to deserts as hauling starts.
- Stranded ships: 53 of 100 at t450 today. Ships on producing planets should
  free within one turn of an operator stocking; deserts depend on the ship
  prerequisites above.
- Industrialist fuel hoard: 22k and growing ~107/turn today. Should stop
  growing on operator planets as bids at the floor absorb listed stock; the
  recipe-valuation defect (`_output_unit_value` case 3) is a separate fix
  and is still recommended.
- Money: operators inject capital at setup like market makers; watch the
  money KPI and note the new injection in the summary.
- Risk: operators on hoard planets buy at 25 and sell at ~30 with little
  ship traffic, so they may run at a loss on upkeep. That is the intended
  "interesting" pressure; the exit is condition decay, not bankruptcy.

## Phases

1. **Fuel dealer.** `ActorType` → `REGULAR`/`SERVICE` with the market
   maker absorbed, shared dealer functions extracted from the market maker
   (no behavior change, covered by existing tests), `spaceport` facility
   pre-built,
   `SpaceportOperatorBrain` with fuel bid/ask/stock target, 2 per planet,
   capital sizing, summary and UI counts, unit tests for pricing and stock
   rules, smoke assertion on fuel-ask planet count. No upkeep yet.
2. **Upkeep.** `FacilityUpkeepDrive`, condition scaling, ask withdrawal.
3. **Level 2 and ship prerequisites.** Upgrade recipe, maintenance-goods
   stocking, the tank-overflow fix and demand ranking fix so hauling to
   deserts works.
4. **Future.** Gate refuelling to spaceports with capacity limits, orbital
   tier, other service actors on the same base class.

## Open questions

Settled 2026-09-04: upkeep drive only; level 1 pre-built; no capital
injection, bootstrap on the wage; condition decay is the only exit; level 2
deferred; one `SERVICE` type absorbing market makers, one brain per
service, shared logic as functions. Still open:

1. Whether to eventually restrict ship refuelling to spaceports. Not needed
   for the fuel problem; deferred.
2. Ship-side prerequisites (tank overflow, demand ranking): measure phase 1
   first, then decide.
3. Market-maker exchange facility and upkeep: intended direction, no date.
