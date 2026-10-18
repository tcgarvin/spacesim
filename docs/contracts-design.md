# Contracts: Transport Jobs, Passage, and Government Freight

Status: landed 2026-09-11 (89e05eb..ac52dc7); measurements are in the
decision log entry of that date. The first use is
migration v2 (`docs/migration-design.md`, "Open"); the primitive is meant to
carry later uses without changing shape.

## Why

Migration v1 moves an actor by teleport at ship speed. The fare goes to the
spaceport operators and no ship is involved. Fleet capacity does not bound
migration: a wave of leavers moves at once, which is the herding the decision
log records at 100 planets.

A ship should carry the migrant. Once a ship can be paid to carry something
that is not its own cargo, the same mechanism covers other jobs: a
spaceport operator ordering fuel delivered, an industrialist ordering an
input, the government paying for routine freight. That last one is the
baseline the fleet is missing today: a broke ship has no income and a
docked ship on a poor planet has nothing worth exporting.

## The primitive

One data type, `Contract`, in `core/contracts.py`. Core owns what a contract
does. Brains own whether to post one and whether to accept one, the same
split as migration and market orders.

```python
@dataclass
class Contract:
    contract_id: str
    poster: Poster            # an Actor, or GOVERNMENT
    origin: Planet
    destination: Planet
    payload: Payload          # see below
    advance: int              # paid to the carrier when the payload loads
    on_delivery: int          # paid to the carrier on arrival at destination
    posted_turn: int
    expires_turn: int
    status: ContractStatus    # OPEN, ACCEPTED, LOADED, DELIVERED, CANCELLED, EXPIRED
    carrier: Ship | None
```

Payloads, one class each, all with a `hold_units` property:

| Payload | Carries | Hold units | First user |
|---------|---------|------------|------------|
| `PassengerPayload(actor, land)` | a regular actor, with the destination land claimed at load | `MIGRANT_CARGO_UNITS` | migration v2 |
| `ConsignmentPayload(units)` | an abstract lot that exists only inside the contract | `units` | government freight |
| `CargoPayload(commodity, quantity, supplied_by)` | real goods; `supplied_by` is `POSTER` (freight: the poster hands the goods over at load) or `CARRIER` (procurement: the ship sources them and is paid on delivery) | `quantity` | later: operator fuel orders, input orders |

v2 implements the first two. `CargoPayload` is in the design so the type does
not have to change for it, but no brain posts or accepts one yet.

Money. A poster that is an actor reserves `advance + on_delivery` at posting,
the way a bid reserves money, and the reserve is released on cancel or
expiry. The government reserves nothing; its payouts are created, the same
way the government wage is. Every payment goes to the carrier in full. v1's
fare went to the origin's spaceport operators; a port fee to keep that flow
was considered and rejected on 2026-09-11 as unneeded.

Boards. Each planet has a `ContractBoard` holding its open contracts, next to
its market. Ships read every board, as they already read every market, so a
ship two lanes away can plan a trip around a contract. Boards expire
contracts at the top of the turn.

Lifecycle, all in core:

| Step | Who calls it | What happens |
|------|--------------|--------------|
| `board.post(contract)` | migration phase, government refresh | reserve poster money, status OPEN |
| `board.accept(contract, ship)` | ship brain, docked at origin | status ACCEPTED, `ship.contracts` gains it; hold units count against the hold from here |
| `load_contract(ship, contract)` | `Ship.start_journey`, after the fuel check passes | payload boards or is handed over, advance paid, status LOADED |
| `deliver_contract(ship, contract)` | `Ship.update_journey` on arrival at `destination` | payload delivered, `on_delivery` paid, status DELIVERED |
| `release_contract(ship, contract)` | ship brain, or core after `CONTRACT_LOAD_PATIENCE` turns unloaded | back to OPEN on the board, no money moves |
| `strand_contract(ship, contract, planet)` | core, when a loaded ship ends up docked somewhere that is not the destination for `CONTRACT_STRAND_PATIENCE` turns | payload unloaded where the ship is, `on_delivery` refunded to the poster, status CANCELLED |
| `board.cancel(contract)` | poster's brain, before acceptance | reserve released |

