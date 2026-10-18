"""Why is medicine stockpiled while actors go without it?

Builds its own sim (in-process) so it can see every actor's inventory, drive
state, live order book, and brain internals -- the Parquet export only covers
logged actors. Run directly:

    uv run python notebooks/medicine_probe.py
"""

import statistics
from collections import defaultdict

from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import ActorBrain, BrainCache
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.simulation import Simulation

TURNS = 500
PLANETS = 16
ACTORS = 100

WATCH = ["medicine", "ship_supplies", "refined_chemicals", "chemicals", "food"]


def p(*a):
    print(*a, flush=True)


sim = Simulation()
sim.setup_simple(
    num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
)
reg = sim.commodity_registry
com = {c: reg.get_commodity(c) for c in WATCH}

# Track world stock + ship cargo over time.
history = []
for t in range(TURNS):
    sim.run_turn()
    if (t + 1) % 50 == 0:
        world = {}
        ship_held = {}
        for name, c in com.items():
            world[name] = sum(a.inventory.get_quantity(c) for a in sim.actors)
            ship_held[name] = sum(s.cargo.get_quantity(c) for s in sim.ships)
        history.append((t + 1, world, ship_held))

p(f"=== run: {PLANETS} planets x {ACTORS} actors, {TURNS} turns ===")

# ---------------------------------------------------------------- 0. stock
p("\n--- world stock over time (all actors) / [in ship cargo] ---")
p("turn   " + "  ".join(f"{n:>18}" for n in WATCH))
for turn, world, ships in history:
    p(f"{turn:>5}  " + "  ".join(f"{world[n]:>12}[{ships[n]:>3}]" for n in WATCH))


# ---------------------------------------------- 1. who holds it, by type
def actor_kind(a):
    if a.actor_type == ActorType.SERVICE:
        return "market_maker"
    return "industrialist" if isinstance(a.brain, IndustrialistBrain) else "colonist"


p("\n--- holdings by actor kind (end of run) ---")
p(f"{'commodity':>18} {'kind':>14} {'holders':>8} {'units':>9} {'mean/holder':>12}")
for name, c in com.items():
    by = defaultdict(list)
    for a in sim.actors:
        q = a.inventory.get_quantity(c)
        if q > 0:
            by[actor_kind(a)].append(q)
    ship_q = [s.cargo.get_quantity(c) for s in sim.ships if s.cargo.get_quantity(c) > 0]
    if ship_q:
        by["ship"] = ship_q
    for kind, qs in sorted(by.items()):
        p(f"{name:>18} {kind:>14} {len(qs):>8} {sum(qs):>9} {sum(qs) / len(qs):>12.1f}")

# -------------------------------------------- 2. per-planet medicine table
med = com["medicine"]
p("\n--- per-planet medicine: book, stock, deprivation ---")
p(
    f"{'planet':>12} {'bid':>5} {'bidQ':>6} {'ask':>5} {'askQ':>6} {'avg':>5} "
    f"{'scar':>5} {'vol50':>6} {'stock':>7} {'holders%':>8} {'depr%':>6} "
    f"{'mmStock':>7} {'prodStock':>9}"
)

vol50 = defaultdict(int)
for planet in sim.planets:
    mkt = planet.market
    hist = mkt.volume_history.get(med, []) if hasattr(mkt, "volume_history") else []
    vol50[planet.name] = sum(hist[-50:]) if hist else 0

