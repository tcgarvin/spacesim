from operator import itemgetter
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from spacesim.core.actor import Actor
from spacesim.core.actor_brain import (
    GOVERNMENT_WAGE,
    ActorBrain,
    BrainCache,
    _get_avg_price,
    _get_bid_ask,
)
from spacesim.core.brains.hand_food import hand_food_viable
from spacesim.core.brains.migration import (
    MigrationIntent,
    clear_intent,
    draw_propensity,
    is_leaving,
)
from spacesim.core.brains.migration import decide_migration as decide_migration_for
from spacesim.core.commands import (
    CancelOrderCommand,
    EconomicCommand,
    GovernmentWorkCommand,
    MarketCommand,
    PlaceBuyOrderCommand,
    ProcessCommand,
)
from spacesim.core.drives.food_drive import food_pantry_units

if TYPE_CHECKING:
    from spacesim.core.commodity import CommodityDefinition
    from spacesim.core.market import Market
    from spacesim.core.migration import MigrationDecision
    from spacesim.core.process import ProcessDefinition

# Sort key for ranked-profit entries: the discounted profit. itemgetter
# avoids a Python-level lambda per comparison. reverse=True keeps the sort
# stable, so registry order still breaks ties.
_DISCOUNTED_PROFIT_KEY = itemgetter(0)


