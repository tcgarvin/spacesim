# Actor Needs (Drives)

Actor needs are drives in `spacesim2/core/drives/`. Each drive inherits from
`ActorDrive` (`actor_drive.py`), is attached to one actor, and ticks once per
turn: it consumes satisfying goods from the actor's inventory and updates a
`DriveMetrics` record.

| Drive | Consumption | Basic good | Fallback good | Miss penalty |
|-------|-------------|------------|--------------|--------------|
| Food (`food_drive.py`) | deterministic, 1/turn | `processed_food` | `food` | 0.2 |
| Clothing (`clothing_drive.py`) | stochastic, p = 1/60 per turn | `clothing` | `quality_clothing` | 0.5 |
| Shelter (`shelter_drive.py`) | stochastic, p = 1/120 per turn | `simple_building_materials` | `prefab_housing` (durable) | 0.5 |
| Health (`health_drive.py`) | stochastic, p = 1/90 per turn | `medicine` | `advanced_medicine` | 0.4 |

## Metrics

Each `tick()` updates four values, all in [0, 1]:

- **health**: 1.0 if the drive's material is in stock (for food, whether the
  actor ate this turn), else 0.0.
- **debt**: accumulated neglect. A missed consumption event adds the drive's
  `DEBT_MISS_PENALTY`. A satisfied turn multiplies debt by
  `DEBT_DECAY_FACTOR` (0.8), or by `QUALITY_DEBT_DECAY_FACTOR` (0.5) when the
  quality good was consumed. Stochastic drives decay only on event turns or
  while stocked. `get_score()` is `1 - debt`.
- **buffer**: expected days of supply (stock / expected events per day)
  through `log_norm_ratio(days, target, cap)`, so returns diminish toward
  a per-drive target. Target/cap days: food 7/30, clothing 60/180,
  health 90/270, shelter 120/360.
- **urgency**: priority multiplier. Fixed at 1.0 for all drives.

## Fallback goods

Every need drive consumes its basic good first and the fallback good only
when the basic good is out of stock. Fallback consumption decays debt faster
(0.5 vs 0.8). Both goods count toward buffer coverage. For clothing and
health `materials()` returns the basic good only, so those needs never bid
for or stockpile the upgraded good; the upgraded goods are owned by the
prosperity drives below. Food and shelter are the exceptions: `FoodDrive`
bids for both the staple `processed_food` and the premium `food`, and
`ShelterDrive` bids for both `simple_building_materials` and the durable
`prefab_housing`.

## Durable materials (servings)

A durable covers many consumption events instead of one.
`ActorDrive.material_servings(commodity_id)` returns how many, 1.0 by
default. `ShelterDrive` returns `1 / PREFAB_WEAR_PROBABILITY` (10) for
`prefab_housing`: a held prefab serves every shelter event and is consumed
only on a 0.1 wear roll, about once in 1200 turns at the 1/120 event rate.
A shelter event consumes a building material only when no prefab is held.

`ActorBrain._drive_buy_commands` works in servings, not units: stock on hand
and the drive buffer count `quantity * servings`, `_cheapest_material_ask`
compares `ask / servings` (a 75-credit prefab that serves ten events beats a
15-credit brick), willingness to pay is the per-event value times servings,
the self-supply cap is compared per serving, and the quantity bought is
`ceil(need / servings)`, so an actor buys one prefab rather than ten. Every
other drive is at servings 1.0 and behaves exactly as before.

## Prosperity drives

`prosperity_drive.py` holds one class, `ProsperityDrive`, with one instance
per category on every regular actor. Design in `docs/prosperity-design.md`.

| Category | Good | Base event rate | Base target units |
|----------|------|-----------------|-------------------|
| food | `food` | 1/60 | 2 |
| clothing | `quality_clothing` | 1/60 | 2 |
| health | `advanced_medicine` | 1/90 | 1 |
| luxury | `luxury_goods` | 1/45 | 2 |

`prefab_housing` and `computers` are not prosperity categories. They are
durables: the prefab is a dwelling `ShelterDrive` holds, and computers are
productive capital an industrialist holds for an output bonus (see
`docs/commodities.md`, "Capital"). Both are reported in the summary
`durables` block instead of the prosperity index.

