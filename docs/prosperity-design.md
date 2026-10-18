# Prosperity Drives: Tier 2 and 3 Consumption

Design for demand above subsistence. Status: phases 1, 2, and 4 implemented
(`core/drives/prosperity_drive.py`, summary `prosperity` block, surplus
money discount in `ActorBrain._surplus_money_discount`); phase 3 (UI) open.

## Why

Every finished good above tier 1 has no buyer. Need drives consume a quality
good only when it happens to be in stock, and the buy loop in
`ActorBrain._drive_buy_commands` picks the cheapest material, so nobody bids
for processed food, quality clothing, prefab housing, or advanced medicine.
Luxury goods and computers have no consumer at all. Producers cannot enter a
chain nobody pulls on, which is half of the "tier 3 empty" finding in
`TODO.md`.

The fix is a second family of drives that a rich actor spends surplus on,
and a prosperity index that makes rich and poor planets visible.

## Prosperity drives

One class, `ProsperityDrive`, parameterized by a small config table. Four
instances per regular actor:

| Category | Good | Base event rate | Base target units |
|----------|------|-----------------|-------------------|
| food | `food` | 1/60 | 2 |
| clothing | `quality_clothing` | 1/60 | 2 |
| health | `advanced_medicine` | 1/90 | 1 |
| luxury | `luxury_goods` | 1/45 | 2 |

The index is the unweighted mean over these four.

Shelter and computing left the table because their goods became durables.
A prosperity drive eats one unit per event at a hand-set rate, which is the
wrong model for a good you own: `prefab_housing` is now a dwelling
`ShelterDrive` holds, serving every shelter event and wearing out on a 0.1
roll (10 events, about 1200 turns), and `computers` are productive capital
an industrialist holds for a 25% output bonus on any facility-gated recipe,
breaking at 0.005 per run (200 runs). Demand for both now comes from the
return on holding the good rather than from an event rate. Coverage for
these two sat at 0.02 and 0.00 at 12 planets / 300 turns. They are measured
in the summary `durables` block instead of the prosperity index; see
`docs/commodities.md` ("Capital") and `docs/needs.md` ("Durable materials").

The food category is the exception to the pattern below. `processed_food`
is the staple `FoodDrive` eats, made by `process_food`: 40 biomass + 1
chemicals -> 60 processed_food at a chemical plant. Hand-cooked `food` (4
biomass -> 4 food) is the premium good, so the food category's prosperity
good is `food` itself and `FoodDrive.materials()` returns both.

Each other prosperity drive owns its good outright. The need drives return
the basic good only from `materials()`: `ClothingDrive` and `HealthDrive` do
not bid for the quality good, though they still consume it as a last resort
when the basic good is out of stock. That fallback is the only remaining
coupling between the two families. `ShelterDrive` is no longer one of them:
it owns `prefab_housing` outright as a durable.

Mechanics follow `ClothingDrive`: a Bernoulli consumption event per turn,
debt that grows on a missed event and decays on a served one, a
log-normalized buffer, and `MISS_PENALTY` set to 0.1.
`ActorBrain._drives_by_priority` ranks prosperity drives behind every need
regardless of welfare, so needs draw from the budget first.

### Gate

A prosperity drive places no buy orders unless the actor's basic needs are
met: every need drive has `debt < 0.25` and `buffer >= 0.2`. The buffer is
log-normalized against each drive's own target, so 0.2 is three units of
food (half the 6-unit pantry target) and any one unit of clothing, shelter,
or medicine. A homeless actor does not shop for processed food. The gate controls purchasing only.
Consumption events still fire and use stock on hand, and metrics still
update, so losing the gate shows up as falling coverage rather than a frozen
number.

Exposed as `ActorDrive.can_purchase(actor) -> bool`, default `True`, checked
in `_drive_buy_commands` before the bid is priced.

### Taste

Each actor gets a fixed taste vector at creation: weight 1.0 on every
category and 3.0 on one favorite drawn uniformly. Taste scales two things in
the actor's prosperity drive for that category:

- Event rate: `p_event = base_rate * taste`. The favorite good is used up
  faster.
- Target units: `target = round(base_target * taste)`. The favorite good is
  stocked deeper.

Taste does not scale the miss penalty or the welfare of a unit directly. A
faster rate lowers the buffer for the same stock, which raises
`marginal_welfare()`, which raises willingness to pay. The preference for the
favorite good comes out of the buffer arithmetic that already exists.

Taste never touches need drives. Hunger is the same for everyone; taste only
changes what an actor does with surplus.

Stored as `Actor.tastes` keyed by category, rolled by `random_tastes()` in
`Simulation._setup_planet_actors` next to `_random_initial_skills`.

### Pricing

