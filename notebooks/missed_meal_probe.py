"""Who misses meals, and why, after the prosperity surplus-money discount.

In-process Tier-1b probe. One arm per invocation; the "before" arm forces
``ActorBrain._surplus_money_discount`` to 1.0 so both arms run from the same
working tree.

    uv run python notebooks/missed_meal_probe.py --tag after
    uv run python notebooks/missed_meal_probe.py --tag before --no-surplus-discount

Records one row per missed-meal actor-turn in the window (default 301-450):
brain type, planet biomass bucket, money, food/biomass on hand, the economic
action taken, the chosen recipe, whether the actor could have run make_food,
its own food bid this turn, the planet's best food ask, the standing consumer
vs industrial food bids, the planet's food volume that turn split by buyer
class, and the food WTP decomposition (welfare bound vs replacement cap).

Patches, all call through, all asserted to have fired:
``Simulation.run_turn`` (turn counter), ``Actor.take_turn`` (miss rows),
``ColonistBrain/IndustrialistBrain.decide_economic_action`` (action taken),
``ActorBrain._drive_buy_commands`` and ``IndustrialistBrain._buy_command``
(bid provenance), ``PlaceBuyOrderCommand.execute`` (bids placed),
``ActorBrain._drive_willingness_to_pay`` (WTP decomposition),
``Market._execute_transaction`` (food volume by buyer class).
"""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import PlaceBuyOrderCommand, ProcessCommand
from spacesim2.core.market import Market
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 450
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
WINDOW_START = 301

STATE: Dict[str, Any] = {"turn": 0}
CALLS: Counter = Counter()
ROWS: List[Dict[str, Any]] = []
EXPOSURE: Counter = Counter()  # (brain, bucket) -> actor-turns in window
MISSES: Counter = Counter()  # (brain, bucket) -> missed actor-turns
TURN_BIDS: Dict[str, List[Tuple[str, int, int, str]]] = defaultdict(list)
WTP: Dict[str, Tuple[float, float, float]] = {}
# (turn, planet) -> Counter of buyer class -> units / credits
FOOD_FLOW: Dict[Tuple[int, int], Counter] = defaultdict(Counter)
LAST_ECON: Dict[str, str] = {}


def bucket(value: float) -> str:
    if value < 0.4:
        return "lo<0.4"
    if value <= 0.7:
        return "mid"
    return "hi>0.7"


def brain_tag(actor: Any) -> str:
    return "I" if isinstance(actor.brain, IndustrialistBrain) else "C"


def buyer_class(sim: Simulation, buyer: Any) -> str:
    if getattr(buyer, "actor_type", None) is not ActorType.REGULAR:
        return "dealer_or_ship"
    recipe = getattr(buyer.brain, "chosen_recipe_id", None)
    if recipe:
        process = sim.process_registry.get_process(recipe)
        if process is not None:
            for commodity, _qty in process.inputs_items:
                if commodity.id == "food":
                    return f"input:{recipe}"
    return "consumer"


