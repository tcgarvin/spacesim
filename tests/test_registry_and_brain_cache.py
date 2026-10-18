"""Registry caches and the per-actor-turn BrainCache invalidation rules.

Registry caches cover all_commodities, all_processes, and the producer index.
"""

import math
from pathlib import Path
from unittest.mock import Mock

import yaml

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.actor_brain import (
    GOVERNMENT_WAGE,
    TOOL_EXPECTED_LIFESPAN,
    ActorBrain,
    _get_bid_ask,
)
from spacesim2.core.brains.colonist import ColonistBrain
from spacesim2.core.commands import PlaceSellOrderCommand
from spacesim2.core.commodity import CommodityDefinition, CommodityRegistry
from spacesim2.core.market import Market
from spacesim2.core.process import ProcessRegistry

from .helpers import get_actor

DATA_DIR = Path(__file__).parent.parent / "data"


def _load_real_registries() -> tuple[CommodityRegistry, ProcessRegistry]:
    commodity_registry = CommodityRegistry()
    commodity_registry.load_from_file(DATA_DIR / "commodities.yaml")
    process_registry = ProcessRegistry(commodity_registry)
    process_registry.load_from_file(DATA_DIR / "processes.yaml")
    return commodity_registry, process_registry


class TestProcessRegistryProducerIndex:
    def test_index_matches_brute_force_scan_for_every_commodity(self) -> None:
        """The producer index agrees with a full-registry scan."""
        commodity_registry, process_registry = _load_real_registries()
        all_processes = process_registry.all_processes()
        assert all_processes, "real process data should not be empty"

        for commodity in commodity_registry.all_commodities():
            brute_force = [p for p in all_processes if commodity in p.outputs]
            indexed = process_registry.get_processes_producing(commodity)
            assert {p.id for p in indexed} == {p.id for p in brute_force}

    def test_unknown_commodity_has_no_producers(self) -> None:
        _, process_registry = _load_real_registries()
        stranger = CommodityDefinition(
            id="never_registered", name="X", transportable=True, description=""
        )
        assert process_registry.get_processes_producing(stranger) == []


class TestRegistryListCaches:
    def test_all_commodities_reflects_late_registration(self) -> None:
        registry = CommodityRegistry()
        first = CommodityDefinition(
            id="a", name="A", transportable=True, description=""
        )
        registry.add_commodity(first)
        assert registry.all_commodities() == [first]

        late = CommodityDefinition(id="b", name="B", transportable=True, description="")
        registry.add_commodity(late)
        assert late in registry.all_commodities()
        assert len(registry.all_commodities()) == 2

    def test_all_processes_and_index_reflect_late_load(self, tmp_path: Path) -> None:
        commodity_registry, process_registry = _load_real_registries()
        before = list(process_registry.all_processes())
        wood = commodity_registry.get_commodity("wood")
        assert wood is not None
        producers_before = {
            p.id for p in process_registry.get_processes_producing(wood)
        }

        extra = tmp_path / "extra_processes.yaml"
        extra.write_text(
            yaml.dump(
                [
                    {
                        "id": "test_late_wood_process",
                        "name": "Late Wood Process",
                        "inputs": {},
                        "outputs": {"wood": 1},
                        "tools_required": [],
                        "facilities_required": [],
                        "labor": 1,
                        "description": "test",
                    }
                ]
            )
        )
        process_registry.load_from_file(extra)

        after_ids = {p.id for p in process_registry.all_processes()}
        assert after_ids == {p.id for p in before} | {"test_late_wood_process"}
        producers_after = {p.id for p in process_registry.get_processes_producing(wood)}
        assert producers_after == producers_before | {"test_late_wood_process"}


