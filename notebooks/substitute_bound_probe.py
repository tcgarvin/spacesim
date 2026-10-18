"""Does the processed-food substitute bound need to generalize to shelter and health?

In-process Tier-1b probe, one arm per invocation. The "before" arm disables the
``ProsperityCategory`` food bound (``ProsperityDrive.max_numeraire_multiple``
returns inf) so both arms run from the same working tree.

    uv run python notebooks/substitute_bound_probe.py --tag after
    uv run python notebooks/substitute_bound_probe.py --tag before --no-food-bound

Window defaults to turns 301-450. Records, per missed shelter/health maintenance
event: brain type, planet resource bucket, money, wood/tools/biomass on hand,
the best local ask, the misser's own bid and its WTP decomposition, and the
standing consumer vs industrial bids. Records the whole-window auction for
simple_building_materials and medicine split by buyer class (drive consumer,
recipe input, facility build, dealer/ship), the netback ceilings industrialists
compute for those inputs, and per-planet price and imputed-make-cost ratios of
each upgraded good to its basic good.

Patches, all call through, all asserted to have fired: ``Simulation.run_turn``
(turn counter), ``ShelterDrive.tick`` / ``HealthDrive.tick`` (event detection by
recording the drive's own random draw), ``Actor.take_turn`` (miss rows),
``ActorBrain._drive_willingness_to_pay`` (WTP decomposition),
``ActorBrain._drive_buy_commands`` and ``IndustrialistBrain._buy_command`` (bid
provenance), ``IndustrialistBrain._input_price_ceiling`` (netback ceilings),
``PlaceBuyOrderCommand.execute`` (bids placed), ``Market._execute_transaction``
(volume by buyer class).
"""

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import ActorBrain
from spacesim2.core.brains.industrialist import IndustrialistBrain
from spacesim2.core.commands import PlaceBuyOrderCommand
from spacesim2.core.drives.health_drive import HealthDrive
from spacesim2.core.drives.prosperity_drive import ProsperityDrive
from spacesim2.core.drives.shelter_drive import ShelterDrive
from spacesim2.core.market import Market
from spacesim2.core.simulation import Simulation

DEFAULT_TURNS = 450
DEFAULT_PLANETS = 12
DEFAULT_ACTORS = 100
WINDOW_START = 301
SAMPLE_EVERY = 25

BM = "simple_building_materials"
PREFAB = "prefab_housing"
MED = "medicine"
ADVMED = "advanced_medicine"
TRACKED = (BM, MED)

STATE: Dict[str, Any] = {"turn": 0}
CALLS: Counter = Counter()
SHELTER_ROWS: List[Dict[str, Any]] = []
HEALTH_ROWS: List[Dict[str, Any]] = []
EXPOSURE: Counter = Counter()  # (drive, brain, bucket) -> events in window
MISSES: Counter = Counter()
PENDING: Dict[str, Dict[str, bool]] = defaultdict(dict)
TURN_BIDS: Dict[str, List[Tuple[str, int, int, str]]] = defaultdict(list)
WTP: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
BID_SRC: Dict[Tuple[str, str], str] = {}
CEIL_CTX: Dict[Tuple[str, str], str] = {}
# commodity -> buyer class -> units / credits, over the whole window
FLOW_UNITS: Dict[str, Counter] = defaultdict(Counter)
FLOW_CREDITS: Dict[str, Counter] = defaultdict(Counter)
# (commodity, kind) -> list of ceilings
CEILINGS: Dict[Tuple[str, str], List[float]] = defaultdict(list)
PRICE_ROWS: List[Dict[str, Any]] = []


def bucket(value: float) -> str:
    if value < 0.4:
        return "lo<0.4"
    if value <= 0.7:
        return "mid"
    return "hi>0.7"


def brain_tag(actor: Any) -> str:
    return "I" if isinstance(actor.brain, IndustrialistBrain) else "C"


def fallback_class(sim: Simulation, buyer: Any, commodity_id: str) -> str:
    recipe = getattr(buyer.brain, "chosen_recipe_id", None)
    if recipe:
        process = sim.process_registry.get_process(recipe)
        if process is not None:
            for commodity, _qty in process.inputs.items():
                if commodity.id == commodity_id:
                    return f"input:{recipe}"
    return "consumer?"


