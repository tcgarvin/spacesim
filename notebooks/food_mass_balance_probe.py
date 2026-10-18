"""Mass and cost balance for the make_food monoculture, by planet biomass.

Answers four questions over t301-450:
  1. make_food / gather_biomass attempts vs successes, and where the cooked
     biomass came from (gathered, bought on-planet, imported by ship).
  2. Food produced / eaten / sold / held, and the resting sell orders of
     actors holding more than 20 food.
  3. Which branch returned make_food: the food<5 need gate, the colonist
     profit scan, or the industrialist chosen recipe. For profit-scan cases,
     the food quote at decision time vs the price actually realized within
     30 turns.
  4. Per-unit food cost actually experienced, by bucket.

    uv run python notebooks/food_mass_balance_probe.py --turns 450 --planets 12
"""

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import GOVERNMENT_WAGE, ProcessCommand
from spacesim2.core.market import Market
from spacesim2.core.simulation import Simulation

STATE: Dict[str, Any] = {"turn": 0, "sim": None}
CALLS: Counter = Counter()
WINDOW = 301

PROC: Dict[Tuple[str, str], Counter] = defaultdict(Counter)  # (bucket, pid) -> stats
FLOW: Dict[str, Counter] = defaultdict(Counter)  # bucket -> mass flows
BRANCH: Dict[str, Counter] = defaultdict(Counter)  # bucket -> branch of make_food
SCAN_ROWS: List[Dict[str, Any]] = []  # profit-scan make_food decisions
SALES: Dict[str, List[Tuple[int, int]]] = defaultdict(list)  # actor -> (turn, price)
BIO_SPEND: Dict[str, List[int]] = defaultdict(list)  # bucket -> prices paid


def bucket_of(planet: Any) -> str:
    v = planet.attributes.biomass
    return "lo<0.4" if v < 0.4 else ("mid" if v <= 0.7 else "hi>0.7")


def is_ship(party: Any) -> bool:
    return getattr(party, "actor_type", None) is None