Prosperity bids go through the existing two-layer path: willingness to pay
is `marginal_welfare / value_of_money`, capped by replacement cost when the
actor could make the good, and the posted bid escalates from the reference
price under scarcity pressure. Nothing new. The absent-material bid in
`docs/needs.md` never fires for a prosperity drive: each lists one material,
so there is no second material to be missing from the local book. The value of money is floored at
10% of the hungry value, so a rich actor's ceiling is about ten times a
hungry actor's. Since the prosperity stake (0.1) is half the food stake
(0.2), that put every prosperity ceiling near four times the food price,
about 43 credits, while prefab housing costs 66-84 to make, luxury goods
76-92, advanced medicine 157, and computers 265. The 2026-09-06 probe
(`notebooks/prosperity_blockers_probe.py`) found the welfare term binding
on 100% of prosperity bids with gated actors holding a median 920 credits.

Phase 4 fixes this with a surplus money discount, applied to prosperity
drives only. Cash beyond `SURPLUS_REFERENCE_DAYS` (30) of food is surplus;
lambda is scaled by `reference_days / affordable_days`, floored at
`SURPLUS_DISCOUNT_FLOOR` (0.1). An actor holding 90 days of food money gets
a ceiling three times higher; the floor caps the gain at ten times. Need
drives keep the undiscounted lambda, so a rich actor still bids for food
and clothing exactly as before. The posted bid is still bounded by the
reference price under scarcity pressure (at most four times the average or
imputed price), so the discount raises the ceiling, not the opening bid.

## Prosperity index

Per actor, in [0, 1]: the unweighted mean over the six prosperity drives of
an exponential moving average of "event served" (1 when a consumption event
found stock, 0 when it missed), with a half-life near 60 turns. Unweighted
so the index means the same thing on every planet and a lucky taste draw
does not inflate it. Money is not in the index. The money supply grows
without bound through the government wage, so any cash term would light the
whole galaxy by turn 600 regardless of consumption.

Per planet: mean and standard deviation across regular actors. Prosperity
tiers for the map are thresholds on the mean:

| Tier | Mean prosperity |
|------|-----------------|
| 0 | < 0.15 |
| 1 | 0.15 to 0.4 |
| 2 | 0.4 to 0.7 |
| 3 | >= 0.7 |

Wellbeing (`view_model.planet_wellbeing`) and the summary verdict stay on
need drives only. A poor planet is not suffering because it lacks computers.

## UI

The renderer encodes one axis today: distress as a red glow that pulses
toward famine and is off for a healthy planet. Prosperity is a second axis at
the other end, drawn as discrete sprite overlays so it reads at fit zoom on
100 planets:

- Tier 1: scattered city lights on the sprite.
- Tier 2: a faint orbital ring.
- Tier 3: a bright ring with a station sprite.

Red glow and a ring rarely coincide because the gate stops purchases when
needs fail.

Panel additions in `PlanetDetail`: a prosperity row beside wellbeing, mean
and spread, and one row per prosperity category with coverage. Charts strip:
a galaxy prosperity series next to wellbeing, and 30-turn trade volume split
into needs goods and prosperity goods. HUD: mean prosperity and planet count
per tier.

Frame contract: `PlanetSnapshot` gains `prosperity: float` and
`prosperity_tier: int`, computed in the same per-turn sweep as wellbeing.
The render thread reads nothing else new.

## Summary

New `prosperity` block in `compute_summary`: mean index over the four
categories, per-category coverage, share of actors passing the gate, and
volume for each prosperity good over the shared activity window
(`_ACTIVITY_WINDOW_TURNS`, 50 turns).

A `durables` block covers the two goods that left the index:
`computer_holder_share` (industrialists whose chosen recipe lists a capital
good that hold one), `prefab_holder_share` (regular actors with a dwelling),
and volume per planet-turn for `computers` and `prefab_housing`. Not part of
the verdict.
Open: a smoke assertion that at least one prosperity good
trades at nonzero volume by turn 200, once the first probe shows the level
to assert against.

## Phases

| Phase | Work | Question it answers |
|-------|------|---------------------|
| 1 | `ProsperityDrive`, gate, taste vector, need drives drop quality preference, summary block. Done. | Do tier 2 goods trade once someone bids for them? |
| 2 | Luxury and computing categories. Done, shipped with phase 1; computing later became capital. | Does demand alone pull tier 3 through the chain? |
| 3 | Snapshot fields, tier overlays, panel rows, charts | Can you see rich and poor planets at fit zoom? |
| 4 | Surplus money discount on prosperity lambda. Done. | Is the demand curve steep enough? |

Phase 1 is measurable in one 12-planet 200-turn run. Supply may still stall
on facility depth; the probe shows which side binds.

## Rejected

- Quality preference inside need drives. Lets a homeless actor buy processed
  food. Replaced by separate gated drives.
- Planet culture (skewed favorite draw per planet). Deferred until per-actor
  taste has been observed.
- Habituation (standard-of-living ratchet). Deferred; see phase 4.
- Continuous gold halo on the map. Unreadable at fit zoom.