class TestBrainCacheInvalidation:
    """One BrainCache per brain, refreshed each decide_* entry.

    Market-derived entries live for a whole sim turn. Actor-state entries are
    also dropped when inventory or skills change.
    """

    def _actor_and_brain(self) -> tuple[Actor, ColonistBrain, Mock]:
        sim = Mock()
        sim.current_turn = 0
        brain = ColonistBrain()
        actor = Actor(
            name="CacheTester",
            sim=sim,
            actor_type=ActorType.REGULAR,
            drives=[],
            brain=brain,
        )
        return actor, brain, sim

    def test_market_entries_survive_within_a_turn_but_not_across_turns(self) -> None:
        actor, brain, sim = self._actor_and_brain()
        food = CommodityDefinition(
            id="food", name="Food", transportable=True, description=""
        )
        market = Mock()
        market.get_bid_ask_spread.return_value = (3, 5)

        cache = brain._turn_cache(actor)
        assert _get_bid_ask(market, food, cache) == (3, 5)

        # Market mock changes, but within the same turn the quote is cached.
        market.get_bid_ask_spread.return_value = (4, 6)
        assert _get_bid_ask(market, food, brain._turn_cache(actor)) == (3, 5)

        # New turn: the quote is re-fetched from the changed market.
        sim.current_turn = 1
        assert _get_bid_ask(market, food, brain._turn_cache(actor)) == (4, 6)

    def test_inventory_change_clears_actor_group_but_keeps_market_group(self) -> None:
        actor, brain, _sim = self._actor_and_brain()
        thing = CommodityDefinition(
            id="thing", name="Thing", transportable=True, description=""
        )

        cache = brain._turn_cache(actor)
        cache.bid_ask["food"] = (3, 5)
        cache.replacement_cost[("food", 10)] = 7.0
        cache.imputed_cost["food"] = 7.0
        cache.best_result = (None, 0.0)

        # No state change: everything survives.
        cache = brain._turn_cache(actor)
        assert cache.bid_ask == {"food": (3, 5)}
        assert cache.replacement_cost == {("food", 10): 7.0}

        # An inventory change, as after a ProcessCommand, drops the actor-state
        # group and keeps market quotes.
        actor.inventory.add_commodity(thing, 1)
        cache = brain._turn_cache(actor)
        assert cache.bid_ask == {"food": (3, 5)}
        assert cache.replacement_cost == {}
        assert cache.imputed_cost == {}
        assert cache.best_result is None

    def test_skill_change_clears_skill_and_valuation_groups(self) -> None:
        actor, brain, _sim = self._actor_and_brain()
        cache = brain._turn_cache(actor)
        cache.skill_factor["make_food"] = 1.5
        cache.ranked_profits = []
        cache.bid_ask["food"] = (3, 5)

        actor.improve_skill("farming", 0.1)
        cache = brain._turn_cache(actor)
        assert cache.skill_factor == {}
        assert cache.ranked_profits is None
        assert cache.bid_ask == {"food": (3, 5)}

    def test_turn_change_clears_market_and_actor_groups(self) -> None:
        actor, brain, sim = self._actor_and_brain()
        cache = brain._turn_cache(actor)
        cache.bid_ask["food"] = (3, 5)
        cache.replacement_cost[("food", 10)] = 7.0
        cache.ranked_profits = []

        sim.current_turn = 1
        cache = brain._turn_cache(actor)
        assert cache.bid_ask == {}
        assert cache.replacement_cost == {}
        assert cache.ranked_profits is None

    def test_skill_factor_survives_turn_and_inventory_changes(self) -> None:
        """Skill factors depend only on skills, so they outlive turns and inventory."""
        actor, brain, sim = self._actor_and_brain()
        thing = CommodityDefinition(
            id="thing", name="Thing", transportable=True, description=""
        )

        cache = brain._turn_cache(actor)
        cache.skill_factor["make_food"] = 1.5

        actor.inventory.add_commodity(thing, 1)
        cache = brain._turn_cache(actor)
        assert cache.skill_factor == {"make_food": 1.5}

        sim.current_turn = 1
        cache = brain._turn_cache(actor)
        assert cache.skill_factor == {"make_food": 1.5}

    def test_ranked_profits_survives_inventory_change(self) -> None:
        """The mid-turn inventory bump keeps ranked_profits and drops best_result.

        The ranking is quote and skill derived, and keeping it avoids a second
        registry scan in decide_market_actions. best_result reads inventory
        via can_execute, so it must drop.
        """
        actor, brain, _sim = self._actor_and_brain()
        thing = CommodityDefinition(
            id="thing", name="Thing", transportable=True, description=""
        )

        cache = brain._turn_cache(actor)
        cache.ranked_profits = []
        cache.best_result = (None, 0.0)

        actor.inventory.add_commodity(thing, 1)
        cache = brain._turn_cache(actor)
        assert cache.ranked_profits == []
        assert cache.best_result is None

    def test_yield_modifier_is_never_reset(self) -> None:
        actor, brain, sim = self._actor_and_brain()
        cache = brain._turn_cache(actor)
        cache.yield_modifier["gather_biomass"] = 0.7

        actor.improve_skill("farming", 0.1)
        sim.current_turn = 5
        cache = brain._turn_cache(actor)
        assert cache.yield_modifier == {"gather_biomass": 0.7}

    def test_each_brain_owns_one_cache_instance(self) -> None:
        actor, brain, _sim = self._actor_and_brain()
        first = brain._turn_cache(actor)
        assert brain._turn_cache(actor) is first
        assert ColonistBrain()._turn_cache(actor) is not first