def install() -> None:
    o_run = Simulation.run_turn
    o_exec = ProcessCommand.execute
    o_col = ColonistBrain.decide_economic_action
    o_ind = IndustrialistBrain.decide_economic_action
    o_scan = ColonistBrain._find_most_profitable_process
    o_txn = Market._execute_transaction
    o_consume = Actor.take_turn

    def run_turn(self: Simulation) -> None:
        STATE["turn"] = self.current_turn + 1
        CALLS["run_turn"] += 1
        o_run(self)

    def execute(self: ProcessCommand, actor: Actor) -> bool:
        if STATE["turn"] < WINDOW or self.process_id not in (
            "make_food",
            "gather_biomass",
        ):
            return o_exec(self, actor)
        reg = actor.sim.commodity_registry
        bio = reg.get_commodity("biomass")
        food = reg.get_commodity("food")
        bk = bucket_of(actor.planet)
        bio_before = actor.inventory.get_quantity(bio)
        food_before = actor.inventory.get_quantity(food)
        ok = o_exec(self, actor)
        CALLS["exec"] += 1
        key = (bk, self.process_id)
        PROC[key]["attempts"] += 1
        if ok:
            PROC[key]["successes"] += 1
            PROC[key]["bio_delta"] += actor.inventory.get_quantity(bio) - bio_before
            PROC[key]["food_delta"] += actor.inventory.get_quantity(food) - food_before
        else:
            # Distinguish a missing-input refusal from a failed skill check.
            proc = actor.sim.process_registry.get_process(self.process_id)
            short = any(
                not actor.inventory.has_quantity(c, q) for c, q in proc.inputs.items()
            )
            PROC[key]["short_inputs" if short else "skill_fail"] += 1
        return ok

    def note_branch(actor: Actor, cmd: Any, branch: str, market: Any) -> None:
        if not isinstance(cmd, ProcessCommand) or cmd.process_id != "make_food":
            return
        bk = bucket_of(actor.planet)
        BRANCH[bk][branch] += 1
        if branch == "profit_scan" and market is not None:
            reg = actor.sim.commodity_registry
            food = reg.get_commodity("food")
            bio = reg.get_commodity("biomass")
            bid, ask = market.get_bid_ask_spread(food)
            SCAN_ROWS.append(
                {
                    "turn": STATE["turn"],
                    "bucket": bk,
                    "name": actor.name,
                    "food_avg": market.get_avg_price(food),
                    "food_bid": int(bid) if bid is not None else -1,
                    "bio_avg": market.get_avg_price(bio),
                }
            )

    def col_decide(self: ColonistBrain, actor: Actor) -> Any:
        STATE["scan_hit"] = False
        cmd = o_col(self, actor)
        if STATE["turn"] >= WINDOW and actor.actor_type is ActorType.REGULAR:
            CALLS["decide"] += 1
            reg = actor.sim.commodity_registry
            food = reg.get_commodity("food")
            gate = actor.inventory.get_quantity(food) < 5
            branch = (
                "profit_scan"
                if STATE.get("scan_hit")
                else ("need_gate" if gate else "other")
            )
            note_branch(actor, cmd, branch, actor.planet.market)
        return cmd

    def scan(self: ColonistBrain, actor: Actor, market: Any, cache: Any = None) -> Any:
        result = o_scan(self, actor, market, cache)
        if result is not None and result.id == "make_food":
            STATE["scan_hit"] = True
        return result

    def ind_decide(self: IndustrialistBrain, actor: Actor) -> Any:
        cmd = o_ind(self, actor)
        if STATE["turn"] >= WINDOW and actor.actor_type is ActorType.REGULAR:
            CALLS["decide"] += 1
            reg = actor.sim.commodity_registry
            food = reg.get_commodity("food")
            gate = actor.inventory.get_quantity(food) < 5
            branch = (
                "chosen_recipe"
                if self.chosen_recipe_id == "make_food"
                else ("need_gate" if gate else "other")
            )
            note_branch(actor, cmd, branch, actor.planet.market)
        return cmd

    def txn(
        self: Market, buyer: Any, seller: Any, commodity_type: Any, *a: Any, **kw: Any
    ) -> Any:
        result = o_txn(self, buyer, seller, commodity_type, *a, **kw)
        if STATE["turn"] < WINDOW:
            return result
        CALLS["txn"] += 1
        t = self.transaction_history[-1]
        # Market holds no planet back-reference, so resolve the bucket from
        # whichever counterparty is a planet-resident actor.
        planet = None
        for party in (buyer, seller):
            candidate = getattr(party, "planet", None)
            if candidate is not None and hasattr(candidate, "attributes"):
                planet = candidate
                break
        if planet is None:
            return result
        bk = bucket_of(planet)
        if commodity_type.id == "biomass":
            src = "bio_imported" if is_ship(seller) else "bio_bought_local"
            FLOW[bk][src] += t.quantity
            FLOW[bk][f"{src}_credits"] += t.total_amount
            if getattr(buyer, "actor_type", None) is ActorType.REGULAR:
                BIO_SPEND[bk].extend([int(t.price)] * t.quantity)
        elif commodity_type.id == "food":
            FLOW[bk]["food_sold"] += t.quantity
            if getattr(seller, "actor_type", None) is ActorType.REGULAR:
                SALES[seller.name].append((STATE["turn"], int(t.price)))
        return result

    def take_turn(self: Actor) -> None:
        o_consume(self)
        if STATE["turn"] >= WINDOW and self.actor_type is ActorType.REGULAR:
            if self.food_consumed_this_turn:
                FLOW[bucket_of(self.planet)]["food_eaten"] += 1

    Simulation.run_turn = run_turn  # type: ignore[method-assign]
    ProcessCommand.execute = execute  # type: ignore[method-assign]
    ColonistBrain.decide_economic_action = col_decide  # type: ignore
    ColonistBrain._find_most_profitable_process = scan  # type: ignore
    IndustrialistBrain.decide_economic_action = ind_decide  # type: ignore
    Market._execute_transaction = txn  # type: ignore[method-assign]
    Actor.take_turn = take_turn  # type: ignore[method-assign]