`processed_food` comes from `process_food`: 40 biomass + 1 chemicals -> 60
processed_food at a chemical plant. Hand-cooked `food` comes from
`make_food`: 4 biomass -> 4 food, no facility. Food is the one category
whose prosperity good is also a need material: `FoodDrive.materials()`
returns both goods and bids for whichever is cheaper.

- `WELLBEING = False`: excluded from planet wellbeing, the summary `drives`
  block, and the verdict. Reported in the summary `prosperity` block.
- Miss penalty 0.1, below every need, and `ActorBrain._drives_by_priority`
  ranks prosperity drives behind all needs regardless of welfare.
- Gate: `can_purchase()` is true only while every need drive has
  `debt < 0.25` and `buffer >= 0.2` (`needs_are_met`). Consumption from stock
  and metric updates continue while gated.
- Taste: `Actor.tastes` is fixed at creation, weight 1.0 per category and
  3.0 on one favorite. It multiplies the event rate and the target stock.
  Prosperity drives read it at construction; the actor also keeps it for
  later export and UI use, not yet consumed by either.
- `coverage` is an EMA of "event served" with a 60-turn half-life;
  `prosperity_index(actor)` is its unweighted mean across categories.

## Economic coupling (willingness to pay)

`ActorDrive` exposes the hooks brains use to price buy orders:

- `deprivation_stake()`: welfare value of one consumed unit. Equals the
  drive's `MISS_PENALTY`, so it is comparable across drives.
- `marginal_welfare()`: `deprivation_stake() * (1 - buffer)`. A well-stocked
  drive values an extra unit less.
- `materials()`: satisfying commodities; need drives return only the basic
  good, prosperity drives return their own upgraded good.
- `target_units()`: inventory level the actor keeps on hand
  (food 6, clothing 3, shelter 3, health 2), counted in servings.
- `can_purchase(actor)`: whether the brain may bid for this drive this
  turn. Always true for needs; prosperity drives gate on met needs.

### Substitute bids for a drive's other materials

`ActorBrain._drive_buy_commands` bids for the cheapest material that has a
local ask, so the drive's other materials draw no bid at all. Ships plan
against the destination order book, so a planet whose colonists hand-feed on
`food` at 7 shows no demand for `processed_food`, even though every one of
them would take it at 6. Where a local plant sells a few units at 20, the
book showed five bids at 18 and nothing under it, and no ship could sell a
full hold there. The staple surplus stayed on the plant planets at 1.

`ActorBrain._add_substitute_material_bids` posts one extra bid per other
material of the drive, whether or not someone sells it locally at a higher
price:

| Term | Value |
|------|-------|
| price | `min(wtp, ask * SUBSTITUTE_BID_DISCOUNT, reference * (1 + scarcity_pressure))` |
| quantity | the drive's restock `need`, capped by the money left |
| when | after every drive has placed its primary bid |

`wtp` is `_drive_willingness_to_pay` for the drive, unchanged. `ask` is the
cheapest local ask among the drive's materials and `SUBSTITUTE_BID_DISCOUNT`
is 0.9, so the buyer offers a little less for the untried good than for the
one on the shelf. `reference` is `_drive_bid_reference`, and unfilled bids
raise `scarcity_pressure` toward its cap of 3.0, so a never-traded good's
bid starts where the market can supply and ratchets up while nothing
arrives. With a shelf ask present the discounted ask is the binding term.

The pass runs last, after every drive's primary bid, and draws on the same
running budget. A substitute bid therefore never outbids a shelf purchase
for a lower-priority need, and the bids an actor places in a turn together
reserve no more than its money. Quantity is the full restock `need`, so an
actor with cash can end a turn holding up to twice its target; consumption
keeps it out of the market until stock falls back below target.

Only `FoodDrive` lists more than one material, so today only
`processed_food` is affected. Cost is one dict lookup per material:
`_cheapest_material_ask` has already filled the quote cache.

### The food gate only fires where hand food works

Both brains gate on a low pantry and answer it by gathering biomass and
cooking. `ProcessCommand.execute` floors every run at 1 unit of output, so
an actor whose land yields no biomass still brings home 1 unit a turn: four
gather turns and a cook turn make 4 food per 5 turns against 1 eaten per
turn. The pantry never reaches the gate's threshold, so the actor gathers
for the rest of its life and buys nothing.

