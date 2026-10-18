# Migration: Actors Moving Between Planets

Status: v2 landed 2026-09-11. An actor that its planet serves badly posts a
passage contract offering a fare, a ship accepts it and carries the actor,
and the migrant draws a new land where it lands. Nobody teleports: the
fleet's capacity bounds how many people move.

## Why

Poor planets do not import (decision log, 2026-09-08): a wood-poor or
barren planet self-feeds at most of its labor and ships rarely call. Trade
was the only equalizer and it does not reach those planets. Migration is
the other one: people leave.

## The split

Every reason to move lives in the brain. What a move does lives in core.

| Layer | File | Owns |
|-------|------|------|
| Brain | `core/brains/migration.py` | pressure, propensity, intent with hysteresis, destination scoring, liquidation |
| Brain | `core/brains/colonist.py`, `core/brains/industrialist.py` | `decide_migration`, `on_relocated`, investment and sell-sweep guards while leaving |
| Core | `core/migration.py` | `PlanetStats`, the migration phase, the fare estimate, `relocate_actor` |
| Core | `core/contracts.py` | the passage contract itself: board, money, boarding, delivery |
| Core | `core/planet.py` | `remove_actor`, `release_land`, `add_actor_with_land` |

`ActorBrain.decide_migration(actor)` returns a `MigrationRequest`
(destination, fare offered now) or `NO_MIGRATION`. The default stays put, so
a brain that never thinks about moving needs no code.
`ActorBrain.on_relocated` is the hook for resetting planet-specific brain
state after a move.

## Turn order

1. `refresh_planet_stats` at the top of `run_turn`: one `PlanetStats` per
   planet (population, free land count and per-resource pool mean, median
   need debt, median prosperity, median money) over regular residents.
   Cost is 0.4% of a 12-planet turn.
2. Actor phase. `Actor.take_turn` asks land-claiming actors for a decision
   and stores it on `actor.migration_request`. Nothing moves here: the
   threaded phase shards by planet.
3. Ship phase. A ship brain accepts an open passage contract; departing,
   `Ship.start_journey` loads the passenger, which cancels the actor's
   orders, claims a land at the destination, clears the inventory, removes
   the actor from the origin and from `sim.actors`, and pays the fare to
   the carrier. `Ship.update_journey` delivers on arrival.
4. `run_migration_phase`: every standing request with no live contract
   posts one; a raised offer or a re-aimed destination replaces the open
   contract; `NO_MIGRATION` cancels it. Nobody moves in this phase.
5. Market matching.

A passenger aboard a ship is in no actor list and does not act, eat, or
trade. `actor.in_transit` is the authoritative flag; `actor.planet` keeps
pointing at the origin until arrival so nothing has to handle a planet-less
actor. The threaded phase's count invariant still holds.

Contract boards expire stale offers at the top of the turn, before any
brain reads one.

## The brain's decision

Checked once every `MIGRATION_CHECK_INTERVAL` (10) turns per actor, on an
offset derived from the actor's name, so scoring cost is one tenth of an
actor-turn.

**Pressure**, the push, in [0, 1]: the worst need-drive `debt`, plus 0.2
times the prosperity shortfall, plus 0.3 times the largest gap between the
planet mean and the actor's own land. Debt already integrates neglect over
time, which is why there is no separate "sustained hardship" timer.

**Gain**, the pull. A destination scores
`land + wellbeing + 0.2 * prosperity - 0.5 * fare / money`, where land is
the free pool's mean coefficient (what a newcomer will draw), wellbeing is
one minus the residents' median need debt, and the fare is what the actor
would pay. Staying scores the same way with the actor's own land and no
fare. An intent forms only if some destination beats staying by
`MIN_MIGRATION_GAIN` (0.1). This is the load-bearing rule: in the first
hundred turns of a run every actor everywhere has a need debt of 1.0 while
the economy bootstraps, and pressure alone moved most of the population
between planets that were all equally bad.

**Roll.** With pressure at or above `ENTER_THRESHOLD` (0.35), the actor
forms an intent with probability `BASE_LEAVE_RATE * propensity * pressure`
per check. Propensity is a fixed per-brain draw in [0.2, 1.0]. The base
rate 0.094 gives a chronically deprived actor (pressure 0.8, median
propensity) a 50% chance of forming an intent within 150 turns.

**Intent.** Held for `MIN_INTENT_TURNS` (30) before any request. During the
intent the brain stops buying tools, building facilities, and adopting
recipes, and its sell sweep offers everything it holds at keep level 0,
facilities included, except need-drive materials. The waiting period is
where the actor turns stock into fare money. Every later check re-aims the
destination without the fare term, since the actor has already decided to
pay; if nothing clears the gain floor any more, or pressure falls under
`EXIT_THRESHOLD` (0.2), the intent is dropped.

**Request.** Once the intent is old enough and the fare is affordable, the
brain returns the request every turn until a ship carries the actor away.