def buyer_class(sim: Simulation, buyer: Any, commodity_id: str) -> str:
    if getattr(buyer, "actor_type", None) is not ActorType.REGULAR:
        return "dealer_or_ship"
    src = BID_SRC.get((buyer.name, commodity_id))
    if src:
        return src
    return fallback_class(sim, buyer, commodity_id)


def head_of(klass: str) -> str:
    if klass.startswith("input:"):
        return klass
    if klass.startswith("build:"):
        return "build:facility"
    if klass.startswith("consumer"):
        return "consumer"
    return klass


def _market_bids(sim: Simulation, market: Any, commodity: Any) -> Tuple[List[int], ...]:
    cons: List[int] = []
    ind: List[int] = []
    for order in market.buy_orders.get(commodity, []):
        if order.cancelled:
            continue
        klass = buyer_class(sim, order.actor, commodity.id)
        if klass.startswith("consumer") or klass == "drive":
            cons.append(int(order.price))
        elif klass.startswith(("input:", "build:")):
            ind.append(int(order.price))
    return cons, ind


def install_patches(disable_food_bound: bool, sim_box: Dict[str, Any]) -> None:
    orig_run_turn = Simulation.run_turn
    orig_take_turn = Actor.take_turn
    orig_shelter_tick = ShelterDrive.tick
    orig_health_tick = HealthDrive.tick
    orig_wtp = ActorBrain._drive_willingness_to_pay
    orig_drive_buys = ActorBrain._drive_buy_commands
    orig_ind_buy = IndustrialistBrain._buy_command
    orig_ceiling = IndustrialistBrain._input_price_ceiling
    orig_place_buy = PlaceBuyOrderCommand.execute
    orig_txn = Market._execute_transaction

    def run_turn(self: Simulation) -> None:
        STATE["turn"] = self.current_turn + 1
        TURN_BIDS.clear()
        WTP.clear()
        BID_SRC.clear()
        CEIL_CTX.clear()
        PENDING.clear()
        CALLS["run_turn"] += 1
        orig_run_turn(self)

    def event_drawn(fn: Any, drive: Any, actor: Any, prob: float) -> Tuple[Any, bool]:
        """Call ``fn`` while recording the drive's first random draw."""
        draws: List[float] = []
        real_random = random.random

        def recorder() -> float:
            value = real_random()
            draws.append(value)
            return value

        random.random = recorder  # type: ignore[assignment]
        try:
            result = fn(drive, actor)
        finally:
            random.random = real_random  # type: ignore[assignment]
        return result, bool(draws) and draws[0] < prob

    def shelter_tick(self: ShelterDrive, actor: Any) -> Any:
        from spacesim2.core.drives import shelter_drive as sd

        stock = actor.inventory.get_available_quantity(self.building_materials)
        quality = (
            actor.inventory.get_available_quantity(self.quality_materials)
            if self.quality_materials
            else 0
        )
        result, event = event_drawn(orig_shelter_tick, self, actor, sd.BASE_EVENT_PROB)
        CALLS["shelter_tick"] += 1
        if event and STATE["turn"] >= WINDOW_START:
            PENDING[actor.name]["shelter_event"] = True
            PENDING[actor.name]["shelter_miss"] = (stock + quality) == 0
            PENDING[actor.name]["shelter_stock"] = stock
        return result

    def health_tick(self: HealthDrive, actor: Any) -> Any:
        from spacesim2.core.drives import health_drive as hd

        stock = (
            actor.inventory.get_available_quantity(self.medicine)
            if self.medicine
            else 0
        )
        quality = (
            actor.inventory.get_available_quantity(self.quality_medicine)
            if self.quality_medicine
            else 0
        )
        result, event = event_drawn(orig_health_tick, self, actor, hd.BASE_EVENT_PROB)
        CALLS["health_tick"] += 1
        if event and STATE["turn"] >= WINDOW_START:
            PENDING[actor.name]["health_event"] = True
            PENDING[actor.name]["health_miss"] = (stock + quality) == 0
            PENDING[actor.name]["health_stock"] = stock
        return result

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
        if commodity.id in TRACKED and STATE["turn"] >= WINDOW_START:
            welfare = drive.marginal_welfare() / lam if lam > 0 else 0.0
            replacement = self._replacement_cost(actor, market, commodity, cache)
            capped = (
                -1.0
                if replacement is None
                else replacement * (1.0 + drive.metrics.debt)
            )
            WTP[(actor.name, commodity.id)] = (
                float(value),
                float(welfare),
                float(capped),
            )
        return value

    def drive_buys(self: ActorBrain, actor: Any, market: Any, cache: Any = None) -> Any:
        cmds = orig_drive_buys(self, actor, market, cache)
        for cmd in cmds:
            cmd._probe_src = "consumer"
        return cmds

    def ceiling_fn(
        self: IndustrialistBrain,
        actor: Any,
        market: Any,
        commodity: Any,
        quantity_per_run: float,
        output_value: float,
        recipe_cost: float,
        memo: Any,
        max_cost_multiple: float = math.inf,
    ) -> float:
        value = orig_ceiling(
            self,
            actor,
            market,
            commodity,
            quantity_per_run,
            output_value,
            recipe_cost,
            memo,
            max_cost_multiple,
        )
        CALLS["ceiling"] += 1
        recipe = self.chosen_recipe_id or "-"
        kind = "build" if math.isfinite(max_cost_multiple) else "input"
        tag = f"{kind}:{recipe}"
        CEIL_CTX[(actor.name, commodity.id)] = tag
        if commodity.id in TRACKED and STATE["turn"] >= WINDOW_START:
            if math.isfinite(value):
                CEILINGS[(commodity.id, tag)].append(float(value))
            else:
                CEILINGS[(commodity.id, tag + "|inf")].append(float("nan"))
        return value

    def ind_buy(self: IndustrialistBrain, *a: Any, **kw: Any) -> Any:
        cmds = orig_ind_buy(self, *a, **kw)
        actor = a[0] if a else kw["actor"]
        for cmd in cmds:
            cmd._probe_src = CEIL_CTX.get(
                (actor.name, cmd.commodity_type.id), "input:?"
            )
        return cmds

    def place_buy(self: PlaceBuyOrderCommand, actor: Any) -> bool:
        ok = orig_place_buy(self, actor)
        CALLS["place_buy"] += 1
        if ok:
            src = getattr(self, "_probe_src", "other")
            TURN_BIDS[actor.name].append(
                (self.commodity_type.id, int(self.price), int(self.quantity), src)
            )
            BID_SRC[(actor.name, self.commodity_type.id)] = src
        return ok

    def take_turn(self: Actor) -> None:
        turn = STATE["turn"]
        if turn < WINDOW_START or self.actor_type is not ActorType.REGULAR:
            orig_take_turn(self)
            return
        CALLS["take_turn"] += 1
        sim = self.sim
        planet = self.planet
        reg = sim.commodity_registry
        wood_c = reg.get_commodity("wood")
        tools_c = reg.get_commodity("simple_tools")
        bio_c = reg.get_commodity("biomass")
        chem_c = reg.get_commodity("refined_chemicals")
        money_before = float(self.money)
        wood = self.inventory.get_available_quantity(wood_c) if wood_c else 0
        tools = self.inventory.get_available_quantity(tools_c) if tools_c else 0
        bio = self.inventory.get_available_quantity(bio_c) if bio_c else 0
        chem = self.inventory.get_available_quantity(chem_c) if chem_c else 0

        orig_take_turn(self)

        flags = PENDING.get(self.name, {})
        tag = brain_tag(self)
        market = planet.market
        for drive_name, commodity_id, attr in (
            ("shelter", BM, planet.attributes.wood),
            ("health", MED, planet.attributes.biomass),
        ):
            if not flags.get(f"{drive_name}_event"):
                continue
            bkt = bucket(attr)
            EXPOSURE[(drive_name, tag, bkt)] += 1
            if not flags.get(f"{drive_name}_miss"):
                continue
            MISSES[(drive_name, tag, bkt)] += 1
            commodity = reg.get_commodity(commodity_id)
            best_bid, best_ask = market.get_bid_ask_spread(commodity)
            cons_bids, ind_bids = _market_bids(sim, market, commodity)
            own = [b for b in TURN_BIDS.get(self.name, []) if b[0] == commodity_id]
            wtp_row = WTP.get((self.name, commodity_id), (-1.0, -1.0, -1.0))
            row = {
                "turn": turn,
                "name": self.name,
                "brain": tag,
                "planet": planet.name,
                "market_id": id(market),
                "attr": round(attr, 2),
                "bucket": bkt,
                "money": round(money_before),
                "wood": wood,
                "tools": tools,
                "biomass": bio,
                "refined_chem": chem,
                "recipe": getattr(self.brain, "chosen_recipe_id", None) or "-",
                "best_ask": int(best_ask) if best_ask is not None else -1,
                "best_bid": int(best_bid) if best_bid is not None else -1,
                "own_bid": max((b[1] for b in own), default=-1),
                "cons_bid_med": round(statistics.median(cons_bids))
                if cons_bids
                else -1,
                "ind_bid_med": round(statistics.median(ind_bids)) if ind_bids else -1,
                "ind_bidders": len(ind_bids),
                "wtp": round(wtp_row[0], 1),
                "welfare_wtp": round(wtp_row[1], 1),
                "repl_cap": round(wtp_row[2], 1),
            }
            (SHELTER_ROWS if drive_name == "shelter" else HEALTH_ROWS).append(row)

    def execute_transaction(
        self: Market, buyer: Any, seller: Any, commodity_type: Any, *a: Any, **kw: Any
    ) -> Any:
        result = orig_txn(self, buyer, seller, commodity_type, *a, **kw)
        CALLS["txn"] += 1
        if STATE["turn"] >= WINDOW_START and commodity_type.id in (
            BM,
            MED,
            PREFAB,
            ADVMED,
        ):
            txn = self.transaction_history[-1]
            head = head_of(buyer_class(sim_box["sim"], buyer, commodity_type.id))
            FLOW_UNITS[commodity_type.id][head] += txn.quantity
            FLOW_CREDITS[commodity_type.id][head] += txn.total_amount
        return result

    Simulation.run_turn = run_turn  # type: ignore[method-assign]
    Actor.take_turn = take_turn  # type: ignore[method-assign]
    ShelterDrive.tick = shelter_tick  # type: ignore[method-assign]
    HealthDrive.tick = health_tick  # type: ignore[method-assign]
    ActorBrain._drive_willingness_to_pay = wtp  # type: ignore[method-assign]
    ActorBrain._drive_buy_commands = drive_buys  # type: ignore[method-assign]
    IndustrialistBrain._buy_command = ind_buy  # type: ignore[method-assign]
    IndustrialistBrain._input_price_ceiling = ceiling_fn  # type: ignore[method-assign]
    PlaceBuyOrderCommand.execute = place_buy  # type: ignore[method-assign]
    Market._execute_transaction = execute_transaction  # type: ignore[method-assign]

    if disable_food_bound:
        ProsperityDrive.max_numeraire_multiple = (  # type: ignore[method-assign]
            lambda self: math.inf
        )


