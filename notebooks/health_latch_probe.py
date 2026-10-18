"""Tier-1b probe: why do actors stay latched with health debt and no medicine?

Classifies every regular actor with health debt >= 0.25 and zero
medicine/advanced_medicine on hand into a "why no medicine" bucket, by
replaying ActorBrain._drive_buy_commands' decision for the health drive at
sample turns.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.core import actor_brain as ab_mod
from spacesim2.core.actor import ActorType
from spacesim2.core.actor_brain import ActorBrain, BrainCache

DEBT_THRESHOLD = 0.25
BID_WINDOW = 10
MISS_WINDOW = 90

MEDICINE_IDS = ("medicine", "advanced_medicine")

# state
med_bids: dict[str, list[tuple[int, int, int]]] = defaultdict(
    list
)  # name -> (turn, qty, price)
diag: dict[tuple[int, str], dict] = {}
sample_turns: set[int] = set()
wrapper_calls = 0

_orig = ActorBrain._drive_buy_commands


def _replay_health(self, actor, market, cache):
    """Replay the budget loop, returning the health drive's decision record."""
    rec: dict[str, Any] = {"reached": False}
    lam = self._value_of_money(actor, market, cache)
    rec["lam"] = float(lam)
    if lam <= 0:
        rec["stop"] = "lam_zero"
        return rec
    available = actor.money
    for drive in self._drives_by_priority(actor):
        is_health = drive.metrics.get_name() == "health"
        mats = drive.materials()
        if not mats or not drive.can_purchase(actor):
            if is_health:
                rec.update(
                    reached=True,
                    available=available,
                    stop="no_mats_or_gate",
                    can_purchase=bool(drive.can_purchase(actor)),
                )
                return rec
            continue
        have = sum(actor.inventory.get_quantity(m) for m in mats)
        need = drive.target_units() - have
        if need <= 0:
            if is_health:
                rec.update(reached=True, available=available, stop="no_need")
                return rec
            continue
        target, ask = self._cheapest_material_ask(actor, market, mats, cache)
        wtp = self._drive_willingness_to_pay(actor, market, drive, target, lam, cache)
        if wtp <= 0:
            if is_health:
                rec.update(
                    reached=True, available=available, stop="wtp_zero", wtp=float(wtp)
                )
                return rec
            continue
        if ask is not None and ask <= wtp:
            bid = ask
        else:
            ref = self._drive_bid_reference(actor, market, target, cache)
            pressure = market.scarcity_pressure_for(target)
            bid = min(wtp, int(round(ref * (1.0 + pressure))))
        if is_health:
            qty = min(need, available // bid) if bid > 0 else 0
            rec.update(
                reached=True,
                available=available,
                money=actor.money,
                ask=(None if ask is None else int(ask)),
                wtp=float(wtp),
                bid=int(bid),
                need=int(need),
                qty=int(qty),
                stop=(
                    "bid_nonpositive"
                    if bid <= 0
                    else ("placed" if qty > 0 else "no_budget")
                ),
            )
            return rec
        if bid <= 0:
            continue
        qty = min(need, available // bid)
        if qty > 0:
            available -= qty * bid
    if not rec["reached"]:
        rec["stop"] = "health_drive_absent"
    return rec


def _wrapped(self, actor, market, cache=None):
    global wrapper_calls
    wrapper_calls += 1
    cmds = _orig(self, actor, market, cache)
    for c in cmds:
        if (
            c.__class__ is ab_mod.PlaceBuyOrderCommand
            and c.commodity_type.id in MEDICINE_IDS
        ):
            med_bids[actor.name].append((market.current_turn, c.quantity, c.price))
    if market.current_turn in sample_turns and actor.actor_type is ActorType.REGULAR:
        hd = next((d for d in actor.drives if d.metrics.get_name() == "health"), None)
        if hd is not None and hd.metrics.debt >= DEBT_THRESHOLD:
            reg = actor.sim.commodity_registry
            held = sum(
                actor.inventory.get_quantity(reg.get_commodity(i))
                for i in MEDICINE_IDS
                if reg.get_commodity(i)
            )
            if held == 0:
                diag[(market.current_turn, actor.name)] = _replay_health(
                    self, actor, market, BrainCache()
                )
    return cmds


ActorBrain._drive_buy_commands = _wrapped


def run(turns: int, planets: int, actors: int, samples: list[int]) -> dict:
    sample_turns.update(samples)
    sim = create_and_setup_simulation(planets=planets, actors=actors, makers=2)
    reg = sim.commodity_registry
    med = reg.get_commodity("medicine")
    adv = reg.get_commodity("advanced_medicine")
    if med is None:
        raise RuntimeError("medicine commodity missing")

    med_procs = {
        p.id
        for p in sim.process_registry.all_processes()
        if any(c.id == "medicine" for c in p.outputs)
    }

    prev_debt: dict[str, float] = {}
    latch_start: dict[str, int] = {}
    last_miss: dict[str, int] = {}
    ever_listed: dict[str, bool] = defaultdict(bool)
    price_series: dict[int, dict] = {}
    out_samples: dict[int, Any] = {}

    regulars = [a for a in sim.actors if a.actor_type is ActorType.REGULAR]

    for _ in range(turns):
        sim.run_turn()
        t = sim.current_turn
        for p in sim.planets:
            if p.market.get_ask_levels(med):
                ever_listed[p.name] = True
        for a in regulars:
            hd = next((d for d in a.drives if d.metrics.get_name() == "health"), None)
            if hd is None:
                continue
            d = hd.metrics.debt
            pd = prev_debt.get(a.name, 0.0)
            if d > pd + 1e-9:
                last_miss[a.name] = t
            if d >= DEBT_THRESHOLD and pd < DEBT_THRESHOLD:
                latch_start[a.name] = t
            if d < DEBT_THRESHOLD:
                latch_start.pop(a.name, None)
            prev_debt[a.name] = d

        if t in (50, 100, 150, 200) or t == turns:
            prices = [
                p.market.get_avg_price(med)
                for p in sim.planets
                if p.market.has_price_signal(med)
            ]
            producers = sum(
                1
                for a in sim.actors
                if getattr(a.brain, "chosen_recipe_id", None) in med_procs
            )
            price_series[t] = {
                "planets_with_signal": len(prices),
                "mean_price": round(sum(prices) / len(prices), 1) if prices else None,
                "producers": producers,
                "planets_with_ask": sum(
                    1 for p in sim.planets if p.market.get_ask_levels(med)
                ),
                "planets_ever_listed": sum(
                    1 for p in sim.planets if ever_listed[p.name]
                ),
            }

        if t in sample_turns:
            buckets: Counter[str] = Counter()
            rows = []
            stale = 0
            latched = 0
            for a in regulars:
                hd = next(
                    (d for d in a.drives if d.metrics.get_name() == "health"), None
                )
                if hd is None or hd.metrics.debt < DEBT_THRESHOLD:
                    continue
                held = a.inventory.get_quantity(med) + (
                    a.inventory.get_quantity(adv) if adv else 0
                )
                if held > 0:
                    continue
                latched += 1
                mk = a.planet.market
                asks = mk.get_ask_levels(med)
                best_ask = asks[0][0] if asks else None
                depth = sum(q for _, q in asks)
                vol30 = mk.get_30_day_average_volume(med) * 30
                prod = sum(
                    1
                    for o in a.planet.actors
                    if getattr(o.brain, "chosen_recipe_id", None) in med_procs
                )
                bids = [b for b in med_bids[a.name] if b[0] > t - BID_WINDOW]
                rec = diag.get((t, a.name), {})
                miss_t = last_miss.get(a.name, -(10**6))
                if t - miss_t > MISS_WINDOW:
                    stale += 1

                if bids:
                    last_bid_price = max(b[2] for b in bids)
                    if best_ask is None:
                        bucket = "bid_placed_no_local_ask"
                    elif last_bid_price < best_ask:
                        bucket = "bid_below_ask"
                    else:
                        bucket = "bid_at_or_above_ask_unfilled"
                else:
                    stop = rec.get("stop", "not_sampled")
                    if stop == "no_budget":
                        avail = rec.get("available", 0)
                        money = rec.get("money", a.money)
                        bucket = (
                            "no_bid_broke"
                            if money < rec.get("bid", 1)
                            else "no_bid_budget_taken_by_other_needs"
                        )
                    elif stop == "wtp_zero":
                        bucket = "no_bid_wtp_zero"
                    elif stop in ("no_mats_or_gate",):
                        bucket = "no_bid_gate"
                    elif stop == "placed":
                        bucket = "bid_this_turn_only"
                    else:
                        bucket = f"no_bid_{stop}"
                    if not ever_listed[a.planet.name] and bucket.startswith("no_bid"):
                        bucket = "no_supply_ever_on_planet"
                buckets[bucket] += 1
                rows.append(
                    {
                        "last_bid": (max(b[2] for b in bids) if bids else None),
                        "wtp": rec.get("wtp"),
                        "actor": a.name,
                        "planet": a.planet.name,
                        "turns_latched": t - latch_start.get(a.name, t),
                        "turns_since_miss": (t - miss_t) if miss_t > -(10**5) else None,
                        "money": a.money,
                        "bucket": bucket,
                        "best_ask": best_ask,
                        "ask_depth": depth,
                        "vol30": round(vol30, 1),
                        "producers_on_planet": prod,
                        "ever_listed": ever_listed[a.planet.name],
                        "diag": rec,
                    }
                )

            def _med(vals):
                v = sorted(x for x in vals if x is not None)
                return round(v[len(v) // 2], 1) if v else None

            bstats = {}
            for b in buckets:
                rs = [r for r in rows if r["bucket"] == b]
                bstats[b] = {
                    "n": len(rs),
                    "med_money": _med(r["money"] for r in rs),
                    "med_bid": _med(r["last_bid"] for r in rs),
                    "med_wtp": _med(r["wtp"] for r in rs),
                    "med_best_ask": _med(r["best_ask"] for r in rs),
                    "med_turns_latched": _med(r["turns_latched"] for r in rs),
                    "med_turns_since_miss": _med(r["turns_since_miss"] for r in rs),
                    "share_stale90": round(
                        sum(
                            1
                            for r in rs
                            if r["turns_since_miss"] is None
                            or r["turns_since_miss"] > MISS_WINDOW
                        )
                        / max(1, len(rs)),
                        2,
                    ),
                    "med_producers": _med(r["producers_on_planet"] for r in rs),
                }
            out_samples[t] = {
                "bucket_stats": bstats,
                "n_regular": len(regulars),
                "n_latched": latched,
                "stale_no_miss_90": stale,
                "buckets": dict(buckets),
                "median_turns_latched": (
                    sorted(r["turns_latched"] for r in rows)[len(rows) // 2]
                    if rows
                    else None
                ),
                "examples": rows[:3],
            }
            print(
                f"turn {t}: latched={latched}/{len(regulars)} {dict(buckets)}",
                flush=True,
            )

    return {
        "params": {
            "turns": turns,
            "planets": planets,
            "actors": actors,
            "wrapper_calls": wrapper_calls,
        },
        "price_series": price_series,
        "samples": {str(k): v for k, v in out_samples.items()},
    }


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=200)
    ap.add_argument("--planets", type=int, default=12)
    ap.add_argument("--actors", type=int, default=100)
    ap.add_argument("--samples", type=str, default="100,150,200")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    res = run(a.turns, a.planets, a.actors, [int(x) for x in a.samples.split(",")])
    if res["params"]["wrapper_calls"] == 0:
        raise RuntimeError("monkeypatch never fired")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res["price_series"], indent=1))
    for t, s in res["samples"].items():
        print(
            f"\n== turn {t}: latched {s['n_latched']}/{s['n_regular']}, "
            f"stale(no miss in 90) {s['stale_no_miss_90']}, "
            f"median turns latched {s['median_turns_latched']}"
        )
        tot = sum(s["buckets"].values()) or 1
        for b, c in sorted(s["buckets"].items(), key=lambda kv: -kv[1]):
            print(f"   {b:>42} {c:>5} {100 * c / tot:>4.0f}%")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