planet_rows = []
for planet in sim.planets:
    mkt = planet.market
    buys = [o for o in mkt.buy_orders.get(med, []) if not o.cancelled]
    sells = [o for o in mkt.sell_orders.get(med, []) if not o.cancelled]
    bid = max((o.price for o in buys), default=0)
    ask = min((o.price for o in sells), default=0)
    regulars = [a for a in planet.actors if a.actor_type != ActorType.SERVICE]
    mms = [a for a in planet.actors if a.actor_type == ActorType.SERVICE]
    stock = sum(a.inventory.get_quantity(med) for a in planet.actors)
    holders = sum(1 for a in regulars if a.inventory.get_quantity(med) > 0)
    mm_stock = sum(a.inventory.get_quantity(med) for a in mms)
    depr = 0
    for a in regulars:
        for d in a.drives:
            if d.metrics.get_name() == "health" and d.metrics.debt > 0.3:
                depr += 1
    planet_rows.append(
        (
            planet.name,
            bid,
            sum(o.quantity for o in buys),
            ask,
            sum(o.quantity for o in sells),
            mkt.get_avg_price(med),
            round(mkt.scarcity_pressure_for(med), 2),
            vol50[planet.name],
            stock,
            round(100 * holders / max(1, len(regulars))),
            round(100 * depr / max(1, len(regulars))),
            mm_stock,
            stock - mm_stock,
        )
    )
for r in sorted(planet_rows, key=lambda r: -r[8]):
    p(
        f"{r[0]:>12} {r[1]:>5} {r[2]:>6} {r[3]:>5} {r[4]:>6} {r[5]:>5} {r[6]:>5} "
        f"{r[7]:>6} {r[8]:>7} {r[9]:>8} {r[10]:>6} {r[11]:>7} {r[12]:>9}"
    )

# ------------------------------------- 3. deprived actors: money and bids
p("\n--- deprived actors (health debt > 0.3): what are they doing? ---")
deprived = []
for a in sim.actors:
    if a.actor_type == ActorType.SERVICE:
        continue
    for d in a.drives:
        if d.metrics.get_name() == "health" and d.metrics.debt > 0.3:
            deprived.append((a, d))

p(
    f"count deprived: {len(deprived)} of {sum(1 for a in sim.actors if a.actor_type != ActorType.SERVICE)}"
)
if deprived:
    monies = sorted(a.money for a, _ in deprived)
    p(
        f"money: min {monies[0]}, p25 {monies[len(monies) // 4]}, "
        f"median {monies[len(monies) // 2]}, p75 {monies[3 * len(monies) // 4]}, "
        f"max {monies[-1]}"
    )
    have0 = sum(1 for a, _ in deprived if a.inventory.get_quantity(med) == 0)
    p(f"holding 0 medicine: {have0} / {len(deprived)}")
    # Do they have a resting medicine bid?
    with_bid = 0
    bid_prices = []
    for a, _ in deprived:
        orders = a.planet.market.get_actor_orders(a)["buy"]
        mine = [o for o in orders if o.commodity_type == med and not o.cancelled]
        if mine:
            with_bid += 1
            bid_prices.append(max(o.price for o in mine))
    p(f"with a resting medicine bid: {with_bid} / {len(deprived)}")
    if bid_prices:
        bid_prices.sort()
        p(
            f"their bid prices: min {bid_prices[0]}, median "
            f"{bid_prices[len(bid_prices) // 2]}, max {bid_prices[-1]}"
        )

    # Recompute the WTP machinery for a sample.
    p("\n  sample of 12 deprived actors: WTP decomposition")
    p(
        f"  {'actor':>26} {'money':>7} {'lam':>9} {'mw_h':>6} {'wtp':>7} "
        f"{'repl':>7} {'ref':>7} {'scar':>5} {'bid':>6} {'ask':>6} {'fbuf':>5} {'hdebt':>6}"
    )
    for a, d in deprived[:12]:
        mkt = a.planet.market
        cache = BrainCache()
        lam = ActorBrain._value_of_money(a.brain, a, mkt, cache)
        wtp = ActorBrain._drive_willingness_to_pay(a.brain, a, mkt, d, med, lam, cache)
        repl = ActorBrain._replacement_cost(a.brain, a, mkt, med, cache)
        ref = ActorBrain._drive_bid_reference(a.brain, a, mkt, med, cache)
        scar = mkt.scarcity_pressure_for(med)
        b, k = mkt.get_bid_ask_spread(med)
        fdrive = next(x for x in a.drives if x.metrics.get_name() == "food")
        p(
            f"  {a.name:>26} {a.money:>7} {lam:>9.5f} {d.marginal_welfare():>6.3f} "
            f"{wtp:>7} {('-' if repl is None else round(repl, 1)):>7} {ref:>7.1f} "
            f"{scar:>5.2f} {str(b):>6} {str(k):>6} {fdrive.metrics.buffer:>5.2f} "
            f"{d.metrics.debt:>6.2f}"
        )