**The offer.** No ship is obliged to fly a passage, so the fare is bid, not
set. The first request offers `passage_fare(navigator, origin, destination)`,
the leg's fuel cost plus half; from that turn the
offer rises linearly to `FARE_HEADROOM` (1.5) times it over
`FARE_ESCALATION_TURNS` (20), capped there and capped at the actor's money.
Money the actor's own open contract is holding counts toward that budget,
since cancelling to re-price hands the reserve straight back.

**Expiry.** A passage nobody accepts within `PASSAGE_CONTRACT_TTL` (30)
turns expires and refunds. The brain reposts once, at the offer the
escalation has reached by then; after a second expiry it drops the intent
and the actor rejoins the local economy.

**Destination draw** is a softmax over the scores at temperature 0.1, not
an argmax. Pressure rises on many planets at once, and an argmax would send
every wave to one planet and fill its pool.

## Core mechanics

- The contract: `PassengerPayload(actor)`, `advance = fare_offer`,
  `on_delivery = 0`, expiring `PASSAGE_CONTRACT_TTL` turns after posting.
  A re-price posts a new contract carrying the original `posted_turn`, so
  the escalation does not reset the wait the summary reports.
  Posting reserves the advance the way a bid reserves money; cancelling or
  expiring returns it. The whole fare goes to the carrier at boarding, not
  on delivery, so a passenger can fund a broke ship's fuel. What makes the
  ship deliver is that a loaded contract pins its destination.
- `passage_fare(navigator, origin, destination)` is only the estimate the
  brain opens its offer at:
  `max(PASSAGE_FARE_MINIMUM, ceil(leg_fuel_cost * (1 + PASSAGE_FARE_MARGIN)))`
  with `PASSAGE_FARE_MARGIN` 0.5 and `PASSAGE_FARE_MINIMUM` 20.
  `leg_fuel_cost(navigator, origin, destination)` in `core/navigation.py` is
  the baseline burn at efficiency 1.0 times `Navigator.fuel_value_reference()`,
  or `FUEL_BID_FALLBACK_FLOOR` before anything has traded. Government freight
  is priced off the same leg cost at a 0.25 margin, so a passage pays a
  carrier better than the job it competes with. The escalation then takes the
  offer from fuel x 1.5 to fuel x 2.25.
- A passenger occupies `MIGRANT_CARGO_UNITS` (20) of hold, so it rides
  along with a trade rather than taking a whole trip, and never shares a
  hold with a government lot of 85 to 100 units. Which contracts a
  ship accepts is ship-brain logic; see `docs/contracts-design.md`.
- Finding a live contract: `actor.passage_contract` holds the last one
  posted and is never cleared. Its status says whether the passage is
  still running, so nothing has to be reset on cancel, expiry, or
  delivery, and the brain reads an expiry off the same field.
- Land: claimed at boarding, not at posting, so open contracts hold
  nothing at the destination and a queue of them cannot exhaust a pool
  nobody is travelling to. The origin land returns to the origin's pool.
  Leavers are selected for bad draws, so a high-emigration planet's free
  pool degrades, and that is intended.
- A boarding that finds the destination pool empty fails, and the contract
  goes back on the board as OPEN.
- Stranding: a ship docked somewhere that is not a loaded contract's
  destination for `CONTRACT_STRAND_PATIENCE` (10) turns puts the passenger
  down where it is, if that planet has a free land.
- Requests core ignores: no planet, or the destination is the origin.
- Kept across a move: money, skills, tastes, drives. Lost: inventory,
  facilities, land, orders.
- Pool size: `--lands-per-planet`, default 200 against 100 actors per
  planet. A bare `Planet` still defaults to 100.

## Measuring it

`--summary` has a `migration` block, not in the verdict:

| Key | Meaning |
|-----|---------|
| `departures` | passengers that have boarded a ship |
| `arrivals` | passengers put down at a destination |
| `aboard` | passengers in flight right now |
| `waiting` | open passage contracts nobody has accepted |
| `expired` | passage contracts that ran out unaccepted |
| `median_wait_turns` | median turns from the first ask to boarding |
| `departures_per_100_turns_per_1000_actors` | the rate, normalized |

`waiting` and `expired` large on poor planets is the failure to watch for:
it means the fleet is not calling where migration is most needed.

`notebooks/migration_probe.py` follows every migrant and a stayer from the
same origin matched on pressure at the departure turn, and compares need
debt, prosperity, and money 100 and 200 turns later. That is the test of
whether moving helps. Run it at 100 planets for a galaxy-scale answer.

## Open

- The offer opens at a flat distance rate and climbs on a fixed schedule.
  Ships price fuel by flow; the opening offer could too.
- A migrant with no facility or recipe re-selects on arrival; whether the
  destination's market has room for one more producer is not scored.
- Brain drain: skills travel with the actor, so a poor planet loses its
  skilled residents first. Unmeasured.
