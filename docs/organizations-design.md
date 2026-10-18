# Organizations: Trading Companies and Ship Building

Status: landing 2026-09-16. Ownership and remittance (`Ship.owner`,
`Ship.retained_capital`, `Ship.remitted_total`) are on `main`; the
`TradingCompanyBrain` service actor, the hull-buying and ship-spawning
logic, and their tests are landing alongside this document. Sections below
that describe that brain cite only the names given for it, not code read
from disk.

## Why

`TODO.md` names fleet capacity as the current limit on food imports: a
fixed ship count set at setup cannot grow to meet demand. Organizations
make fleet size emergent instead of a CLI knob (`--ships`): a trading
company buys hulls out of its own earnings when that is profitable, and
stops when it is not.

## Taxonomy

`docs/spaceport-design.md` splits actors into regular (judged on their own
drives) and service (judged on keeping a capability available). The trading
company is a third role under the service kind:

| Kind | Judged on | Needs | Income | Brains |
|------|-----------|-------|--------|--------|
| Regular | own welfare, the drives | 4 drives | work, production, trade | colonist, industrialist |
| Service | availability of a capability | facility upkeep, once there is a facility | wage plus spread or fee | market maker, spaceport operator |
| Service (fleet owner) | fleet size and ship solvency | none | ship remittances | trading company |

A trading company is a pure fleet owner: it does not trade cargo, work a
job, or hold drives. Its only decision is whether to buy a hull.

## Ownership and remittance

Landed on `main`. Every `Ship` has an `owner` satisfying the `ShipOwner`
protocol (`core/ship.py`): a `name`, a `money` balance, and
`receive_remittance(ship, amount)`. The default is `INDEPENDENT`, the
single `IndependentOwner` instance; an independent ship keeps everything it
earns.

`Ship.retained_capital` is the ship's own working capital, kept back from
remittance. Unset, it defaults to `retained_capital_for(initial_money)` =
`SHIP_RETAINED_CAPITAL_MULTIPLE` (3) times the ship's starting purse — 3x
starting capital.

`Ship.remit_surplus()` runs at the end of `take_turn` and pays out only
when the ship is docked, not distressed, and owned by something other than
`INDEPENDENT`. The amount is `money - reserved_money - retained_capital`;
reserved money backs resting bids and is never remitted. `Ship.remitted_total`
accumulates what a ship has paid its owner.

The ship phase runs serially on the main thread — only the actor phase is
sharded across worker threads (`docs/performance.md`) — so a trading
company on one planet crediting a ship docked on another needs no
synchronization; see `docs/dev-guide-ships.md`, "Ownership and remittance".

## The company's valuation rule

Landing 2026-09-16, not yet on disk; the rule as specified: a trading
company values a hull purchase against an EMA of remittance per ship per
turn, multiplied by a horizon in turns (`SHIP_VALUE_HORIZON_TURNS`) and
discounted for ramp-up (`SHIP_RAMP_DISCOUNT`) — a new ship earns nothing
until it reaches its first market. A single hull bid is capped at the
company's cash on hand minus its own endowment, so a bad quarter cannot
spend the company into distress the way an overextended ship can.

## The hull chain

Verified against `data/processes.yaml` and `data/commodities.yaml`:

1. `make_ship_hull_section` (facility `advanced_factory`): 3
   `advanced_building_materials`, 4 `common_metal`, 2 `precision_parts`, 1
   `electronics` -> 1 `ship_hull_section`.
2. `make_ship_components` (facility `advanced_factory`): 2
   `precision_parts`, 1 `electronics`, 1 `polymers` -> 1 `ship_components`.
3. `build_shipyard`: 10 `simple_building_materials`, 8
   `advanced_building_materials`, 2 `heavy_machinery` -> 1 `shipyard`
   facility.
4. `make_ship_hull` (facility `shipyard`): 5 `ship_hull_section`, 2
   `ship_components`, 1 `computers`, 2 `heavy_machinery` -> 1 `ship_hull`.

`ship_hull` (`data/commodities.yaml`) is `transportable: false` and
`tradable: true`: too large to carry as cargo, but it trades on the market
of the planet that built it. At 100-planet prices the chain runs about
5.5k credits per hull (five hull sections, two component sets, one
computer, two heavy machinery, plus the labor and facility upkeep in
`make_ship_hull` itself).

## The spawn queue

Landing 2026-09-16, not yet on disk. A trading company that wins a
`ship_hull` bid does not build a `Ship` object mid-turn; hulls are queued
and drained into new `Ship` instances between the actor phase and the ship
phase, keeping the same serial, thread-safe boundary the ship phase
already relies on for remittance.

## KPIs

`spacesim/analysis/summary.py`, `_summarize_fleet`: `fleet.owned_ships`,
`fleet.independent_ships`, `fleet.ships_built` (ships named with the
`" Trading Co-"` substring — a proxy, since there is no setup-vs-built
counter on `Simulation`), `fleet.remitted_total`, `fleet.hull_bids_open`
(open buy orders for `ship_hull` across every planet market),
`fleet.hulls_built` and `fleet.shipyards` (current population-wide
`ship_hull` and `shipyard` inventory — stocks, not cumulative counts, in
the absence of a production counter), and `fleet.company_money_median`
(median money of actors whose brain class is `TradingCompanyBrain`, 0.0
when none exist).

`spacesim/analysis/export/schema.py`, `SHIP_TURNS_SCHEMA`: `owner_name`,
`retained_capital`, `remitted_total` columns on the per-ship, per-turn
export.

## Measured baseline

From today's probe: median ship remittance 81/turn, p75 119; 91 of 100
ships hold more than 10x the 1000-credit retained-capital floor by turn
450. Payback time from launch: about 215 turns on a 5k hull, about 430
turns on a 20k hull.

## Negative feedback: two ships per planet

A past A/B run that forced 2 ships per planet cut median ship money from
about 1650 to about 470: more ships split the same trade volume, so each
ship earns less and remits less. A trading company's own valuation rule
must see this feedback — a glutted fleet's falling EMA remittance per ship
is what should stop it from buying another hull — or fleet size will
overshoot the trade volume that can support it.

## Open

- Ship deletion or scrapping: nothing removes a ship from `sim.ships`, so a
  company has no way to shrink a fleet that overshot.
- Companies on planets without a `shipyard` facility: `make_ship_hull`
  requires one, so a company on a planet that never builds a shipyard has
  no source of hulls to bid on.
- The endowment and starting tank for a newly spawned ship are not yet
  decided.
- Tuning `SHIP_VALUE_HORIZON_TURNS` and `SHIP_RAMP_DISCOUNT` against
  measured payback times.
- A 100-planet A/B of organizations against the fixed fleet size has not
  been run.