class TestBestProcessRankedWalkEquivalence:
    """The ranked walk in _best_process_and_raw_profit matches a full scan.

    The reference is a first-strictly-better registry scan, including
    tie-breaking by registry order and the > 10.0 profit bar.
    """

    @staticmethod
    def _brute_force_reference(
        brain: ColonistBrain, actor: Actor, market: object
    ) -> tuple:
        best_process = None
        best_discounted_profit = 10.0
        best_raw_profit = 0.0
        for process in actor.sim.process_registry.all_processes():
            input_cost = 0.0
            for commodity, quantity in process.inputs.items():
                _bid, ask = market.get_bid_ask_spread(commodity)  # type: ignore[attr-defined]
                price = ask if ask is not None else market.get_avg_price(commodity)  # type: ignore[attr-defined]
                input_cost += price * quantity
            output_value = 0.0
            for commodity, quantity in process.outputs.items():
                bid, _ask = market.get_bid_ask_spread(commodity)  # type: ignore[attr-defined]
                price = bid if bid is not None else market.get_avg_price(commodity)  # type: ignore[attr-defined]
                output_value += price * quantity
            expected_value = (
                output_value
                * brain._expected_yield_modifier(actor, process)
                * brain._expected_skill_factor(actor, process)
            )
            discounted_profit = expected_value - input_cost
            if (
                actor.can_execute_process(process.id)
                and discounted_profit > best_discounted_profit
            ):
                best_process = process
                best_discounted_profit = discounted_profit
                best_raw_profit = output_value - input_cost
        return (best_process, best_raw_profit)

    def test_matches_brute_force_scan_on_a_real_sim(self) -> None:
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=2, num_regular_actors=10, num_market_makers=1, num_ships=1
        )
        for _ in range(5):
            sim.run_turn()
        # run_turn leaves current_turn at the just-played turn, so brains would
        # serve quotes cached mid-phase, before end-of-turn matching moved the
        # books. Step to the next turn so the cached path and the reference
        # both read the post-matching state, as a real decide_* call would.
        sim.current_turn += 1

        checked = 0
        for planet in sim.planets:
            market = planet.market
            for actor in planet.actors:
                brain = actor.brain
                if not isinstance(brain, ColonistBrain):
                    continue
                expected = self._brute_force_reference(brain, actor, market)
                cache = brain._turn_cache(actor)
                got = brain._best_process_and_raw_profit(actor, market, cache)
                assert got == expected
                # Again from the memoized ranking after an inventory-only
                # change, the mid-turn path that skips the second scan.
                wood = sim.commodity_registry.get_commodity("wood")
                assert wood is not None
                actor.inventory.add_commodity(wood, 1)
                expected_after = self._brute_force_reference(brain, actor, market)
                cache = brain._turn_cache(actor)
                assert cache.ranked_profits is not None  # survived the bump
                got_after = brain._best_process_and_raw_profit(actor, market, cache)
                assert got_after == expected_after
                checked += 1
        assert checked >= 10