A passenger that is stranded draws a land at the stranding planet if the
pool has one, and otherwise stays aboard until the ship next docks at a
planet that does. Stranding should be rare; it is counted.

## Migration v2 on top of this

`MigrationRequest` keeps `destination` and gains `fare_offer`, what the actor
is offering now. `core/migration.py` no longer moves anyone:

1. Actor phase: the brain returns a request, as today.
2. Ship phase: ships accept, load, and deliver. Delivery calls
   `relocate_actor`, which is unchanged.
3. Contract phase, where the migration phase is now: a request with no open
   contract posts one (`advance = fare_offer`, `on_delivery = 0`, expiry
   `PASSAGE_CONTRACT_TTL` turns); a request with an open contract at a
   higher offer re-prices it; `NO_MIGRATION` from an actor with an open
   contract cancels it.
4. Market matching.

The fare is paid in full at boarding, not on delivery, so a passenger can
fund a ship's fuel the way a government job can. The ship's reason to
deliver is that a loaded contract pins its destination, below.

While the contract is open the actor keeps living on the origin planet:
eats, works, sells. The v1 "leaving" mode of the brain (no tools, no
facilities, no recipes, sell everything) stays on. The brain escalates
`fare_offer` from `passage_fare` toward its cap over
`FARE_ESCALATION_TURNS` (20) while unaccepted, and drops the
intent if the contract expires twice. That gives fare price discovery
without a second market.

The fare is priced like a government job, off the same
`leg_fuel_cost(navigator, origin, destination)` in `core/navigation.py`:
`max(20, ceil(leg_fuel_cost * (1 + PASSAGE_FARE_MARGIN)))` with
`PASSAGE_FARE_MARGIN` 0.5 against the job's 0.25, and the escalation carries
it to fuel x 2.25. A job is a ship's fallback and only has to beat idling; a
passage asks the ship to fly a destination it did not choose, so the fare
has to cover the trip's expected maintenance as well as its fuel.

The land is claimed at load, not at post, so an open contract holds nothing
at the destination and the no-overshoot guarantee from v1 still holds.

`sim.migrants_in_transit` goes away; a migrant in flight is a
`PassengerPayload` on a ship. The `migration` summary block keeps its
counts and adds median turns from post to load.

## The ship brain

`TraderBrain` today picks a destination three ways: a loaded `TradePlan`, a
hold of cargo worth more elsewhere, or an empty reposition. Contracts join
in two places and change nothing about how those three are judged.

**Riders.** When the brain has settled on a destination for this departure,
`_accept_riders(destination)` walks the local board for OPEN contracts bound
for any planet on `Navigator.route(origin, destination)`, sorts by
`(advance + on_delivery) / hold_units`, and accepts them while they fit the
hold left after the plan's cargo and add-ons. Riders are pure revenue on a
trip already paid for, so no margin test applies. This is the add-on cargo
rule applied to contracts.

An intermediate rider costs the trip nothing but hold space: the ship flies
past those planets anyway, and `Ship._deliver_passed_contracts` drops the
payload as the ship passes, without docking. A ship that docks at an
intermediate planet for fuel has already delivered what was bound for it,
since the deliveries run first. Both that and the en-route stop share one span
test, `Ship._route_nodes_passed`.

**Riders decide the trip.** Accepting riders after the destination is settled
leaves the board out of the choice of destination, which is where it belongs:
a queue at the origin is a reason to pick one haul over another. The value of
that queue for a given trip is `_route_rider_value(origin, destination,
free_hold)`, the open contracts the route serves filled greedily by payment
per hold unit, and it enters every ranking the brain does — the best trade
plan out of an origin, the trade-plan side of the contract-trip comparison,
the cargo hop's destination, and the origin score in
`_find_reposition_target`.

It stays out of `TradePlan.is_profitable` and the `MIN_MARGIN` test. A rider
rides a trip the ship was flying anyway, so it must not make a marginal haul
look worth flying; it decides between hauls that already qualify on their own
cargo. A `ContractPlan` is not credited with it, since the jobs it would carry
are already its profit.