# ------------------------------------------- 4. producers and their floors
p("\n--- medicine producers: floors vs consumer WTP ---")
producers = [
    a
    for a in sim.actors
    if isinstance(a.brain, IndustrialistBrain)
    and a.brain.chosen_recipe_id == "make_medicine"
]
p(f"actors currently on make_medicine: {len(producers)}")
labs = sum(
    1
    for a in sim.actors
    if reg.get_commodity("chemistry_lab")
    and a.inventory.get_quantity(reg.get_commodity("chemistry_lab")) > 0
)
p(f"actors owning a chemistry_lab: {labs}")
floors = []
for a in producers[:40]:
    cache = BrainCache()
    f = ActorBrain._replacement_cost(a.brain, a, a.planet.market, med, cache)
    if f is not None:
        floors.append(f)
if floors:
    floors.sort()
    p(
        f"producer replacement-cost floors: min {floors[0]:.1f} median "
        f"{floors[len(floors) // 2]:.1f} max {floors[-1]:.1f}"
    )
p(
    f"medicine held by producers-on-recipe: {sum(a.inventory.get_quantity(med) for a in producers)}"
)

# what recipe is everyone on?
recipe_counts = defaultdict(int)
for a in sim.actors:
    if isinstance(a.brain, IndustrialistBrain) and a.brain.chosen_recipe_id:
        recipe_counts[a.brain.chosen_recipe_id] += 1
p("\ntop recipes by headcount:")
for rid, n in sorted(recipe_counts.items(), key=lambda kv: -kv[1])[:14]:
    p(f"  {rid:>28} {n:>5}")

# ----------------------------------------------------- 5. consumption math
n_regular = sum(1 for a in sim.actors if a.actor_type != ActorType.SERVICE)
p(
    f"\nexpected medicine consumption: {n_regular} actors * 1/90 per turn = "
    f"{n_regular / 90:.1f} units/turn = {n_regular / 90 * TURNS:.0f} over the run"
)
final_stock = sum(a.inventory.get_quantity(med) for a in sim.actors) + sum(
    s.cargo.get_quantity(med) for s in sim.ships
)
p(f"final medicine stock: {final_stock}")

# ------------------------------------------------- 6. cross-planet spread
p("\n--- cross-planet price dispersion (avg price, planets with a signal) ---")
for name, c in com.items():
    prices = [
        pl.market.get_avg_price(c)
        for pl in sim.planets
        if pl.market.has_price_signal(c)
    ]
    if len(prices) >= 2:
        p(
            f"{name:>18} n={len(prices):>3} min {min(prices):>5} median "
            f"{statistics.median(prices):>6.1f} max {max(prices):>5}"
        )

# ------------------------------------------------------------- 7. ships
p("\n--- ships ---")
p(f"ships: {len(sim.ships)}, docked: {sum(1 for s in sim.ships if s.planet)}")
cargo_totals = defaultdict(int)
for s in sim.ships:
    for c, q in s.cargo.commodities.items():
        cargo_totals[c.id] += q
p("aggregate ship cargo:")
for cid, q in sorted(cargo_totals.items(), key=lambda kv: -kv[1])[:12]:
    p(f"  {cid:>22} {q:>6}")
monies = sorted(s.money for s in sim.ships)
p(f"ship money: min {monies[0]} median {monies[len(monies) // 2]} max {monies[-1]}")