class TestSharedQuoteTable:
    """The actor-independent process quote table is shared per market.

    It is keyed on (sim turn, quote_version). An identical key implies
    identical quotes and average prices, so the tables are identical. Any
    best-quote mutation bumps the version and forces a rebuild.
    """

    def test_two_colonists_share_the_table_and_a_quote_move_rebuilds(self) -> None:
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=1, num_regular_actors=20, num_market_makers=1, num_ships=1
        )
        for _ in range(5):
            sim.run_turn()
        sim.current_turn += 1

        planet = sim.planets[0]
        market = planet.market
        colonists = [a for a in planet.actors if isinstance(a.brain, ColonistBrain)]
        assert len(colonists) >= 4
        first, second, seller, third = colonists[:4]
        brain_a = first.brain
        brain_b = second.brain
        brain_c = third.brain
        assert isinstance(brain_a, ColonistBrain)
        assert isinstance(brain_b, ColonistBrain)
        assert isinstance(brain_c, ColonistBrain)

        cache_a = brain_a._turn_cache(first)
        expected_a = TestBestProcessRankedWalkEquivalence._brute_force_reference(
            brain_a, first, market
        )
        assert brain_a._best_process_and_raw_profit(first, market, cache_a) == (
            expected_a
        )
        table_a = cache_a.process_quote_values
        assert table_a is not None
        shared = market.shared_quote_table
        assert shared is not None
        assert shared[1] is table_a

        # Second actor, unchanged book: same object, no rebuild. The shared
        # table still yields the brute-force answer for B's own skills and
        # inventory, since only the quote-derived part is shared.
        cache_b = brain_b._turn_cache(second)
        expected_b = TestBestProcessRankedWalkEquivalence._brute_force_reference(
            brain_b, second, market
        )
        assert brain_b._best_process_and_raw_profit(second, market, cache_b) == (
            expected_b
        )
        assert cache_b.process_quote_values is table_a

        # An order improving a best quote bumps the version; the next scan
        # rebuilds a fresh table with the new quote priced in.
        food = sim.commodity_registry.get_commodity("food")
        assert food is not None
        bid, ask = market.get_bid_ask_spread(food)
        version_before = market.quote_version
        seller.money += 1000  # ensure the improving order can be funded
        if ask is not None and ask > 1:
            seller.inventory.add_commodity(food, 5)
            assert market.place_sell_order(seller, food, 1, ask - 1)
        else:
            # Cannot undercut a 1-credit or absent ask; improve the bid side.
            assert market.place_buy_order(
                seller, food, 1, 1 if bid is None else bid + 1
            )
        assert market.quote_version > version_before

        cache_c = brain_c._turn_cache(third)
        expected_c = TestBestProcessRankedWalkEquivalence._brute_force_reference(
            brain_c, third, market
        )
        assert brain_c._best_process_and_raw_profit(third, market, cache_c) == (
            expected_c
        )
        table_c = cache_c.process_quote_values
        assert table_c is not None
        assert table_c is not table_a

    def test_quote_version_bump_sites(self) -> None:
        registry = CommodityRegistry()
        registry.load_from_file(str(DATA_DIR / "commodities.yaml"))
        market = Market()
        market.commodity_registry = registry
        food = registry.get_commodity("food")
        assert food is not None

        seller = get_actor("Seller", initial_money=1000)
        seller.inventory.add_commodity(food, 20)
        buyer = get_actor("Buyer", initial_money=1000)

        # Populate the incremental quote cache so the bump logic can compare
        # against a known best rather than bumping conservatively.
        market.get_bid_ask_spread(food)

        v0 = market.quote_version
        best = market.place_sell_order(seller, food, 1, 10)  # first ask: best
        assert best and market.quote_version == v0 + 1

        market.get_bid_ask_spread(food)
        v1 = market.quote_version
        worse = market.place_sell_order(seller, food, 1, 15)  # behind the best
        assert worse and market.quote_version == v1  # no bump

        improving = market.place_sell_order(seller, food, 1, 8)  # new best
        assert improving and market.quote_version == v1 + 1

        market.get_bid_ask_spread(food)
        v2 = market.quote_version
        assert market.cancel_order(worse)  # not at the best: no bump
        assert market.quote_version == v2
        assert market.cancel_order(improving)  # at the best ask: bump
        assert market.quote_version == v2 + 1

        # Bid side: a first (best-populating) bid bumps too.
        market.get_bid_ask_spread(food)
        v3 = market.quote_version
        assert market.place_buy_order(buyer, food, 1, 5)
        assert market.quote_version == v3 + 1