**Pickup stops.** A ship already in flight can break the journey at an
intermediate node for a turn to take on contracts bound farther along its own
route. The mechanics are the refuel stop's: `Ship._take_route_stop` asks both
`wants_refuel_stop` and `wants_pickup_stop` about each node passed, a stop may
serve either reason or both, the shortfall accounting and
`REFUEL_STOP_MAX_SHORTFALL` are shared, and the resume is
`start_journey(..., resuming=True)`, which loads what the stop accepted.
`wants_pickup_stop` accepts when the riders' payment beats the trip's own
per-turn value over the stop's turns plus the shortfall fuel, and the ship can
pay that shortfall. A stop that cannot depart within `REFUEL_STOP_MAX_TURNS`
is abandoned and the contracts go back on the board. Stops are counted in
`Ship.pickup_stop_turns` and reported as `contracts.pickup_stops_window`.

This is what reaches contracts posted after a ship departed, and boards on
planets no ship treats as an origin. It is the in-flight counterpart of remote
pickup below.

**Contract trips.** `_best_contract_trip()` groups the OPEN contracts on the
local board by destination and values each group as a plan: payments, less the
outbound fuel priced the way `TradePlan.total_fuel_cost` prices it, less
expected maintenance. The best group becomes a `ContractPlan` and takes the
place of a trade plan in the lifecycle. It passes the same
`_fuel_safe_destination` gate and the same cash gate for round-trip fuel,
with one difference: the advance counts as cash for that gate, because it is
paid at load. A contract trip must more than cover its fuel and maintenance,
the test a distressed ship already applies to trade plans.

The two compete directly: the brain evaluates both searches and adopts
whichever has the higher `_trip_turn_value`, profit over the turns the trip
occupies, with a trade plan winning ties. A ship listing cargo where it sits
runs the contract search as well, since the trade-plan search there waits for
the listing to go stale and a partly filled listing never does.

Load happens in `start_journey`, after the fuel is deducted, so the advance
is not in hand when the fuel bid goes in on the docked turn. When money alone
does not cover the fuel, `_execute_contract_plan` therefore calls
`load_contract` for the group's consignments on the docked turn, before the
bid. A consignment load has no effect beyond the payment and the status, so
an early load costs nothing if the departure slips; the payload is then
pinned to the destination and core strands it if the ship never leaves.
Passengers are never boarded early. That is what lets a ship with no money
and a dry tank take a government job, buy fuel with the advance, and leave.

**Pinning.** A ship holding a LOADED contract flies to its destination. With
several aboard the pin goes to the destination whose route covers the most of
the others, farthest first among equals; the rest are delivered as the ship
passes them, and anything the route misses waits for the stranding rule. In
`decide_travel` this sits where the loaded-plan branch is: if the fuel gate
allows, depart; otherwise stay, and `decide_trade_actions` commits
`_committed_fuel_need` to that leg so the top-up funds it. The cargo-hop and
reposition branches do not run while a loaded contract is aboard. A refuel
stop is still allowed; the passenger stays aboard through it, and the resume
destination is the contract's. If the ship is still docked short of the
destination after `CONTRACT_STRAND_PATIENCE` (proposed 10) turns, core
strands the contract.

**Remote pickup.** `_find_reposition_target` scores candidate origins by the
best plan sourced there, plus the riders that plan's route would carry, against
the best contract trip available there valued on the whole hold — the two are
alternative uses of one hold, so the score is the larger, not the sum. A planet
with a queue of passengers and nothing to export can still pull an empty ship
in. A ship that has held no
plan for `REPOSITION_CONTRACT_PATIENCE` docked turns will fly to such a queue
on the payments alone, without that trip having to beat the trade plans
elsewhere. This is the only part of the design that reaches poor planets, and
it is the part to measure first: v1's whole purpose was planets ships do not
call at.

Everything above lives in `TraderBrain`; nothing in `Ship` decides. `Ship`
gains `contracts`, `free_hold()` (capacity less cargo less accepted
contract units), the load call in `start_journey`, and the deliver call in
`update_journey`.

## Government freight