`hand_food_viable` (`core/brains/hand_food.py`) prices the track in labor
before the gate is read. It takes the two recipes from the registry and
scales the gathering step by the actor's own land, the coefficient
`ProcessCommand.execute` applies before its 1-unit floor:

| biomass availability | labor turns per unit of food | viable |
|---|---|---|
| 1.0 | 0.375 | yes |
| 0.2 | 0.875 | yes |
| 0.1 | 1.5 | no |
| 0.0 | infinite | no |

Viable means one labor turn covers `DAILY_CONSUMPTION`, which is 1 food.
Below that line the brains skip the whole food block and the actor falls
through to its other work. The food drive's own market bid is untouched, so
those residents buy imported food out of the wage. The verdict depends only
on the land and the recipes, so it is memoized in the never-reset group of
`BrainCache` and dropped with the rest of the cache when the actor migrates.

The skill multiplier is left out of the figure: it scales the inputs and
outputs of both steps alike and it changes over an actor's life, whereas
land does not.

### Displacement bids for goods made by hand

A need gate in `decide_economic_action` spends a labor turn making the good
itself and leaves no order behind, so the planet shows no demand for it,
nothing is shipped in, and the gate fires again next turn. On biomass-poor
planets colonists cook food from a 2-unit gather while a neighbour two or
three fuel units away sells processed food at 1-3 credits.

Both brains record what a need gate makes (`ActorBrain._record_self_supply`,
cleared each turn by `_begin_self_supply_record`) and
`ActorBrain._displacement_bid_commands` posts a bid for it:

| Term | Value |
|------|-------|
| ceiling | `_drive_willingness_to_pay` with `labor_value = labor_opportunity_cost`, or the good's own `_replacement_cost` at that labor value when it is a recipe input rather than a drive material |
| price | the local ask if it is at or under the ceiling, else `min(ceiling, reference * (1 + scarcity_pressure))` |
| targets | every material of the matched drive when the recorded good is one of them, else the recorded good alone |
| quantity | one run's base output per target, less what the drive passes already bid for it, capped by the money left |
| when | after the primary and substitute bids |

The bid covers every material of the drive, not only the good the gate
produced. What displaces the labor is whatever a ship can bring cheaply, and
for the food drive that is the imported staple rather than the hand-cooked
`food` the gate makes. Either material meets the need and matching is
deferred, so both orders may rest and both may fill; the pantry then sits
above target and the actor skips cooking for longer.

`labor_opportunity_cost` is what a turn of the actor's labor is worth: the
raw profit of a colonist's next-best process or an industrialist's recipe
score, floored at `GOVERNMENT_WAGE`. `_replacement_cost` takes it as
`labor_value` and memoizes on `(commodity, labor value)`, so the wage-based
entry is never served to an opportunity-cost call. The same number prices
tool make-or-buy, so there is one definition of the labor turn.

The quantity is one run's base output, before planet-attribute scaling: what
a purchase displaces is the labor turn, and on a poor planet the scaled
output of that turn is small. The bid stands even with the pantry at target,
because its point is that a fill stops the gate from tripping next turn.
Goods no drive consumes, directly or as an input to a drive material's
recipe, draw no bid, so this raises no price for a good the actor is not
making for itself.

## Adding a new drive

A drive needs a complete supply chain or it starves silently:

1. Add the raw material and finished good to `data/commodities.yaml`, and
   gathering and production processes to `data/processes.yaml`. Use the
   `commodity-process-design` skill.
2. Create the drive class in `core/drives/` inheriting `ActorDrive`. Follow
   `clothing_drive.py` for stochastic durable goods or `food_drive.py` for
   deterministic staples. Raise `ValueError` in `__init__` if the required
   commodity is missing from the registry.
3. Wire it into actor construction and check that brains trade the new
   commodities. Brains iterate commodities dynamically; do not hardcode.

## Not implemented

Money-savings, tool-ownership, and social or variety needs have no drive
classes. Money and tools are handled directly by brain logic. Habituation
(a standard of living that creates debt when lost) is deferred.