def pct(n: float, d: float) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "-"


BUCKETS = ("lo<0.4", "mid", "hi>0.7")


def report(sim: Simulation) -> None:
    print("\n[1] make_food and gather_biomass: attempts, outcomes, biomass mass")
    print(
        f"{'bucket':<9}{'process':<16}{'attempts':>10}{'ok':>9}{'ok%':>8}"
        f"{'short_in':>10}{'skillfail':>10}{'bio_delta':>11}{'food_out':>10}"
    )
    for bk in BUCKETS:
        for pid in ("make_food", "gather_biomass"):
            c = PROC[(bk, pid)]
            if not c["attempts"]:
                continue
            print(
                f"{bk:<9}{pid:<16}{c['attempts']:>10}{c['successes']:>9}"
                f"{pct(c['successes'], c['attempts']):>8}"
                f"{c['short_inputs']:>10}{c['skill_fail']:>10}"
                f"{c['bio_delta']:>11}{c['food_delta']:>10}"
            )

    print("\n[2] biomass sources vs cooking consumption (units)")
    print(
        f"{'bucket':<9}{'cooked_bio':>12}{'gathered':>11}{'bought_local':>14}"
        f"{'imported':>10}{'bought_px':>11}"
    )
    for bk in BUCKETS:
        cooked = -PROC[(bk, "make_food")]["bio_delta"]
        gathered = PROC[(bk, "gather_biomass")]["bio_delta"]
        px = statistics.fmean(BIO_SPEND[bk]) if BIO_SPEND[bk] else -1.0
        print(
            f"{bk:<9}{cooked:>12}{gathered:>11}"
            f"{FLOW[bk]['bio_bought_local']:>14}{FLOW[bk]['bio_imported']:>10}"
            f"{px:>11.1f}"
        )

    print("\n[3] food balance (units)")
    print(f"{'bucket':<9}{'produced':>10}{'eaten':>9}{'sold':>9}{'held_end':>10}")
    reg = sim.commodity_registry
    food = reg.get_commodity("food")
    held: Dict[str, int] = defaultdict(int)
    for actor in sim.actors:
        if actor.actor_type is ActorType.REGULAR:
            held[bucket_of(actor.planet)] += actor.inventory.get_quantity(food)
    for bk in BUCKETS:
        print(
            f"{bk:<9}{PROC[(bk, 'make_food')]['food_delta']:>10}"
            f"{FLOW[bk]['food_eaten']:>9}{FLOW[bk]['food_sold']:>9}{held[bk]:>10}"
        )

    print("\n[4] actors holding >20 food at end: resting sell orders")
    print(
        f"{'bucket':<9}{'actors':>8}{'med_held':>10}{'listed%':>9}"
        f"{'med_ask':>9}{'med_bid':>9}{'med_age':>9}"
    )
    for bk in BUCKETS:
        rich = [
            a
            for a in sim.actors
            if a.actor_type is ActorType.REGULAR
            and bucket_of(a.planet) == bk
            and a.inventory.get_quantity(food) > 20
        ]
        if not rich:
            continue
        asks: List[int] = []
        ages: List[int] = []
        bids: List[int] = []
        listed = 0
        for a in rich:
            orders = [
                o
                for o in a.planet.market.sell_orders.get(food, [])
                if not o.cancelled and o.actor is a
            ]
            if orders:
                listed += 1
                asks.extend(o.price for o in orders)
                ages.extend(sim.current_turn - o.created_turn for o in orders)
            bid, _ = a.planet.market.get_bid_ask_spread(food)
            if bid is not None:
                bids.append(int(bid))
        print(
            f"{bk:<9}{len(rich):>8}"
            f"{statistics.median([a.inventory.get_quantity(food) for a in rich]):>10.0f}"
            f"{pct(listed, len(rich)):>9}"
            f"{(statistics.median(asks) if asks else -1):>9.0f}"
            f"{(statistics.median(bids) if bids else -1):>9.0f}"
            f"{(statistics.median(ages) if ages else -1):>9.0f}"
        )

    print("\n[5] which branch returned make_food")
    labels = ("need_gate", "profit_scan", "chosen_recipe", "other")
    print(f"{'bucket':<9}{'total':>9}" + "".join(f"{x:>15}" for x in labels))
    for bk in BUCKETS:
        c = BRANCH[bk]
        tot = sum(c.values())
        if not tot:
            continue
        print(f"{bk:<9}{tot:>9}" + "".join(f"{pct(c[x], tot):>15}" for x in labels))

    print("\n[6] profit-scan make_food: quote at decision vs realized sale <=30 turns")
    print(
        f"{'bucket':<9}{'rows':>7}{'food_avg':>10}{'food_bid':>10}"
        f"{'bio_avg':>9}{'realized':>10}{'sold%':>8}"
    )
    for bk in BUCKETS:
        rows = [r for r in SCAN_ROWS if r["bucket"] == bk]
        if not rows:
            continue
        realized: List[int] = []
        matched = 0
        for r in rows:
            after = [
                p
                for t, p in SALES.get(r["name"], [])
                if r["turn"] <= t <= r["turn"] + 30
            ]
            if after:
                matched += 1
                realized.append(statistics.median(after))
        print(
            f"{bk:<9}{len(rows):>7}"
            f"{statistics.fmean([r['food_avg'] for r in rows]):>10.1f}"
            f"{statistics.fmean([r['food_bid'] for r in rows if r['food_bid'] >= 0] or [-1]):>10.1f}"
            f"{statistics.fmean([r['bio_avg'] for r in rows]):>9.1f}"
            f"{(statistics.fmean(realized) if realized else -1):>10.1f}"
            f"{pct(matched, len(rows)):>8}"
        )

    print(
        "\n[7] realized per-unit food cost (make_food: 4 biomass + 1 labor -> 2 food)"
    )
    print(
        f"{'bucket':<9}{'bio_px':>9}{'bio_cost/u':>12}{'labor/u':>10}"
        f"{'succ_rate':>11}{'cost/u':>9}{'food_px':>9}"
    )
    for bk in BUCKETS:
        c = PROC[(bk, "make_food")]
        if not c["attempts"]:
            continue
        succ = c["successes"] / c["attempts"]
        out_per_ok = c["food_delta"] / c["successes"] if c["successes"] else 0.0
        bio_px = statistics.fmean(BIO_SPEND[bk]) if BIO_SPEND[bk] else 0.0
        bio_cost = 4 * bio_px / out_per_ok if out_per_ok else -1.0
        labor = GOVERNMENT_WAGE / (out_per_ok * succ) if out_per_ok and succ else -1.0
        food_px = statistics.fmean(
            [p.market.get_avg_price(food) for p in sim.planets if bucket_of(p) == bk]
        )
        print(
            f"{bk:<9}{bio_px:>9.1f}{bio_cost:>12.1f}{labor:>10.1f}"
            f"{succ:>11.2f}{bio_cost + labor:>9.1f}{food_px:>9.1f}"
        )


def main(argv: List[str]) -> int:
    global WINDOW
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=450)
    ap.add_argument("--planets", type=int, default=12)
    ap.add_argument("--actors", type=int, default=100)
    ap.add_argument("--window-start", type=int, default=301)
    args = ap.parse_args(argv)
    WINDOW = args.window_start
    install()
    sim = Simulation()
    STATE["sim"] = sim
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    for turn in range(1, args.turns + 1):
        sim.run_turn()
        if turn % 100 == 0:
            print(f"... turn {turn}", flush=True)
    missing = [k for k in ("run_turn", "decide", "exec", "txn") if not CALLS[k]]
    if missing:
        raise RuntimeError(f"monkeypatch did not land: {missing} {dict(CALLS)}")
    print(f"### calls {dict(CALLS)}")
    report(sim)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