`core/government.py`, `refresh_government_jobs(sim)`, runs at the top of the
turn with `refresh_planet_stats`. Every planet's board is kept at
`GOVERNMENT_JOBS_PER_PLANET` (proposed 1) open government contracts:

| Field | Value |
|-------|-------|
| payload | `ConsignmentPayload(units)`, units drawn from `GOVERNMENT_JOB_UNITS_MIN..MAX` (85 to 100), so a job is a whole trip and never a rider |
| destination | a random planet within `GOVERNMENT_JOB_MAX_HOPS` (proposed 2) lanes |
| advance | `ceil(fuel_required * fuel_reference * (1 + GOVERNMENT_JOB_MARGIN))`, margin proposed 0.25 |
| on_delivery | 0 |
| expiry | `GOVERNMENT_JOB_TTL` turns, proposed 30, then re-rolled |

`fuel_required` is `Ship.calculate_fuel_needed(distance)` at efficiency 1.0
and `fuel_reference` is `Navigator.fuel_value_reference()`, or
`FUEL_BID_FALLBACK_FLOOR` before anything has traded. A taken job is
replaced the next turn, so the supply is one job per planet at a time, not
one per ship.

Landed. `refresh_government_jobs` runs in `run_turn` right after the board
expiry loop, and the five constants above are module constants in
`core/government.py` at their proposed values. Destination membership is
decided by `Navigator.route`, as written; a breadth-first walk of the lane
graph bounds the candidate set first, and the in-range set is cached per
planet since the galaxy is static after setup. `sim.government_payouts`
counts created money at the one place in `contracts.py` where the
government pays a carrier, and the summary's `contracts` block reports it.

Sizing. The advance covers one leg's fuel at the typical galaxy price and a
quarter more. A trade plan needs 15% on its purchase cost, which at the
cargo values ships move is several times a leg's fuel, so a job beats a
trade only when there is no trade. Where fuel is spiked locally the advance
does not cover it; the ship should reposition to a station, as it does now.

Money. These payouts are created. At 100 planets, one job per planet, a
mean leg of a few fuel units at a reference near 40, and a fill rate under
one per planet per 30 turns, the ceiling is on the order of a few hundred
credits per planet per 100 turns, against a government wage that already
creates 10 per actor-turn. The summary reports `government_payouts` so the
number is checked rather than assumed.

## What this should change

Before and after, `dev ab --base 619c4a8`, 12 planets and then 100:

| KPI | Expect |
|-----|--------|
| migration departures per 100 turns per 1000 actors | lower than v1; bounded by fleet trips now |
| migrant outcome at +100 turns (`notebooks/migration_probe.py`) | no worse than v1 |
| median turns from post to load | the new number; large on poor planets is the failure to watch for |
| passage contracts expired | near zero, or the pickup logic is not reaching them |
| stranded ships, idle docked turns | down; jobs give idle ships a paid move |
| ships with money below one short round trip | down |
| government payouts per 100 turns | small against total money |
| fleet money at t400 | up by less than the payouts, or jobs are pure subsidy |

## Decisions to make

1. `MIGRANT_CARGO_UNITS`: 10 or 100. At 100 every migrant is a whole trip.
   v1 moved 295 actors in 400 turns at 12 planets with 12 ships; at 100
   units that is 295 passenger-only trips against roughly 0.2 trips a ship
   can fly per turn, so about a third of the fleet's capacity. At 10 a
   passenger is a rider on a trade the ship was making anyway. Landed at
   10, then raised to 20 the same day when government lots became 85 to
   100 units, so that no passenger fits beside a lot.
2. Government payload: abstract consignment, or real goods the government
   buys and sells. Consignment does not touch the goods economy; real goods
   would. Proposal: consignment now; real goods come free with
   `CargoPayload` if wanted later.
3. Fare timing: all at boarding (proposal), or split with delivery. Boarding
   funds fuel for broke ships; the destination pin is what makes delivery
   happen.
4. Port fee to operators: rejected, none.
5. Government job supply and price: one open job per planet, two hops,
   fuel times 1.25. All three are constants and any of them can be an A/B.
6. Remote pickup in `_find_reposition_target`: in v2, or measured first
   without it. Proposal: in v2, since poor planets are the case that
   matters.