class TestReplacementCostSplitEquivalence:
    """The quote-part split of _replacement_cost matches the single-pass version.

    Values must be bit-identical in every actor state, including right after
    mid-turn inventory and skill bumps, which must not invalidate the
    per-turn replacement_quote_parts table.
    """

    @staticmethod
    def _reference(
        brain: ActorBrain,
        actor: Actor,
        market: object,
        commodity: CommodityDefinition,
    ) -> float | None:
        """Cache-less copy of the pre-split _replacement_cost."""
        best: float | None = None
        for process in actor.sim.process_registry.get_processes_producing(commodity):
            out_qty = process.outputs.get(commodity, 0)
            if out_qty <= 0:
                continue
            if not all(
                actor.inventory.has_quantity(facility, 1)
                for facility in process.facilities_required
            ):
                continue

            input_cost = 0.0
            for input_commodity, qty in process.inputs.items():
                _bid, ask = market.get_bid_ask_spread(input_commodity)  # type: ignore[attr-defined]
                price = (
                    ask if ask is not None else market.get_avg_price(input_commodity)  # type: ignore[attr-defined]
                )
                input_cost += price * qty
            for tool in process.tools_required:
                if actor.inventory.has_quantity(tool, 1):
                    continue
                _bid, ask = market.get_bid_ask_spread(tool)  # type: ignore[attr-defined]
                price = ask if ask is not None else market.get_avg_price(tool)  # type: ignore[attr-defined]
                input_cost += price / TOOL_EXPECTED_LIFESPAN

            expected_out = out_qty * brain._expected_yield_modifier(actor, process)
            if expected_out <= 0:
                continue
            skill_factor = brain._expected_skill_factor(actor, process)
            per_unit = input_cost / expected_out + GOVERNMENT_WAGE / (
                expected_out * skill_factor
            )
            if best is None or per_unit < best:
                best = per_unit
        return best

    def test_matches_reference_on_a_real_sim(self) -> None:
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=2, num_regular_actors=10, num_market_makers=1, num_ships=1
        )
        for _ in range(5):
            sim.run_turn()
        # Step past the just-played turn so cached and reference reads both
        # see the post-matching books. See the ranked-walk test above.
        sim.current_turn += 1

        commodities = sim.commodity_registry.all_commodities()
        tool = sim.commodity_registry.get_commodity("simple_tools")
        assert tool is not None
        checked = 0
        for planet in sim.planets:
            market = planet.market
            for actor in planet.actors:
                brain = actor.brain
                if not isinstance(brain, ActorBrain):
                    continue
                cache = brain._turn_cache(actor)
                for commodity in commodities:
                    expected = self._reference(brain, actor, market, commodity)
                    got = brain._replacement_cost(actor, market, commodity, cache)
                    assert got == expected

                # A mid-turn inventory bump, as after a ProcessCommand, gives
                # the actor a tool and flips tool-amortization branches. The
                # actor-group memo must drop; the quote-parts table must not.
                actor.inventory.add_commodity(tool, 1)
                cache = brain._turn_cache(actor)
                assert cache.replacement_quote_parts  # survived the bump
                assert cache.replacement_cost == {}
                for commodity in commodities:
                    expected = self._reference(brain, actor, market, commodity)
                    got = brain._replacement_cost(actor, market, commodity, cache)
                    assert got == expected

                # A mid-turn skill bump, as after every successful
                # ProcessCommand, still keeps the quote parts.
                actor.improve_skill("farming", 0.2)
                cache = brain._turn_cache(actor)
                assert cache.replacement_quote_parts
                assert cache.replacement_cost == {}
                for commodity in commodities:
                    expected = self._reference(brain, actor, market, commodity)
                    got = brain._replacement_cost(actor, market, commodity, cache)
                    assert got == expected
                checked += 1
        assert checked >= 10