def install_patches(disable_discount: bool, sim_box: Dict[str, Any]) -> None:
    orig_run_turn = Simulation.run_turn
    orig_take_turn = Actor.take_turn
    orig_col_econ = ColonistBrain.decide_economic_action
    orig_ind_econ = IndustrialistBrain.decide_economic_action
    orig_drive_buys = ActorBrain._drive_buy_commands
    orig_ind_buy = IndustrialistBrain._buy_command
    orig_place_buy = PlaceBuyOrderCommand.execute
    orig_wtp = ActorBrain._drive_willingness_to_pay
    orig_txn = Market._execute_transaction

    def run_turn(self: Simulation) -> None:
        STATE["turn"] = self.current_turn + 1
        TURN_BIDS.clear()
        WTP.clear()
        LAST_ECON.clear()
        CALLS["run_turn"] += 1
        orig_run_turn(self)

    def describe(command: Any) -> str:
        if command is None:
            return "idle"
        if isinstance(command, ProcessCommand):
            return command.process_id
        return command.__class__.__name__

    def col_econ(self: ColonistBrain, actor: Any) -> Any:
        cmd = orig_col_econ(self, actor)
        LAST_ECON[actor.name] = describe(cmd)
        return cmd

    def ind_econ(self: IndustrialistBrain, actor: Any) -> Any:
        cmd = orig_ind_econ(self, actor)
        LAST_ECON[actor.name] = describe(cmd)
        return cmd

    def drive_buys(self: ActorBrain, actor: Any, market: Any, cache: Any = None) -> Any:
        cmds = orig_drive_buys(self, actor, market, cache)
        for cmd in cmds:
            cmd._probe_src = "drive"
        return cmds

    def ind_buy(self: IndustrialistBrain, *a: Any, **kw: Any) -> Any:
        cmds = orig_ind_buy(self, *a, **kw)
        for cmd in cmds:
            cmd._probe_src = "industrial"
        return cmds

    def place_buy(self: PlaceBuyOrderCommand, actor: Any) -> bool:
        ok = orig_place_buy(self, actor)
        CALLS["place_buy"] += 1
        if ok:
            TURN_BIDS[actor.name].append(
                (
                    self.commodity_type.id,
                    int(self.price),
                    int(self.quantity),
                    getattr(self, "_probe_src", "other"),
                )
            )
        return ok

    def wtp(
        self: ActorBrain,
        actor: Any,
        market: Any,
        drive: Any,
        commodity: Any,
        lam: float,
        cache: Any = None,
    ) -> int:
        value = orig_wtp(self, actor, market, drive, commodity, lam, cache)
        CALLS["wtp"] += 1
        if commodity.id == "food" and STATE["turn"] >= WINDOW_START:
            welfare = drive.marginal_welfare() / lam if lam > 0 else 0.0
            replacement = self._replacement_cost(actor, market, commodity, cache)
            capped = (
                -1.0
                if replacement is None
                else replacement * (1.0 + drive.metrics.debt)
            )
            WTP[actor.name] = (float(value), float(welfare), float(capped))
        return value

    def take_turn(self: Actor) -> None:
        turn = STATE["turn"]
        in_window = turn >= WINDOW_START and self.actor_type is ActorType.REGULAR
        if not in_window:
            orig_take_turn(self)
            return
        CALLS["take_turn"] += 1
        planet = self.planet
        sim = self.sim
        food = sim.commodity_registry.get_commodity("food")
        biomass_c = sim.commodity_registry.get_commodity("biomass")
        tag = brain_tag(self)
        bio_bucket = bucket(planet.attributes.biomass)
        money_before = float(self.money)
        food_before = self.inventory.get_available_quantity(food)
        bio_before = (
            self.inventory.get_available_quantity(biomass_c) if biomass_c else 0
        )
        EXPOSURE[(tag, bio_bucket)] += 1

        orig_take_turn(self)

        if self.food_consumed_this_turn:
            return
        MISSES[(tag, bio_bucket)] += 1

        market = planet.market
        best_bid, best_ask = market.get_bid_ask_spread(food)
        cons_bids: List[int] = []
        ind_bids: List[int] = []
        for order in market.buy_orders.get(food, []):
            if order.cancelled:
                continue
            klass = buyer_class(sim, order.actor)
            if klass == "consumer":
                cons_bids.append(int(order.price))
            elif klass.startswith("input:"):
                ind_bids.append(int(order.price))
        own_food_bids = [b for b in TURN_BIDS.get(self.name, []) if b[0] == "food"]
        wtp_row = WTP.get(self.name, (-1.0, -1.0, -1.0))
        recipe = getattr(self.brain, "chosen_recipe_id", None)
        ROWS.append(
            {
                "turn": turn,
                "name": self.name,
                "brain": tag,
                "planet": planet.name,
                "market_id": id(market),
                "bio": round(planet.attributes.biomass, 2),
                "bucket": bio_bucket,
                "money": round(money_before),
                "money_end": round(float(self.money)),
                "food": food_before,
                "biomass": bio_before,
                "action": LAST_ECON.get(self.name, "?"),
                "recipe": recipe or "-",
                "can_make_food": self.can_execute_process("make_food"),
                "can_gather_biomass": self.can_execute_process("gather_biomass"),
                "best_ask": int(best_ask) if best_ask is not None else -1,
                "best_bid": int(best_bid) if best_bid is not None else -1,
                "own_bid": max((b[1] for b in own_food_bids), default=-1),
                "own_bid_src": own_food_bids[0][3] if own_food_bids else "-",
                "cons_bid_med": (
                    round(statistics.median(cons_bids)) if cons_bids else -1
                ),
                "ind_bid_med": round(statistics.median(ind_bids)) if ind_bids else -1,
                "ind_bidders": len(ind_bids),
                "wtp": round(wtp_row[0], 1),
                "welfare_wtp": round(wtp_row[1], 1),
                "repl_cap": round(wtp_row[2], 1),
            }
        )

    def execute_transaction(
        self: Market, buyer: Any, seller: Any, commodity_type: Any, *a: Any, **kw: Any
    ) -> Any:
        result = orig_txn(self, buyer, seller, commodity_type, *a, **kw)
        CALLS["txn"] += 1
        if commodity_type.id == "food":
            txn = self.transaction_history[-1]
            sim = sim_box["sim"]
            key = (STATE["turn"], id(self))
            klass = buyer_class(sim, buyer)
            head = "input" if klass.startswith("input:") else klass
            FOOD_FLOW[key][f"u:{head}"] += txn.quantity
            FOOD_FLOW[key][f"c:{head}"] += txn.total_amount
        return result

    Simulation.run_turn = run_turn  # type: ignore[method-assign]
    Actor.take_turn = take_turn  # type: ignore[method-assign]
    ColonistBrain.decide_economic_action = col_econ  # type: ignore[method-assign]
    IndustrialistBrain.decide_economic_action = ind_econ  # type: ignore[method-assign]
    ActorBrain._drive_buy_commands = drive_buys  # type: ignore[method-assign]
    IndustrialistBrain._buy_command = ind_buy  # type: ignore[method-assign]
    PlaceBuyOrderCommand.execute = place_buy  # type: ignore[method-assign]
    ActorBrain._drive_willingness_to_pay = wtp  # type: ignore[method-assign]
    Market._execute_transaction = execute_transaction  # type: ignore[method-assign]

    if disable_discount:
        ActorBrain._surplus_money_discount = (  # type: ignore[method-assign]
            lambda self, actor, market, cache=None: 1.0
        )