class ColonistBrain(ActorBrain):
    """Decision-making logic for regular colonist actors."""

    def __init__(self) -> None:
        # Migration state. The propensity is fixed for the actor's life, so
        # two colonists under identical pressure do not move in lockstep.
        self.migration_intent = MigrationIntent()
        self.migration_propensity = draw_propensity()

    def decide_migration(self, actor: Actor) -> "MigrationDecision":
        """Delegate to the shared migration reasoning."""
        return decide_migration_for(self, actor)

    def on_relocated(self, actor: Actor) -> None:
        """Reset planet-specific state after a move.

        Two things are tied to the old planet: the intent that brought the
        actor here, and the brain cache, whose ``yield_modifier`` group is
        never invalidated because an actor's land was assumed fixed. A
        migrant's land is not, so the cache is dropped outright.
        """
        clear_intent(self)
        self._cache = None

    def decide_economic_action(self, actor: Actor) -> Optional[EconomicCommand]:
        """Decide which economic action to take this turn."""
        # Market quotes and process cost/yield figures are shared with
        # decide_market_actions later this turn, since the market cannot move
        # in between. Inventory- and skill-dependent entries invalidate if
        # the economic command changes actor state. See BrainCache.
        cache = self._turn_cache(actor)

        # Goods made by hand for the actor's own needs this turn draw a
        # displacement bid in the market phase. See
        # ActorBrain._displacement_bid_commands.
        self._begin_self_supply_record()

        # Basic needs first: food, clothing, shelter.
        registry = actor.sim.commodity_registry

        food_commodity = registry.get_commodity("food")
        biomass_commodity = registry.get_commodity("biomass")
        clothing_commodity = registry.get_commodity("clothing")
        fiber_commodity = registry.get_commodity("fiber")
        wood_commodity = registry.get_commodity("wood")

        if not food_commodity or not biomass_commodity:
            return GovernmentWorkCommand()

        # Food is most urgent. The pantry counts the bought staple as well as
        # hand-cooked food, so an actor living on processed food does not cook.
        # On land too barren to feed its holder the gate is skipped outright:
        # gathering there never fills the pantry, so the actor would repeat it
        # forever. It works for wages instead and buys food. See hand_food.py.
        if food_pantry_units(actor) < 5 and hand_food_viable(actor, cache):
            if actor.can_execute_process("make_food"):
                return self._record_self_supply(actor, "make_food")

            # Available, not total: units reserved in the actor's own sell
            # order cannot be cooked, so they must not satisfy the gate.
            biomass_quantity = actor.inventory.get_available_quantity(biomass_commodity)
            if biomass_quantity < 4 and actor.can_execute_process("gather_biomass"):
                return self._record_self_supply(actor, "gather_biomass")

        if clothing_commodity and fiber_commodity:
            clothing_quantity = actor.inventory.get_quantity(clothing_commodity)
            if clothing_quantity < 3:
                if actor.can_execute_process("make_clothing"):
                    return self._record_self_supply(actor, "make_clothing")

                fiber_quantity = actor.inventory.get_available_quantity(fiber_commodity)
                if fiber_quantity < 4 and actor.can_execute_process("gather_fiber"):
                    return self._record_self_supply(actor, "gather_fiber")

        # Keep a wood buffer; it feeds tools and building materials.
        if wood_commodity:
            wood_quantity = actor.inventory.get_quantity(wood_commodity)
            if wood_quantity < 2 and actor.can_execute_process("harvest_wood"):
                return self._record_self_supply(actor, "harvest_wood")

        # simple_building_materials feeds ShelterDrive.
        building_materials = registry.get_commodity("simple_building_materials")
        if building_materials:
            bm_quantity = actor.inventory.get_quantity(building_materials)
            if bm_quantity < 3:
                # Bootstrap recipe: 2 wood + simple_tools -> 1 building material
                if actor.can_execute_process("make_building_materials_wood"):
                    return self._record_self_supply(
                        actor, "make_building_materials_wood"
                    )
                # Probably short on wood.
                if wood_commodity:
                    wood_quantity = actor.inventory.get_quantity(wood_commodity)
                    if wood_quantity < 2 and actor.can_execute_process("harvest_wood"):
                        return self._record_self_supply(actor, "harvest_wood")

        # Tools before productive work.
        tools_commodity = registry.get_commodity("simple_tools")
        if tools_commodity:
            tools_quantity = actor.inventory.get_quantity(tools_commodity)
            if tools_quantity < 2:
                # Buy rather than make when the market is cheaper.
                market = actor.planet.market if actor.planet else None
                should_make_tools = True

                if market:
                    willingness_to_pay = self._calculate_tool_willingness_to_pay(
                        actor, cache
                    )
                    bid, ask = _get_bid_ask(market, tools_commodity, cache)

                    # Affordable tools on the market: buy in the market phase.
                    if ask is not None and ask <= willingness_to_pay:
                        should_make_tools = False

                if should_make_tools and actor.can_execute_process("make_simple_tools"):
                    return ProcessCommand("make_simple_tools")

        market = actor.planet.market if actor.planet else None
        if market:
            best_process = self._find_most_profitable_process(actor, market, cache)

            # best_process was set only where can_execute_process was already
            # true, and nothing mutates actor state between that scan and
            # here, so re-checking would repeat work.
            if best_process:
                return ProcessCommand(best_process.id)

        return GovernmentWorkCommand()

    def _find_most_profitable_process(
        self, actor: Actor, market: "Market", cache: Optional[BrainCache] = None
    ) -> Optional["ProcessDefinition"]:
        """Find the most profitable process by expected outcome.

        Output value is discounted by planet resource availability and the
        actor's skill, since failed runs waste the turn. Otherwise a high
        ore price lures colonists on resource-poor planets into mining loops
        that fail nearly every turn while they starve.
        """
        best_process, _raw_profit = self._best_process_and_raw_profit(
            actor, market, cache
        )
        return best_process

    def _best_process_and_raw_profit(
        self, actor: Actor, market: "Market", cache: Optional[BrainCache] = None
    ) -> Tuple[Optional["ProcessDefinition"], float]:
        """Scan the process registry once for the best process and its raw profit.

        The best process is chosen on discounted expected profit, the same
        criterion as ``_find_most_profitable_process``. Its raw profit is
        undiscounted output_value - input_cost, which
        ``labor_opportunity_cost`` reports as the opportunity cost
        of a turn.

        Three memo levels (see BrainCache), matching what changes when the
        mid-turn economic command executes:

        * ``cache.best_result``: the final answer. Dropped on any inventory
          or skills change, since it read ``can_execute``.
        * ``cache.ranked_profits``: profit figures of every process whose
          discounted profit clears the government-work bar of 10.0, sorted
          by descending discounted profit. Nothing below the bar can be
          selected. Survives inventory-only changes.
        * ``cache.process_quote_values``: the quote-derived input cost and
          output value per process. Depends on neither skills nor
          inventory, so it survives everything within the turn. A
          successful ProcessCommand bumps skills every time, so this level
          spares the second ``decide_*`` call a fresh quote scan. Re-ranking
          from it is a skill multiply and a sort.
        """
        if cache is not None and cache.best_result is not None:
            return cache.best_result

        ranked = cache.ranked_profits if cache is not None else None
        if ranked is None:
            # Cache dicts are hoisted into locals and probed inline: nearly
            # every lookup below is a memo hit, and a helper call per hit is
            # costly. On a miss the helper runs, keeping fill and invalidation
            # logic in one place. When cache is None the throwaway empty dicts
            # always miss, reproducing the uncached path exactly.
            bid_ask_memo = cache.bid_ask if cache is not None else {}
            avg_price_memo = cache.avg_price if cache is not None else {}

            quote_values = cache.process_quote_values if cache is not None else None
            if quote_values is None:
                # The table is actor-independent: quotes and avg prices only,
                # with skill and yield discounting applied per actor below.
                # So it is shared across actors on the same market, keyed on
                # (sim turn, quote_version). Two actors at the same key see
                # identical bid/ask, since the version covers every
                # best-quote mutation, and identical avg prices, since trade
                # history only moves in end-of-turn matching. The shared list
                # is read-only by contract. Per-actor quote caches stay exact
                # contributors: within one actor's turn slice the market
                # cannot mutate, because its own market commands execute
                # after both decide_* calls, so quotes cached earlier in the
                # slice equal live quotes.
                shared_key = (actor.sim.current_turn, market.quote_version)
                shared = market.shared_quote_table
                if shared is not None and shared[0] == shared_key:
                    quote_values = shared[1]
                    if cache is not None:
                        cache.process_quote_values = quote_values
            if quote_values is None:
                quote_values = []
                for process in actor.sim.process_registry.all_processes():
                    input_cost = 0.0
                    for commodity, quantity in process.inputs_items:
                        # Inputs at ask.
                        pair = bid_ask_memo.get(commodity.id)
                        if pair is None:
                            pair = _get_bid_ask(market, commodity, cache)
                        ask = pair[1]
                        if ask is not None:
                            price = float(ask)
                        else:
                            price = avg_price_memo.get(commodity.id, -1.0)
                            if price < 0.0:
                                price = _get_avg_price(market, commodity, cache)
                        input_cost += price * quantity

                    output_value = 0.0
                    for commodity, quantity in process.outputs_items:
                        # Outputs at bid.
                        pair = bid_ask_memo.get(commodity.id)
                        if pair is None:
                            pair = _get_bid_ask(market, commodity, cache)
                        bid = pair[0]
                        if bid is not None:
                            price = float(bid)
                        else:
                            price = avg_price_memo.get(commodity.id, -1.0)
                            if price < 0.0:
                                price = _get_avg_price(market, commodity, cache)
                        output_value += price * quantity

                    quote_values.append((input_cost, output_value, process))
                market.shared_quote_table = (shared_key, quote_values)
                if cache is not None:
                    cache.process_quote_values = quote_values

            yield_memo = cache.yield_modifier if cache is not None else {}
            skill_memo = cache.skill_factor if cache is not None else {}
            ranked = []
            for input_cost, output_value, process in quote_values:
                yield_mod = yield_memo.get(process.id)
                if yield_mod is None:
                    yield_mod = self._expected_yield_modifier(actor, process, cache)
                skill_factor = skill_memo.get(process.id)
                if skill_factor is None:
                    skill_factor = self._expected_skill_factor(actor, process, cache)
                discounted = output_value * yield_mod * skill_factor - input_cost
                # Must exceed the government-work bar of 10.0. Entries at or
                # below it can never be selected by the walk below, so they
                # are dropped before the sort.
                if discounted > 10.0:
                    ranked.append((discounted, output_value - input_cost, process))
            # Stable sort: equal discounted profits keep registry order, which
            # the pre-sort filter also preserves, so the walk below picks the
            # first strictly better process in registry order.
            ranked.sort(key=_DISCOUNTED_PROFIT_KEY, reverse=True)
            if cache is not None:
                cache.ranked_profits = ranked

        best_process: Optional["ProcessDefinition"] = None
        best_raw_profit = 0.0
        for _discounted_profit, raw_profit, process in ranked:
            if actor.can_execute(process):
                best_process = process
                best_raw_profit = raw_profit
                break

        result = (best_process, best_raw_profit)
        if cache is not None:
            cache.best_result = result
        return result

    def labor_opportunity_cost(
        self, actor: Actor, cache: Optional[BrainCache] = None
    ) -> int:
        """What a turn of this colonist's labor is worth.

        The raw profit of the actor's next-best economic action, floored at
        GOVERNMENT_WAGE. Prices the labor turn in tool make-or-buy and in
        the displacement bid for goods the actor makes by hand.
        """
        market = actor.planet.market if actor.planet else None
        if not market:
            return GOVERNMENT_WAGE

        best_process, raw_profit = self._best_process_and_raw_profit(
            actor, market, cache
        )
        if not best_process:
            return GOVERNMENT_WAGE

        return max(GOVERNMENT_WAGE, int(raw_profit))

    def _calculate_tool_willingness_to_pay(
        self, actor: Actor, cache: Optional[BrainCache] = None
    ) -> int:
        """Maximum price the actor pays for a tool.

        cost_to_make_inputs + opportunity_cost_of_turn, where the inputs are
        the market ask for 2 common_metal and the opportunity cost is the
        profit of the next-best action, or government work.
        """
        market = actor.planet.market if actor.planet else None
        if not market:
            return 0  # no market

        # make_simple_tools takes 2 common_metal per processes.yaml.
        common_metal = actor.sim.commodity_registry.get_commodity("common_metal")
        if not common_metal:
            return 0

        bid, ask = _get_bid_ask(market, common_metal, cache)
        metal_price = (
            ask if ask is not None else _get_avg_price(market, common_metal, cache)
        )
        input_cost = int(metal_price * 2)

        opportunity_cost = self.labor_opportunity_cost(actor, cache)

        return input_cost + opportunity_cost

    # Inventory to retain when selling surplus, for goods not backed by a
    # drive. Drive goods derive their keep level from target_units. Biomass
    # and fiber are kept at one batch of make_food and make_clothing: with
    # no keep level, gathered units were listed for sale the same turn, and
    # the reserved stock counted toward the need gate's "enough to cook"
    # check while the recipe could not touch it, so a hungry actor neither
    # gathered nor cooked.
    NON_DRIVE_KEEP_LEVELS = {
        "simple_tools": 2,  # tools for production
        "wood": 2,  # raw input for tools/building materials
        "common_metal": 2,  # alternate building-material input
        "biomass": 4,  # one make_food batch
        "fiber": 4,  # one make_clothing batch
    }

    def decide_market_actions(self, actor: "Actor") -> List[MarketCommand]:
        """Buy drive needs (with willingness-to-pay) and sell surplus production."""
        if not actor.planet:
            return []

        market = actor.planet.market
        commands: List[MarketCommand] = []

        # Cancel all existing orders before re-posting.
        existing_orders = market.get_actor_orders(actor)
        for order in existing_orders["buy"] + existing_orders["sell"]:
            commands.append(CancelOrderCommand(order.order_id))

        # Shared with decide_economic_action earlier this turn: market quotes
        # carry over unconditionally, since the economic command cannot move
        # the order books. Replacement costs and the profitability scan are
        # reused only if it left inventory and skills untouched. See
        # BrainCache.
        cache = self._turn_cache(actor)

        # Drive materials at willingness-to-pay.
        commands.extend(self._drive_buy_commands(actor, market, cache))

        # Tools enable production but are not a drive.
        commands.extend(self._tool_buy_commands(actor, market, cache))

        commands.extend(self._sell_excess_commands(actor, market, cache))

        return commands

    def _tool_buy_commands(
        self, actor: Actor, market: "Market", cache: Optional[BrainCache] = None
    ) -> List[MarketCommand]:
        """Buy simple_tools up to a buffer, bounded by willingness to pay."""
        if is_leaving(self):
            # An actor on its way out is turning stock into fare money, not
            # buying equipment for work it is about to abandon.
            return []
        tools = actor.sim.commodity_registry.get_commodity("simple_tools")
        if not tools:
            return []

        keep = self.NON_DRIVE_KEEP_LEVELS["simple_tools"]
        have = actor.inventory.get_quantity(tools)
        if have >= keep:
            return []
        need = keep - have

        willingness = self._calculate_tool_willingness_to_pay(actor, cache)
        if willingness <= 0:
            return []

        market_sell_orders = sorted(
            [
                o
                for o in market.sell_orders.get(tools, [])
                if o.actor != actor and not o.cancelled
            ],
            key=lambda o: (o.price, o.timestamp),
        )

        if market_sell_orders:
            ask = market_sell_orders[0].price
            if ask > willingness:
                return []  # too expensive; make them in the economic phase
            qty = min(need, actor.money // ask)
            if qty > 0:
                return [PlaceBuyOrderCommand(tools, qty, ask)]
            return []

        # No sellers: rest a bid at willingness to pay to signal demand.
        qty = min(need, actor.money // willingness)
        if qty > 0:
            return [PlaceBuyOrderCommand(tools, qty, willingness)]
        return []

    def _sell_excess_commands(
        self, actor: Actor, market: "Market", cache: Optional[BrainCache] = None
    ) -> List[MarketCommand]:
        """Sell inventory above keep levels, floored at replacement cost.

        While the actor is leaving, the sweep also offers untradable
        goods and drops every working buffer: a facility is worth nothing
        once the actor is off-planet, and tools and raw stock are worth more
        as fare than as a hedge against work the actor will not do here.
        Need-drive materials keep their target, since the actor still eats
        through the intent window and the transit, and a market cannot match
        an actor against itself, so selling the pantry would mean buying it
        back from someone else at the ask every turn.
        """
        leaving = is_leaving(self)
        need_materials = {
            material.id
            for drive in actor.drives
            if drive.WELLBEING
            for material in drive.materials()
        }
        commands: List[MarketCommand] = []
        keep_levels = self._keep_levels_by_commodity(actor)
        for commodity in actor.sim.commodity_registry.all_commodities():
            if not commodity.tradable and not leaving:
                continue
            if leaving and commodity.id not in need_materials:
                keep = 0
            else:
                keep = keep_levels.get(commodity.id, 0)
            available = actor.inventory.get_available_quantity(commodity)
            if available <= keep:
                continue
            commands.extend(
                self._sell_at_or_above_cost(
                    actor, market, commodity, available - keep, cache
                )
            )
        return commands

    def _keep_level(self, actor: Actor, commodity: "CommodityDefinition") -> int:
        """Inventory level to retain for a commodity before selling surplus."""
        for drive in actor.drives:
            if commodity in drive.materials():
                return drive.target_units()
        return self.NON_DRIVE_KEEP_LEVELS.get(commodity.id, 0)

    def _keep_levels_by_commodity(self, actor: Actor) -> Dict[str, int]:
        """Precompute ``_keep_level`` for every commodity in one pass.

        Same first-drive-wins precedence and non-drive fallback as
        ``_keep_level``, but one O(drives) pass over ``actor.drives``
        instead of an O(commodities x drives) membership scan. Drive
        materials and target_units are fixed per drive instance for the
        whole turn, so this is safe to compute once per
        ``_sell_excess_commands`` call.
        """
        levels: Dict[str, int] = {}
        for drive in actor.drives:
            target = drive.target_units()
            for material in drive.materials():
                levels.setdefault(material.id, target)
        for commodity_id, keep in self.NON_DRIVE_KEEP_LEVELS.items():
            levels.setdefault(commodity_id, keep)
        return levels