def sample_prices(sim: Simulation, turn: int) -> None:
    """Per-planet prices and imputed make costs for the four tracked goods."""
    reg = sim.commodity_registry
    goods = {k: reg.get_commodity(k) for k in (BM, PREFAB, MED, ADVMED, "food")}
    if any(c is None for c in goods.values()):
        return
    for planet in sim.planets:
        market = planet.market
        row: Dict[str, Any] = {"turn": turn, "planet": planet.name}
        for key, commodity in goods.items():
            row[f"px_{key}"] = (
                market.get_avg_price(commodity)
                if market.has_price_signal(commodity)
                else -1
            )
        probe_actor = next(
            (
                a
                for a in planet.actors
                if a.actor_type is ActorType.REGULAR
                and isinstance(a.brain, IndustrialistBrain)
            ),
            None,
        )
        if probe_actor is not None:
            memo: Dict[str, float] = {}
            for key, commodity in goods.items():
                cost = probe_actor.brain._imputed_unit_cost(
                    probe_actor, market, commodity, 0, frozenset(), memo, make_only=True
                )
                row[f"mk_{key}"] = -1.0 if math.isinf(cost) else round(float(cost), 1)
        PRICE_ROWS.append(row)


def pct(numerator: int, denominator: int) -> str:
    return f"{100.0 * numerator / denominator:.1f}%" if denominator else "-"