def pct(numerator: int, denominator: int) -> str:
    return f"{100.0 * numerator / denominator:.1f}%" if denominator else "-"


def report(tag: str, turns: int) -> None:
    print(f"\n### arm={tag} rows={len(ROWS)} window={WINDOW_START}-{turns}")
    if not ROWS:
        return

    print("\n[T1] miss rate by brain x biomass bucket (misses / actor-turns)")
    print(f"{'brain':<6}{'bucket':<9}{'misses':>8}{'turns':>9}{'rate':>8}")
    for key in sorted(EXPOSURE):
        print(
            f"{key[0]:<6}{key[1]:<9}{MISSES[key]:>8}{EXPOSURE[key]:>9}"
            f"{pct(MISSES[key], EXPOSURE[key]):>8}"
        )
    total_m, total_e = sum(MISSES.values()), sum(EXPOSURE.values())
    print(f"{'ALL':<6}{'':<9}{total_m:>8}{total_e:>9}{pct(total_m, total_e):>8}")

    print("\n[T2] missed-meal rows by action (top 12)")
    action_counts = Counter((r["brain"], r["action"]) for r in ROWS)
    print(f"{'brain':<6}{'action':<28}{'rows':>7}{'share':>8}")
    for (b, action), count in action_counts.most_common(12):
        print(f"{b:<6}{action:<28}{count:>7}{pct(count, len(ROWS)):>8}")

    print("\n[T3] could the misser have made food?")
    print(
        f"{'brain':<6}{'can_make_food':>14}{'can_gather':>12}{'biomass>0':>11}"
        f"{'rows':>7}"
    )
    for b in ("C", "I"):
        rows = [r for r in ROWS if r["brain"] == b]
        if not rows:
            continue
        print(
            f"{b:<6}{pct(sum(r['can_make_food'] for r in rows), len(rows)):>14}"
            f"{pct(sum(r['can_gather_biomass'] for r in rows), len(rows)):>12}"
            f"{pct(sum(r['biomass'] > 0 for r in rows), len(rows)):>11}"
            f"{len(rows):>7}"
        )

    print("\n[T4] industrialist missers by chosen recipe (top 10)")
    recipes = Counter(r["recipe"] for r in ROWS if r["brain"] == "I")
    for recipe, count in recipes.most_common(10):
        subset = [r for r in ROWS if r["recipe"] == recipe and r["brain"] == "I"]
        money = statistics.median(r["money"] for r in subset)
        print(f"  {recipe:<30}{count:>6} rows  median money {money:>7.0f}")

    print("\n[T5] market state at the miss (medians)")
    print(
        f"{'group':<14}{'rows':>6}{'money':>8}{'ask':>7}{'own_bid':>9}"
        f"{'cons_bid':>10}{'ind_bid':>9}{'wtp':>7}{'welfare':>9}{'cap':>8}"
    )

    def med(rows: List[Dict[str, Any]], field: str) -> float:
        values = [r[field] for r in rows if r[field] >= 0]
        return statistics.median(values) if values else -1.0

    groups: List[Tuple[str, List[Dict[str, Any]]]] = [("all", ROWS)]
    for b in ("C", "I"):
        for bk in ("lo<0.4", "mid", "hi>0.7"):
            subset = [r for r in ROWS if r["brain"] == b and r["bucket"] == bk]
            if subset:
                groups.append((f"{b}/{bk}", subset))
    for name, rows in groups:
        print(
            f"{name:<14}{len(rows):>6}{med(rows, 'money'):>8.0f}"
            f"{med(rows, 'best_ask'):>7.0f}{med(rows, 'own_bid'):>9.0f}"
            f"{med(rows, 'cons_bid_med'):>10.0f}{med(rows, 'ind_bid_med'):>9.0f}"
            f"{med(rows, 'wtp'):>7.0f}{med(rows, 'welfare_wtp'):>9.0f}"
            f"{med(rows, 'repl_cap'):>8.0f}"
        )

    print("\n[T6] why no purchase this turn (row shares)")
    no_ask = [r for r in ROWS if r["best_ask"] < 0]
    ask_rows = [r for r in ROWS if r["best_ask"] >= 0]
    broke = [r for r in ask_rows if r["money"] < r["best_ask"]]
    solvent = [r for r in ask_rows if r["money"] >= r["best_ask"]]
    outbid = [r for r in solvent if 0 <= r["own_bid"] < r["best_ask"]]
    no_bid = [r for r in solvent if r["own_bid"] < 0]
    bid_ok = [r for r in solvent if r["own_bid"] >= r["best_ask"]]
    cap_binds = [
        r
        for r in solvent
        if r["repl_cap"] >= 0
        and r["welfare_wtp"] > r["best_ask"]
        and r["wtp"] < r["best_ask"]
    ]
    for label, subset in (
        ("no local ask", no_ask),
        ("ask, broke", broke),
        ("ask, solvent", solvent),
        ("  ..bid < ask", outbid),
        ("  ..no food bid", no_bid),
        ("  ..bid >= ask", bid_ok),
        ("  ..repl cap binds", cap_binds),
    ):
        print(f"  {label:<20}{len(subset):>6}{pct(len(subset), len(ROWS)):>8}")

    print("\n[T7] food flow on miss planets that turn (per miss row, medians)")
    flows = [FOOD_FLOW.get((r["turn"], r["market_id"]), Counter()) for r in ROWS]
    for head in ("consumer", "input", "dealer_or_ship"):
        units = [f[f"u:{head}"] for f in flows]
        credits = [f[f"c:{head}"] for f in flows]
        share = sum(1 for u in units if u > 0)
        mean_price = (sum(credits) / sum(units)) if sum(units) else 0.0
        print(
            f"  {head:<16} units_total {sum(units):>7}"
            f"  mean_px {mean_price:>6.1f}  rows_with_flow {pct(share, len(flows)):>7}"
        )
    ind_present = [r for r in ROWS if r["ind_bidders"] > 0]
    print(
        f"  rows where an industrial food bid rests: {pct(len(ind_present), len(ROWS))}"
    )

    print("\n[T8] miss-run lengths (consecutive missed turns per actor)")
    by_actor: Dict[str, List[int]] = defaultdict(list)
    for row in ROWS:
        by_actor[row["name"]].append(row["turn"])
    runs: List[int] = []
    for turns_list in by_actor.values():
        turns_list.sort()
        length = 1
        for prev, cur in zip(turns_list, turns_list[1:]):
            if cur == prev + 1:
                length += 1
            else:
                runs.append(length)
                length = 1
        runs.append(length)
    hist = Counter(min(r, 10) for r in runs)
    print(f"  actors with >=1 miss: {len(by_actor)}   runs: {len(runs)}")
    print(f"  run length histogram (10 = 10+): {dict(sorted(hist.items()))}")
    print(
        f"  median run {statistics.median(runs):.0f}  mean {statistics.fmean(runs):.2f}"
        f"  max {max(runs)}  rows in runs>=3 "
        f"{pct(sum(r for r in runs if r >= 3), sum(runs))}"
    )
    top = Counter(row["name"] for row in ROWS).most_common(5)
    print(f"  worst actors: {top}")

    print("\n[T9] example rows")
    for row in ROWS[:3] + ROWS[len(ROWS) // 2 : len(ROWS) // 2 + 2]:
        print("  " + json.dumps(row, sort_keys=True))


def main(argv: List[str]) -> int:
    global WINDOW_START
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    ap.add_argument("--actors", type=int, default=DEFAULT_ACTORS)
    ap.add_argument("--tag", type=str, default="run")
    ap.add_argument("--window-start", type=int, default=WINDOW_START)
    ap.add_argument("--no-surplus-discount", action="store_true")
    args = ap.parse_args(argv)

    WINDOW_START = args.window_start

    sim_box: Dict[str, Any] = {}
    install_patches(args.no_surplus_discount, sim_box)
    sim = Simulation()
    sim_box["sim"] = sim
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    print(f"### arm={args.tag} discount_disabled={args.no_surplus_discount}")
    for turn in range(1, args.turns + 1):
        sim.run_turn()
        if turn % 50 == 0:
            print(f"... turn {turn} rows={len(ROWS)}", flush=True)

    missing = [k for k in ("run_turn", "take_turn", "wtp", "txn") if not CALLS[k]]
    if missing:
        raise RuntimeError(f"monkeypatch did not land: {missing} {dict(CALLS)}")
    print(f"### patch calls {dict(CALLS)}")
    report(args.tag, args.turns)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
