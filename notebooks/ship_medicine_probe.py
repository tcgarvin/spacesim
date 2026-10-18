"""Why do ships never haul medicine between planets?

Runs its own sim, then dissects every ship's state and forces a plan
evaluation for medicine on the richest arbitrage pair it can find.

    uv run python notebooks/ship_medicine_probe.py
"""

from collections import Counter, defaultdict

from spacesim2.core.ship import ShipStatus
from spacesim2.core.simulation import Simulation

TURNS = 500
PLANETS = 16
ACTORS = 100


def p(*a):
    print(*a, flush=True)


sim = Simulation()
sim.setup_simple(
    num_planets=PLANETS, num_regular_actors=ACTORS, num_market_makers=2, num_ships=1
)
reg = sim.commodity_registry
med = reg.get_commodity("medicine")
fuel = reg.get_commodity("nova_fuel")

# Sample ship-plan outcomes over the run.
plan_commodity = Counter()
no_plan_turns = 0
sampled_turns = 0
for t in range(TURNS):
    sim.run_turn()
    if t > 100 and t % 10 == 0:
        sampled_turns += 1
        for s in sim.ships:
            brain = s.brain
            if s.planet is None:
                continue
            plan = brain._plan_search_memo[2] if brain._plan_search_memo else None
            if plan is None:
                no_plan_turns += 1
            else:
                plan_commodity[plan.commodity.id] += 1

p(f"=== {PLANETS} planets x {ACTORS} actors, {TURNS} turns ===")
p(f"\nsampled docked-ship plan outcomes (every 10 turns after t=100):")
p(f"  no plan found: {no_plan_turns}")
for cid, n in plan_commodity.most_common(15):
    p(f"  plan on {cid:>26}: {n}")

p("\n--- ship state at end ---")
st = Counter(s.status.value for s in sim.ships)
p(f"status: {dict(st)}")
monies = sorted(s.money for s in sim.ships)
p(f"money: {monies}")
fuels = sorted(s.cargo.get_quantity(fuel) for s in sim.ships)
p(f"fuel:  {fuels}")
p(f"cargo units: {sorted(s.cargo.get_total_quantity() for s in sim.ships)}")

# Why is no plan found? Re-run the search with instrumentation.
p("\n--- per-ship plan diagnosis (docked ships) ---")
p(
    f"{'ship':>10} {'planet':>12} {'money':>7} {'fuel':>5} {'nExport':>8} {'pairsOK':>8} {'bestPlan':>26}"
)
for s in sim.ships:
    if s.planet is None:
        continue
    b = s.brain
    nav = b._nav
    nav.refresh_market_facts(sim.current_turn)
    exportable = nav.exportable_commodities(s.planet)
    pairs_ok = 0
    for d in sim.planets:
        if d is s.planet:
            continue
        if b._pair_economics(s.planet, d) is not None:
            pairs_ok += 1
    b._plan_search_memo = None
    plan = b._find_best_trade_plan()
    desc = (
        "-"
        if plan is None
        else f"{plan.commodity.id}x{plan.quantity} p{plan.expected_profit}"
    )
    p(
        f"{s.name:>10} {s.planet.name:>12} {s.money:>7} "
        f"{s.cargo.get_quantity(fuel):>5} {len(exportable):>8} {pairs_ok:>8} {desc:>26}"
    )

# Force-evaluate medicine on the best arbitrage pair.
p("\n--- forced medicine plan evaluation on the fattest spread ---")
srcs = []
dsts = []
for pl in sim.planets:
    m = pl.market
    bid, ask = m.get_bid_ask_spread(med)
    if ask is not None:
        srcs.append((ask, pl.name, pl))
    if bid is not None:
        dsts.append((bid, pl.name, pl))
srcs.sort(key=lambda r: (r[0], r[1]))
dsts.sort(key=lambda r: (-r[0], r[1]))
p(f"cheapest medicine asks: {[(a, n) for a, n, _ in srcs[:4]]}")
p(f"highest medicine bids:  {[(b, n) for b, n, _ in dsts[:4]]}")
if srcs and dsts:
    origin = srcs[0][2]
    for _, _, dest in dsts[:3]:
        if dest is origin:
            continue
        s = sim.ships[0]
        b = s.brain
        # Give the ship a clean slate so budget is not the binding constraint.
        pair = b._pair_economics(origin, dest)
        plan = b._evaluate_trade_opportunity(origin, dest, med, pair)
        dist = b._nav.distance(origin, dest)
        p(
            f"{origin.name} -> {dest.name}: dist {dist:.1f}, "
            f"pair_economics {'OK' if pair else 'None'}, "
            f"plan {'None' if plan is None else f'{plan.quantity}u buy {plan.purchase_price_per_unit} sell {plan.expected_sell_price_per_unit} profit {plan.expected_profit} margin {plan.profit_margin:.2f} profitable={plan.is_profitable()}'}"
        )
        # Is dest even in the candidate shortlist?
        cands = b._nav.candidate_destinations(origin, med)
        p(f"    dest in candidate_destinations: {dest in cands} (n={len(cands)})")
        p(
            f"    med in exportable(origin): {med in b._nav.exportable_commodities(origin)}"
        )

# Where does medicine sit?
p("\n--- medicine stock by planet, with local book ---")
rows = []
for pl in sim.planets:
    stock = sum(a.inventory.get_quantity(med) for a in pl.actors)
    bid, ask = pl.market.get_bid_ask_spread(med)
    vol = sum(pl.market.volume_history.get(med, [])[-50:])
    depr = 0
    n = 0
    for a in pl.actors:
        for d in a.drives:
            if d.metrics.get_name() == "health":
                n += 1
                if d.metrics.debt > 0.3:
                    depr += 1
    rows.append((pl.name, stock, bid, ask, vol, round(100 * depr / max(1, n))))
p(f"{'planet':>12} {'stock':>7} {'bid':>6} {'ask':>6} {'vol50':>6} {'depr%':>6}")
for r in sorted(rows, key=lambda r: -r[1]):
    p(f"{r[0]:>12} {r[1]:>7} {str(r[2]):>6} {str(r[3]):>6} {r[4]:>6} {r[5]:>6}")