def med(rows: List[Dict[str, Any]], field: str) -> float:
    values = [r[field] for r in rows if r[field] >= 0]
    return statistics.median(values) if values else -1.0


def quantiles(values: List[float]) -> str:
    if not values:
        return "n=0"
    values = sorted(values)
    n = len(values)

    def q(f: float) -> float:
        return values[min(n - 1, int(f * n))]

    return (
        f"n={n:<5} p25 {q(0.25):>7.1f}  med {q(0.5):>7.1f}  "
        f"p75 {q(0.75):>7.1f}  max {values[-1]:>7.1f}"
    )


def miss_tables(label: str, rows: List[Dict[str, Any]], drive: str) -> None:
    print(f"\n===== {label} =====")
    print(f"\n[{drive}-1] miss rate by brain x planet-attr bucket (misses / events)")
    print(f"{'brain':<6}{'bucket':<9}{'misses':>8}{'events':>8}{'rate':>8}")
    keys = [k for k in sorted(EXPOSURE) if k[0] == drive]
    for key in keys:
        print(
            f"{key[1]:<6}{key[2]:<9}{MISSES[key]:>8}{EXPOSURE[key]:>8}"
            f"{pct(MISSES[key], EXPOSURE[key]):>8}"
        )
    tm = sum(MISSES[k] for k in keys)
    te = sum(EXPOSURE[k] for k in keys)
    print(f"{'ALL':<6}{'':<9}{tm:>8}{te:>8}{pct(tm, te):>8}")
    if not rows:
        print("  (no miss rows)")
        return

    print(f"\n[{drive}-2] market state at the miss (medians)")
    print(
        f"{'group':<12}{'rows':>6}{'money':>8}{'wood':>6}{'tools':>6}{'chem':>6}"
        f"{'ask':>7}{'own_bid':>9}{'cons':>7}{'ind':>7}{'nInd':>6}"
        f"{'wtp':>7}{'welf':>7}{'cap':>7}"
    )
    groups: List[Tuple[str, List[Dict[str, Any]]]] = [("all", rows)]
    for b in ("C", "I"):
        for bk in ("lo<0.4", "mid", "hi>0.7"):
            subset = [r for r in rows if r["brain"] == b and r["bucket"] == bk]
            if subset:
                groups.append((f"{b}/{bk}", subset))
    for name, sub in groups:
        print(
            f"{name:<12}{len(sub):>6}{med(sub, 'money'):>8.0f}{med(sub, 'wood'):>6.0f}"
            f"{med(sub, 'tools'):>6.0f}{med(sub, 'refined_chem'):>6.0f}"
            f"{med(sub, 'best_ask'):>7.0f}{med(sub, 'own_bid'):>9.0f}"
            f"{med(sub, 'cons_bid_med'):>7.0f}{med(sub, 'ind_bid_med'):>7.0f}"
            f"{statistics.median([r['ind_bidders'] for r in sub]):>6.0f}"
            f"{med(sub, 'wtp'):>7.0f}{med(sub, 'welfare_wtp'):>7.0f}"
            f"{med(sub, 'repl_cap'):>7.0f}"
        )

    print(f"\n[{drive}-3] why no stock at the event (row shares)")
    no_ask = [r for r in rows if r["best_ask"] < 0]
    ask_rows = [r for r in rows if r["best_ask"] >= 0]
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
    ind_above = [
        r for r in rows if r["ind_bid_med"] >= 0 and r["ind_bid_med"] > r["own_bid"]
    ]
    for name, subset in (
        ("no local ask", no_ask),
        ("ask, broke", broke),
        ("ask, solvent", solvent),
        ("  ..bid < ask", outbid),
        ("  ..no bid", no_bid),
        ("  ..bid >= ask", bid_ok),
        ("  ..repl cap binds", cap_binds),
        ("ind bid > own bid", ind_above),
    ):
        print(f"  {name:<20}{len(subset):>6}{pct(len(subset), len(rows)):>8}")

    print(f"\n[{drive}-4] example rows")
    for row in rows[:2] + rows[len(rows) // 2 : len(rows) // 2 + 1]:
        print("  " + json.dumps(row, sort_keys=True))


def auction_table(commodity_id: str) -> None:
    units = FLOW_UNITS[commodity_id]
    credits = FLOW_CREDITS[commodity_id]
    total = sum(units.values())
    print(f"\n[auction] {commodity_id}: {total} units in window")
    print(f"  {'buyer class':<38}{'units':>8}{'share':>8}{'mean_px':>9}")
    for head, qty in units.most_common():
        mean_px = credits[head] / qty if qty else 0.0
        print(f"  {head:<38}{qty:>8}{pct(qty, total):>8}{mean_px:>9.1f}")


def ceiling_table() -> None:
    print("\n[ceilings] netback input ceiling by commodity and recipe (finite only)")
    print(f"  {'commodity / kind:recipe':<52}{'n':>7}{'med':>8}{'p90':>8}")
    for key in sorted(CEILINGS):
        values = [v for v in CEILINGS[key] if not math.isnan(v)]
        label = f"{key[0]} / {key[1]}"
        if not values:
            print(f"  {label:<52}{len(CEILINGS[key]):>7}{'inf':>8}{'inf':>8}")
            continue
        values.sort()
        p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
        print(
            f"  {label:<52}{len(values):>7}{statistics.median(values):>8.1f}{p90:>8.1f}"
        )


def ratio_table() -> None:
    print("\n[ratios] per planet-sample, upgraded good vs its basic good")
    pairs = (
        ("prefab/bm price", "px_prefab_housing", "px_simple_building_materials"),
        ("prefab/bm make", "mk_prefab_housing", "mk_simple_building_materials"),
        ("advmed/med price", "px_advanced_medicine", "px_medicine"),
        ("advmed/med make", "mk_advanced_medicine", "mk_medicine"),
        ("prefab/food price", "px_prefab_housing", "px_food"),
        ("bm/food price", "px_simple_building_materials", "px_food"),
        ("advmed/food price", "px_advanced_medicine", "px_food"),
        ("med/food price", "px_medicine", "px_food"),
    )
    for label, num, den in pairs:
        values = [
            r[num] / r[den]
            for r in PRICE_ROWS
            if r.get(num, -1) > 0 and r.get(den, -1) > 0
        ]
        print(f"  {label:<20}{quantiles(values)}")
    print("\n[levels] per planet-sample price medians")
    for key in (
        "px_food",
        "px_simple_building_materials",
        "px_prefab_housing",
        "px_medicine",
        "px_advanced_medicine",
        "mk_simple_building_materials",
        "mk_prefab_housing",
        "mk_medicine",
        "mk_advanced_medicine",
    ):
        values = [float(r[key]) for r in PRICE_ROWS if r.get(key, -1) > 0]
        print(f"  {key:<32}{quantiles(values)}")


def main(argv: List[str]) -> int:
    global WINDOW_START
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--planets", type=int, default=DEFAULT_PLANETS)
    ap.add_argument("--actors", type=int, default=DEFAULT_ACTORS)
    ap.add_argument("--tag", type=str, default="run")
    ap.add_argument("--window-start", type=int, default=WINDOW_START)
    ap.add_argument("--no-food-bound", action="store_true")
    args = ap.parse_args(argv)

    WINDOW_START = args.window_start

    sim_box: Dict[str, Any] = {}
    install_patches(args.no_food_bound, sim_box)
    sim = Simulation()
    sim_box["sim"] = sim
    sim.setup_simple(
        num_planets=args.planets,
        num_regular_actors=args.actors,
        num_market_makers=2,
        num_ships=1,
    )
    print(f"### arm={args.tag} food_bound_disabled={args.no_food_bound}")
    for turn in range(1, args.turns + 1):
        sim.run_turn()
        if turn >= WINDOW_START and turn % SAMPLE_EVERY == 0:
            sample_prices(sim, turn)
        if turn % 50 == 0:
            print(
                f"... turn {turn} shelter_rows={len(SHELTER_ROWS)} "
                f"health_rows={len(HEALTH_ROWS)}",
                flush=True,
            )

    missing = [
        k
        for k in ("run_turn", "take_turn", "shelter_tick", "health_tick", "wtp", "txn")
        if not CALLS[k]
    ]
    if missing:
        raise RuntimeError(f"monkeypatch did not land: {missing} {dict(CALLS)}")
    print(f"\n### arm={args.tag} patch calls {dict(CALLS)}")
    print(f"### window {WINDOW_START}-{args.turns} planets={args.planets}")
    miss_tables("SHELTER (simple_building_materials)", SHELTER_ROWS, "shelter")
    miss_tables("HEALTH (medicine)", HEALTH_ROWS, "health")
    print("\n===== AUCTIONS =====")
    for commodity_id in (BM, PREFAB, MED, ADVMED):
        auction_table(commodity_id)
    ceiling_table()
    print("\n===== PRICE / COST RATIOS =====")
    ratio_table()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