class TestCheapestMaterialAskEquivalence:
    """The quote fast path of _cheapest_material_ask matches a full book scan.

    The reference scan skips own and cancelled orders.
    """

    @staticmethod
    def _reference(actor: Actor, market: object, materials: list) -> tuple:
        chosen = materials[0]
        best_ask = None
        for commodity in materials:
            asks = [
                o.price
                for o in market.sell_orders.get(commodity, [])  # type: ignore[attr-defined]
                if o.actor != actor and not o.cancelled
            ]
            if asks:
                low = min(asks)
                if best_ask is None or low < best_ask:
                    best_ask = low
                    chosen = commodity
        return chosen, best_ask

    def _market_and_actors(self) -> tuple:
        registry = CommodityRegistry()
        registry.load_from_file(str(DATA_DIR / "commodities.yaml"))
        market = Market()
        market.commodity_registry = registry
        me = get_actor("Me", initial_money=1000)
        other = get_actor("Other", initial_money=1000)
        brain = ColonistBrain()
        return registry, market, me, other, brain

    def test_matches_reference_on_a_real_sim(self) -> None:
        from spacesim2.core.simulation import Simulation

        sim = Simulation()
        sim.setup_simple(
            num_planets=2, num_regular_actors=10, num_market_makers=1, num_ships=1
        )
        for _ in range(5):
            sim.run_turn()
        sim.current_turn += 1

        commodities = sim.commodity_registry.all_commodities()
        checked = 0
        for planet in sim.planets:
            market = planet.market
            for actor in planet.actors:
                brain = actor.brain
                if not isinstance(brain, ActorBrain):
                    continue
                cache = brain._turn_cache(actor)
                # The whole-registry list exercises the cross-material min;
                # per-commodity calls exercise every single-material path.
                expected = self._reference(actor, market, commodities)
                got = brain._cheapest_material_ask(actor, market, commodities, cache)
                assert got == expected
                for commodity in commodities:
                    expected = self._reference(actor, market, [commodity])
                    got = brain._cheapest_material_ask(
                        actor, market, [commodity], cache
                    )
                    assert got == expected
                checked += 1
        assert checked >= 10

    def test_actor_owning_the_lowest_ask_falls_back_to_scan(self) -> None:
        registry, market, me, other, brain = self._market_and_actors()
        food = registry.get_commodity("food")
        assert food is not None
        me.inventory.add_commodity(food, 10)
        other.inventory.add_commodity(food, 10)

        market.place_sell_order(me, food, 1, 5)  # own lowest ask
        market.place_sell_order(other, food, 1, 8)
        market.place_sell_order(other, food, 1, 12)

        expected = self._reference(me, market, [food])
        assert expected == (food, 8)
        assert brain._cheapest_material_ask(me, market, [food]) == expected

    def test_cancelled_orders_are_ignored(self) -> None:
        registry, market, me, other, brain = self._market_and_actors()
        food = registry.get_commodity("food")
        assert food is not None
        me.inventory.add_commodity(food, 10)
        other.inventory.add_commodity(food, 10)

        cheap = market.place_sell_order(other, food, 1, 4)
        market.place_sell_order(other, food, 1, 9)
        own = market.place_sell_order(me, food, 1, 3)
        assert market.cancel_order(cheap)  # dead order rests in the book
        assert market.cancel_order(own)  # own cancelled ask must not count

        expected = self._reference(me, market, [food])
        assert expected == (food, 9)
        assert brain._cheapest_material_ask(me, market, [food]) == expected

    def test_only_own_asks_yield_none(self) -> None:
        registry, market, me, _other, brain = self._market_and_actors()
        food = registry.get_commodity("food")
        assert food is not None
        me.inventory.add_commodity(food, 10)
        market.place_sell_order(me, food, 1, 5)

        wood = registry.get_commodity("wood")
        assert wood is not None
        expected = self._reference(me, market, [food, wood])
        assert expected == (food, None)
        assert brain._cheapest_material_ask(me, market, [food, wood]) == expected

    def test_material_order_breaks_price_ties(self) -> None:
        registry, market, me, other, brain = self._market_and_actors()
        food = registry.get_commodity("food")
        wood = registry.get_commodity("wood")
        assert food is not None and wood is not None
        other.inventory.add_commodity(food, 10)
        other.inventory.add_commodity(wood, 10)
        market.place_sell_order(other, food, 1, 7)
        market.place_sell_order(other, wood, 1, 7)

        expected = self._reference(me, market, [wood, food])
        assert expected == (wood, 7)
        assert brain._cheapest_material_ask(me, market, [wood, food]) == expected


class TestSellAtOrAboveCostMaxPass:
    """The single max pass prices like a full sort that reads bids[0].price."""

    def test_prices_at_best_non_own_live_bid(self) -> None:
        registry = CommodityRegistry()
        registry.load_from_file(str(DATA_DIR / "commodities.yaml"))
        market = Market()
        market.commodity_registry = registry
        food = registry.get_commodity("food")
        assert food is not None

        me = get_actor("Me", initial_money=1000)
        other = get_actor("Other", initial_money=1000)
        me.sim.process_registry = _load_real_registries()[1]

        brain = ColonistBrain()
        floor = brain._replacement_cost(me, market, food)
        min_ask = 1 if floor is None else max(1, math.ceil(floor))
        high_bid = min_ask + 10

        market.place_buy_order(other, food, 1, high_bid)
        market.place_buy_order(me, food, 1, high_bid + 5)  # own: ignored
        dead = market.place_buy_order(other, food, 1, high_bid + 9)
        assert market.cancel_order(dead)  # cancelled: ignored

        commands = brain._sell_at_or_above_cost(me, market, food, 3)
        assert len(commands) == 1
        command = commands[0]
        assert isinstance(command, PlaceSellOrderCommand)
        # The best live non-own bid covers the floor, so hit it.
        assert command.price == high_bid
        assert command.quantity == 3

        # With no live non-own bids, the ask rests at the floor.
        market.cancel_order(market.actor_orders[other]["buy"][0])
        commands = brain._sell_at_or_above_cost(me, market, food, 3)
        command = commands[0]
        assert isinstance(command, PlaceSellOrderCommand)
        assert command.price == min_ask
