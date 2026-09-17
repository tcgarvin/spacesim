# TODO

Genuinely open work only. Closed postmortems live in `docs/decision-log.md`;
perf levers live in `docs/performance.md`.

## Organizations: trading companies and ship building (2026-09-16)

`TradingCompanyBrain`, ownership, and the hull chain landed 2026-09-16
(5a1d7af..c59b524); see `docs/organizations-design.md` and the decision
log entry of the same date. Verified with unit tests and 12-planet runs
only.

State at the end of 2026-09-16, one run of 12 planets x 300 turns:

| KPI | Value |
|---|---|
| companies with a hull bid resting at t300 | 11 of 12 |
| median hull bid | ~4.3k |
| imputed hull cost | 5k-9k |
| shipyards built | 2 |
| hulls built | 0 |
| remitted to companies | 156k |

Demand and shipyard entry work; most bids sit below cost, so no ship was
built. Endowment for a spawned ship is `sim.ship_starting_capital` (the
1000 floor at 12 planets) with an empty tank.

Next session, in order:

1. 100-planet A/B against `c59b524^` (`dev ab --base 0ee2810 --python
   .venv-ft/bin/python --workers 12`, 450 turns): `fleet.ships_built`,
   `fleet.hull_bids_open`, `fleet.shipyards`, ship deliveries, food
   deprivation on barren planets, tier-3 volumes. The shipyard-recipe
   agent saw ship deliveries lower in two modified 12-planet reps than in
   two baselines (456/325 vs 675/644); confirm or dismiss as noise first.
2. If bids stay below cost, raise `SHIP_RAMP_DISCOUNT` (0.5 -> 0.75) in
   `core/brains/trading_company.py`; that lifts the 12-planet median bid
   to ~6.5k. `SHIP_VALUE_HORIZON_TURNS` is the same lever.
3. `fleet.ships_built` counts ships whose name contains `" Trading Co-"`
   (`analysis/summary.py`); replace with a stored setup count.

Open:

- Ship deletion or scrapping: nothing removes a ship from `sim.ships`, so a
  company cannot shrink a fleet that overshot.
- Companies on planets without a `shipyard` facility have no source of
  hulls to bid on.
- The endowment and starting fuel tank for a newly spawned ship are not
  yet decided.
- Tuning `SHIP_VALUE_HORIZON_TURNS` and `SHIP_RAMP_DISCOUNT` against
  measured payback times (about 215 turns on a 5k hull, about 430 on a
  20k hull).
- A 100-planet A/B of organizations against the fixed fleet size has not
  been run.

## Fleet capacity is the food-import limit (2026-09-16, evening)

After the flow-credit fix (2f1a616) and the hand-food break-even gate
(cce3791), barren planets bid 24-50 per food unit and 91+ per
processed_food unit, and the planner accepts those plans. What stops the
volume is the fleet: 50 ships x 100 hold deliver ~29 units/turn into
barren planets that eat 600-900 (`tmp/food_pairs_zero_v3.json`,
`notebooks/food_pairs_zero_v3.py`). The hold cap binds 59% of
processed_food plan evaluations; 26-38 of 50 ships are in flight at any
time; shortlist, cash and demand depth no longer bind. Consequences at 50
planets x 400 turns (`tmp/ab_handfood/table.txt`): food pct_deprived
0.022 -> 0.157, migration departures per 100 turns per 1000 actors
40 -> 29 with the passage queue 823 -> 1285 (the same fleet carries the
passengers), clothing and shelter deprivation ~0, ship money 3x.
Levers, all design decisions: ships per planet (`--ships`), hold size
(`cargo_capacity`, ship.py), or a ship-building process. Second-order:
49% of food landed at barren planets is not sold there (purchasing power
or ladder pricing; unprobed), and three barren planets with resting bids
at 28-30 drew 0-1 arrivals in 400 turns because peers bid 91-243.

## Trade volume and food-insecure planets (2026-09-16)

Landed: origin-supply cap on plan quantity (3bda0c5, deliveries +55%,
units per departure 6.4 -> 9.8) and zero-biomass planets (863c957, 1/6 of
planets). Probes: `notebooks/food_security_probe.py`,
`notebooks/food_pairs_zero_planets.py`, `notebooks/ship_hold_probe.py`.
Open, in order of measured size:

- Ranking. Food and processed_food plans into zero-biomass planets are
  built and accepted but win `_best_plan_from` 3 times in 262: the winner
  claims a median 1710 expected profit on 17 units against 267 on 36 for
  food (`tmp/food_pairs_zero_50p.json`). In 82 of 211 losses the winning
  ship flies to the hungry planet anyway with 83 empty slots. Candidates:
  rank on profit per hold unit or per turn; verify the winners' claims
  against realized trip gross first (pre-cap mean was 85 credits per trip).
- Stale cargo. Remote bid-walk turnover cap (price capped at haircut clearing price, depth at 15 turns of volume) tried after 2f1a616: neutral, deliveries -14% within noise, discarded; per-turn transit decay untried. 42% of hold contents is unsold cargo past `MAX_CARGO_HOPS`
  (median 28 of 100 units, age 37 turns, up to 13 hops): `_cargo_disposition`
  only drops over-cap lots from the hold vote and `_choose_cargo_destination`
  bumps hops for everything aboard. Forcing a local listing at the cap was
  tried and reverted (6bac331, 2e425d3): carried-over share 42% -> 40% and
  food mean_health 0.916 -> 0.889 past the variance test at 50 planets x 4
  reps, because 1814 of 1893 stale lots sit where no bid exists at all, so
  they cannot be listed. The lever is upstream: plans buy goods whose
  destination revenue is not realized, then the lot has no market anywhere
  on the ship's path.
- Shortlist reach. 39% of cheap-origin to zero-planet pairs fall off the
  top-8-by-value plus nearest-8 destination shortlist.
- Zero-biomass planets: food ~9.4 per unit, food debt ~0.2, population
  -25% by turn 600, ship food deliveries 0 after turn 150 (50 planets).

## Land (2026-09-10)

- Passage queue residual at 100 planets: 553 waiting at turn 400 with
  whole-ship government lots and 20-unit passengers (decision log,
  2026-09-11, "Passage queue"), median wait 21 turns, down from 1900 and
  34 before the fare and pickup changes. The fare gates the poorest actors
  out entirely (departures -36% at 12 planets). Levers: a cap on leaving
  mode, a crowding term in the destination score, a fare summary key so
  the fare level can be tuned from data, and a check of whether 20-unit
  passengers are what pushed waiting back up (15 units would still keep a
  passenger out of an 85-unit lot's hold).
- Government freight subsidy is at 42k per 400 turns at 100 planets
  (was 256k when jobs could be riders). Jobs-off A/B at 12 planets showed
  the jobs are a solvency floor, not a freight market; a margin 0 or 0.1
  A/B would find where the floor breaks.
- `CargoPayload` (freight and procurement contracts for real goods) is
  typed but no brain posts or carries one. First candidates: spaceport
  operators ordering fuel, industrialists ordering inputs.
- Migration herding: at 100 planets the top destinations fill to the land
  cap and the worst origin loses 76% of its residents (decision log,
  2026-09-11). The destination score has no crowding term and the softmax
  temperature (0.1) is near argmax. Candidates: a fill-ratio penalty from
  `PlanetStats.population` against pool size, a higher temperature.
- The min-one-unit output floor in `ProcessCommand.execute` means a low
  land draw barely affects 1 to 3 unit recipes (`harvest_wood`,
  `gather_fiber`, mining) and bites `gather_biomass` (8) and `farm_biomass`
  (64) hard. Decide whether to keep that asymmetry.
- Refiner siting now imputes ore cost from the actor's own draw, not the
  planet mean. Check the refiner-on-rich-planet fraction at 100 planets.

## Ship trading residuals (post fuel-station and cost-basis selling rework, 2026-09-08)

- Plan expectations are inflated: median expected profit 1346 per plan trip
  versus a median realized cycle net of 0 (mean +147) at 100 planets
  (`trip_probe.py` in the 2026-09-08 session). Plan revenue counts
  destination bid depth plus 15 turns of flow at a 10% haircut; the ladder
  on arrival realizes far less. Value plans with `_realizable_value` plus a
  shorter flow horizon and see whether fewer, better plans beat more plans.
- Refuel repositions cost a mean 858 credits per cycle (fuel bought at the
  station plus the leg) and happen 270 times per 300 turns. Ships still
  arrive short at stations that other ships drained between departure and
  arrival (89% of the residual spiked bids). A station test on operator
  stock or a depth margin above one ship's fill would cut this.
- Bunker orders reserve up to 80% of cash for a full 60-unit tank whenever
  fuel is cheap; tanks hold ~2.6k units fleet-wide (~100k credits) at turn
  400. Cap bunkering at two round trips' worth unless cash is above the
  capital floor.
- Add-on cargo (2026-09-07) is chosen once per plan and not re-bid on later
  accumulating turns; a partly filled add-on flies as is. Re-bidding the
  shortfall is untested.
- Operators still hold ~2k fuel units at turn 400 and never post delivery
  bids that a ship acts on; fuel does not move between planets by ship.
- Ships sit docked a median 10 turns per visit (`ACCUMULATION_PATIENCE` is
  8) while rich planets rest 500-1700 units of processed_food asks at 1-3
  credits; the delivery cycle is 22 turns (12 transit) and the median load
  is 19 of 100. Unverified: if the docked turns are spent accumulating
  against a book that already covers the plan, lifting the resting asks in
  one turn when depth covers the quantity would shorten the cycle and
  raise fleet throughput up to about 2x. Probe where the docked turns go
  (accumulating, laddering the sell, waiting on fuel) before changing it.

## Food imports to biomass-poor planets (2026-09-08)

Displacement bids landed: an actor that hand-makes a drive good posts a
standing bid for every material of that drive, priced with its labor
opportunity cost. Poor-planet processed_food resting depth rose from a
median 6-15 to 14-140 units, but ship imports there stayed at about 0.15
units a turn against 100 eaten. Left open:

- Local hand-food sellers fill most of the new bids at 10-15 credits, so
  the resting depth a ship can plan against is 14-50 units, which does not
  cover fuel for even a 2-3 fuel hop; every processed_food plan the gate
  probe saw sized under 25 units and lost money. The 34 units a turn of
  hand-cooked `food` that clears on those planets is the real demand for
  the drive, and no ship counts it.
- 21-24 of about 60 docked ships have no plan at all: median money 19-158
  credits and tanks at 4-6% of capacity, so every pair fails the cash
  gate. Any fleet-share goal is capped by this underclass first.
- Ships hauled 60% fewer biomass units after the change (A/B, 4 reps, 400
  turns: 318 -> 128 per 50-turn window) while ship sales value in the same
  window was flat to up (249k -> 266k credits in one paired export): the mix
  moved to fiber, tools and building materials. Confirm with a value KPI in
  the summary before treating `ship_delivered_total` as a regression.

## Medicine / upper tier (post 2026-09-03 fixes)

The WTP ceiling and phantom-bid entry are fixed (`FoodDrive.security`,
`_output_unit_value`). Left open:

- Stock discount strength (`_stock_discount`, `STOCK_REFERENCE_RUNS`,
  landed 2026-09-12): entry and the exit check discount output value by
  the producer's own unsold stock. Single 12-planet replicates put luxury
  makers at 5-15 against 45 before while luxury volume doubled; the A/B
  read luxury coverage IMPROVE. Unchecked: whether the discount thins the
  upper tiers too far at 100 planets.
- `ship_supplies` demand now comes from the proactive repair kit
  (`_buy_repair_kit`, 2026-09-08); check that producers enter the recipe.
- nova_fuel clears at 58-287 for ships while they resell at ~50. Probe the
  refiner side: is the t50-150 spike a supply gap?
- Advanced medicine is the one dead tier 3 good (decision log,
  2026-09-12). Electronics trades at about 200 and computers at about 500
  now that computers are capital, but `make_advanced_medicine` has no
  holders and health coverage is 0: the netback advanced medicine offers
  electronics (52-113) is below electronics' make cost (155-436), so
  electronics makers enter only while a resting computers bid lifts
  `make_computers` and leave on the exit check when it fills. Levers, in
  order: the advanced medicine bid (its prosperity event rate 1/90 and
  target 1 give it the smallest welfare stake of the four), the
  electronics recipe cost (rare earth plus two precision parts per unit),
  and the exit check's 10-turn cadence against a bid that comes and goes.
- Prosperity residuals: shelter need lost ~0.04 health to prefab makers
  absorbing building materials on wood-poor planets
  (`notebooks/substitute_bound_probe.py`) before prefab became a durable;
  re-measure. Prosperity food coverage is coverage of hand-cooked `food`,
  the premium good. The index is now four categories, so the map tier
  thresholds in `docs/prosperity-design.md` (0.15 / 0.4 / 0.7) were set
  against a six-category index and need re-tuning before the UI phase.
- Durables at 100 planets: `durables.computer_holder_share` and
  `prefab_holder_share` are 0.19 and 0.22 at 12 planets; unchecked at
  100, as is whether computers ever ship between planets.
- Food refresh (2026-09-07, see the decision log): the industrial track
  exists but barely runs at 300 turns on 12 planets. `process_food` is
  0.1-0.3% of process runs and chemical plants stand on 2-5 of 12 planets;
  heavy machinery trades at 175-260 credits, so a plant is a 200+ credit
  build plus upkeep. Hand food carries the staple almost everywhere. Open:
  (1) the transition is slow, not blocked: a 600-turn run had 137 plants
  and 162 farms on 12 planets, but common metal reached 217 credits and
  the health drive fell to 0.25 (WARN), so metal supply under machinery
  demand and the medicine chain are the next bottlenecks; 100 planets
  unchecked; (2) a plant out-produces its planet (one held
  16,990 processed food at turn 300) and the surplus is not exported.
  Answered 2026-09-07 (see the decision log): at 100 planets it never
  leaves. Not per-unit margin on the demand side; substitute bids now put
  45 resting bids per poor planet at the discounted food price and ships
  still carry 10 units per 100 turns. 92% of (origin, destination) pair
  evaluations fail on fuel or cash (`notebooks/processed_food_export_pairs.py`),
  and the shippable spread is 1-3 credits, one hop's worth. Next: fleet
  fuel and cash at plant planets, then plan ranking by margin per
  trip-turn instead of absolute profit. Then, once imports land, do
  biomass-poor planets still build farms? A farm scores against its own
  planet's biomass price, where hand gathering at attribute 0.2 yields
  1.6 per turn, so a local farm looks good even when a farm two
  lanes away yields 64; (3) latent numeraire
  risk: `_cheapest_effective_price` anchors lambda on the cheapest food, so
  when plants become common and the staple reaches ~2 credits every need
  drive's credit WTP falls in proportion while seller floors stay
  wage-denominated. Anchoring lambda on the hand-food make cost is the
  candidate fix; re-measure once `process_food` has real volume.
- Prosperity UI tiers (phase 3 of `docs/prosperity-design.md`) not started.

## Dev-loop improvements (from the retired roadmap)

- Test-on-edit `PostToolUse` hook: run `uv run pytest -q` on `core/**` edits,
  bootstrap/graph validation on `data/*.yaml` edits (scope matchers tightly).
- Clean stale `SPACESIM_RUN_PATH` entries in `.claude/settings.local.json`.
- `dev compare RUN_A RUN_B`: print only KPIs that moved beyond ~±0.05.
- Scenario fixtures (tool shortage, autarky, abundant ore) the smoke test can
  iterate.
