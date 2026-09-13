"""Galaxy geometry and fuel-reachability facts shared by ship planning.

A :class:`Navigator` holds the galaxy-wide facts every ship would otherwise
recompute per candidate planet per commodity:

- Geometry, fixed after setup: a lazily built all-pairs shortest-route
  matrix over the star-lane network in ``core/galaxy.py``, the waypoints of
  those routes, and per-planet proximity orderings. Computed once per
  simulation and shared by every ship. Ships fly only along lanes, so
  distance throughout ship planning means lane-route length, never
  straight-line distance.
- Market-derived facts, which change as brains post orders: fuel
  purchasability, nearest-fuel-source distances, the galaxy fuel price
  reference, and the trade-signal index of per-planet commodity summaries
  and per-commodity ranked demand shortlists. These are cached per turn:
  ship brains call :meth:`Navigator.refresh_market_facts` with the current
  turn number, which rebuilds the snapshot only on the turn's first call, so
  every ship planning that turn shares one view of the galaxy. Order
  execution is deferred to end-of-turn matching, so the snapshot stays a
  valid superset filter for the whole turn; plan evaluation re-verifies
  exact prices against the live books anyway.

Use :func:`get_navigator` to obtain the per-simulation shared instance.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, FrozenSet, List, Optional, Tuple
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from spacesim2.core.commodity import CommodityDefinition
    from spacesim2.core.planet import Planet
    from spacesim2.core.simulation import Simulation

# Turns of volume history in which fuel must have traded for a planet to
# count as a place where fuel is purchasable.
FUEL_MARKET_RECENCY_TURNS = 10

# Turns of volume history in which a good must have traded to count as
# exportable from a planet. Mirrors TraderBrain's flow recency window.
FLOW_RECENCY_TURNS = 10

# Shortlist sizes for the per-turn trade-signal index. For each (origin,
# commodity) the candidate destinations are the union of the TOP_K demand
# planets with the highest demand value and the NEAREST_M demand planets
# nearest the origin; a nearby modest market can beat a distant top-value one
# after fuel costs. A commodity with at most K + M demand planets gets all of
# them, so galaxies with <= 16 other planets are surveyed exhaustively.
DESTINATION_TOP_K = 8
DESTINATION_NEAREST_M = 8

# Margin over delivered cost for a standing fuel bid that means to attract a
# delivery. Above the 15% TraderBrain arbitrage threshold so a fuel-delivery
# TradePlan passes ``is_profitable()`` for any deliverer.
FUEL_BID_MARGIN = 0.30

# Fuel price for standing bids when no ask exists anywhere in the galaxy and
# the local market has never traded fuel. The market's avg-price default of
# 10 is fabricated and cannot be trusted.
FUEL_BID_FALLBACK_FLOOR = 15

# A fuel station is a market a ship can plan around: at least
# FUEL_STATION_MIN_DEPTH units resting on the ask side, priced no higher than
# FUEL_STATION_MAX_PRICE_MULT times the galaxy fuel reference. One producer
# unit resting, or a book at a panic price, is not somewhere a ship can refuel.
FUEL_STATION_MIN_DEPTH = 6
FUEL_STATION_MAX_PRICE_MULT = 2.0

# Highest price any fuel bid may carry, as a multiple of the galaxy fuel
# reference. Sellers fill a resting bid at the bid price, so an escalating
# bid pays its own escalation and the fill sets the next average price. The
# cap breaks that ratchet: above it a ship flies to a station instead.
FUEL_BID_CEILING_MULT = 2.0

# The ceiling when no planet has believable fuel evidence, i.e. no cap. There
# is nothing to measure a spike against then, and a bootstrap galaxy needs
# high bids to call the first fuel into existence: capping them at the
# fallback floor instead stopped fuel being produced at all, and over three
# 200-turn 12-planet runs the whole fleet sat with 0 departures and no fuel
# ask anywhere.
FUEL_BID_CEILING_UNBOUNDED = 10**9

# Ships are created with fuel_efficiency in [0.8, 1.2]. When estimating an
# unknown deliverer's burn, assume the worst so the bid stays enticing.
DELIVERER_WORST_FUEL_EFFICIENCY = 0.8


@dataclass
class _TradeSignalIndex:
    """One turn's galaxy-wide supply/demand signal snapshot.

    Built by :meth:`Navigator._trade_signal_index` in one O(planets x
    commodities) pass over every market, then shared by every ship planning
    that turn. All fields are superset filters: plan evaluation still
    re-verifies exact prices against the live books.
    """

    exportable_by_planet: Dict["Planet", FrozenSet["CommodityDefinition"]]
    # Per-commodity: planets where it is plausibly acquirable.
    export_planets: Dict["CommodityDefinition", FrozenSet["Planet"]]
    # Per-commodity: planets with a demand signal, best demand value first;
    # ties keep simulation planet order. demand_planets is the same set, for
    # membership tests.
    demand_ranked: Dict["CommodityDefinition", Tuple["Planet", ...]]
    demand_planets: Dict["CommodityDefinition", FrozenSet["Planet"]]
    # Memo of candidate_destinations results, shared by all ships this turn.
    candidate_memo: Dict[
        Tuple["Planet", "CommodityDefinition"], Tuple["Planet", ...]
    ] = field(default_factory=dict)

    def has_any_trade_signal(self) -> bool:
        """Whether any commodity is exportable somewhere and demanded somewhere.

        False means the galaxy is cold: no ship can build a trade plan, so
        planning can be skipped this turn.
        """
        return any(
            self.export_planets[commodity] and self.demand_ranked[commodity]
            for commodity in self.export_planets
        )


@dataclass(frozen=True)
class _FuelScan:
    """One turn's galaxy-wide fuel market snapshot."""

    # (planet, best ask) for every planet with a resting fuel ask.
    asks: List[Tuple["Planet", int]]
    # (planet, best non-dealer ask, non-dealer depth) where such an ask rests.
    producer_asks: List[Tuple["Planet", int, int]]
    cheapest_ask: Optional[int]
    # Median believable per-planet valuation; see fuel_value_reference.
    reference: Optional[float]


class Navigator:
    """Cached view of galaxy geometry and fuel reachability for one simulation.

    Geometry is cached for the simulation's lifetime. Market-derived facts
    are cached until the next effective :meth:`refresh_market_facts` call,
    which is once per turn when called with the turn number, as ship brains
    do.
    """

    def __init__(self, sim: "Simulation") -> None:
        """Create a navigator bound to ``sim``."""
        self._sim = sim
        # Static geometry, built lazily and rebuilt if the planet set changes.
        self._planet_index: Dict["Planet", int] = {}
        self._distances: List[List[float]] = []
        # _previous[i][j] = index of the planet before j on the shortest route
        # from i to j, or -1 for j == i. Routes are rebuilt from it on demand.
        self._previous: List[List[int]] = []
        self._by_proximity: Dict["Planet", List["Planet"]] = {}
        # (planet count, mean) for mean_pair_distance; see that method.
        self._mean_pair_distance: Optional[Tuple[int, float]] = None
        self._lane_count = -1  # lane count the matrix was built from
        # Static commodity facts.
        self._tradeable: Optional[List["CommodityDefinition"]] = None
        self._fuel: Optional["CommodityDefinition"] = None
        self._fuel_resolved = False
        # Market-derived facts, cleared by refresh_market_facts.
        self._fuel_purchasable: Dict["Planet", bool] = {}
        self._fuel_ask_depth: Dict["Planet", int] = {}
        self._nearest_fuel_distance: Dict["Planet", Optional[float]] = {}
        self._fuel_station: Dict["Planet", bool] = {}
        self._nearest_station_distance: Dict["Planet", Optional[float]] = {}
        self._fuel_scan: Optional["_FuelScan"] = None
        self._trade_index: Optional[_TradeSignalIndex] = None
        # Turn the market-fact snapshot belongs to. None means never refreshed
        # or force-refreshed outside a turn.
        self._facts_turn: Optional[int] = None

    # ------------------------------------------------------------------
    # Cache lifecycle
    # ------------------------------------------------------------------

    def refresh_market_facts(self, turn: Optional[int] = None) -> None:
        """Drop market-derived caches so the next queries see the live books.

        With ``turn`` given, as ship brains call it, the refresh is a no-op
        when the caches were already refreshed for that turn: market facts
        are a per-turn shared snapshot, built once and reused by every ship
        planning that turn. Order books only change between turns via
        deferred end-of-turn matching, so within a turn the snapshot stays a
        valid superset filter. Calling without ``turn`` forces a refresh;
        tests and ad-hoc probes use this. Geometry and commodity-registry
        caches are unaffected either way.
        """
        if turn is not None and turn == self._facts_turn:
            return
        self._facts_turn = turn
        self._fuel_purchasable.clear()
        self._fuel_ask_depth.clear()
        self._nearest_fuel_distance.clear()
        self._fuel_station.clear()
        self._nearest_station_distance.clear()
        self._fuel_scan = None
        self._trade_index = None

    # ------------------------------------------------------------------
    # Geometry (static after setup)
    # ------------------------------------------------------------------

    def distance(self, a: "Planet", b: "Planet") -> float:
        """Length of the shortest star-lane route between two planets."""
        i, j = self._indices(a, b)
        return self._distances[i][j]

    def route(self, a: "Planet", b: "Planet") -> List["Planet"]:
        """Planets along the shortest lane route from ``a`` to ``b``.

        The list starts with ``a`` and ends with ``b``, or is ``[a]`` when
        they are the same planet. Consecutive entries are always joined by a
        lane.
        """
        i, j = self._indices(a, b)
        planets = self._sim.planets
        path = [j]
        while path[-1] != i:
            path.append(self._previous[i][path[-1]])
        path.reverse()
        return [planets[k] for k in path]

    def _indices(self, a: "Planet", b: "Planet") -> Tuple[int, int]:
        """Matrix indices of two planets, rebuilding geometry if needed."""
        index = self._planet_index
        i = index.get(a)
        j = index.get(b)
        if (
            i is None
            or j is None
            or len(index) != len(self._sim.planets)
            or len(self._sim.star_lanes) != self._lane_count
        ):
            self._rebuild_geometry()
            i = self._planet_index[a]
            j = self._planet_index[b]
        return i, j

    def nearest_other_distance(self, planet: "Planet") -> Optional[float]:
        """Distance to the closest other planet, or None if it is alone."""
        others = self.planets_by_proximity(planet)
        return self.distance(planet, others[0]) if others else None

    def mean_pair_distance(self) -> float:
        """Mean shortest lane route over all distinct planet pairs.

        The single number that says how big the galaxy is for a ship: fuel
        burn, and so the capital a trade ties up, scales with it. Returns
        0.0 for a galaxy with fewer than two planets.
        """
        planets = self._sim.planets
        count = len(planets)
        if count < 2:
            return 0.0
        # Summing the pair matrix is O(n^2) and every ship asks for this
        # number every turn, so cache it. Planet geometry is fixed after
        # setup; the count guards the one case that changes it.
        cached = self._mean_pair_distance
        if cached is not None and cached[0] == count:
            return cached[1]
        self._indices(planets[0], planets[0])  # ensure the matrix is built
        total = sum(
            self._distances[i][j] for i in range(count) for j in range(i + 1, count)
        )
        mean = total / (count * (count - 1) / 2)
        self._mean_pair_distance = (count, mean)
        return mean

    def planets_by_proximity(self, planet: "Planet") -> List["Planet"]:
        """All other planets sorted nearest-first from ``planet``."""
        cached = self._by_proximity.get(planet)
        if cached is not None and len(cached) == len(self._sim.planets) - 1:
            return cached
        ordered = sorted(
            (p for p in self._sim.planets if p is not planet),
            key=lambda other: self.distance(planet, other),
        )
        self._by_proximity[planet] = ordered
        return ordered

    def _rebuild_geometry(self) -> None:
        """Rebuild the all-pairs shortest-route matrix over the star lanes.

        One Dijkstra per planet over the lane graph, O(P * L log P).

        Raises:
            ValueError: If some planet cannot reach every other planet. The
                galaxy generator guarantees connectivity, so this means a
                hand-built world forgot to add lanes.
        """
        planets = self._sim.planets
        lanes = self._sim.star_lanes
        count = len(planets)
        index = {planet: i for i, planet in enumerate(planets)}
        adjacency: List[List[Tuple[int, float]]] = [[] for _ in planets]
        for lane in lanes.lanes:
            ia, ib = index[lane.a], index[lane.b]
            adjacency[ia].append((ib, lane.length))
            adjacency[ib].append((ia, lane.length))

        infinity = float("inf")
        distances: List[List[float]] = []
        previous: List[List[int]] = []
        for source in range(count):
            dist = [infinity] * count
            prev = [-1] * count
            dist[source] = 0.0
            frontier = [(0.0, source)]
            while frontier:
                d, node = heapq.heappop(frontier)
                if d > dist[node]:
                    continue
                for nxt, length in adjacency[node]:
                    candidate = d + length
                    if candidate < dist[nxt]:
                        dist[nxt] = candidate
                        prev[nxt] = node
                        heapq.heappush(frontier, (candidate, nxt))
            unreachable = [planets[k].name for k in range(count) if dist[k] == infinity]
            if unreachable:
                raise ValueError(
                    f"star-lane network is disconnected: {planets[source].name} "
                    f"cannot reach {', '.join(unreachable[:5])}"
                    + (" ..." if len(unreachable) > 5 else "")
                )
            distances.append(dist)
            previous.append(prev)

        self._planet_index = index
        self._distances = distances
        self._previous = previous
        self._lane_count = len(lanes)
        self._by_proximity.clear()
        self._mean_pair_distance = None

    # ------------------------------------------------------------------
    # Commodity facts (static after setup)
    # ------------------------------------------------------------------

    def tradeable_commodities(self) -> List["CommodityDefinition"]:
        """All transportable commodities. The registry is fixed after setup."""
        if self._tradeable is None:
            self._tradeable = [
                c
                for c in self._sim.commodity_registry.all_commodities()
                if c.transportable
            ]
        return self._tradeable

    def fuel_commodity(self) -> Optional["CommodityDefinition"]:
        """The nova_fuel commodity, or None if it is not defined."""
        if not self._fuel_resolved:
            self._fuel = self._sim.commodity_registry.get_commodity("nova_fuel")
            self._fuel_resolved = True
        return self._fuel

    # ------------------------------------------------------------------
    # Market-derived facts (cached until refresh_market_facts)
    # ------------------------------------------------------------------

    def fuel_purchasable_at(self, planet: "Planet") -> bool:
        """Whether nova_fuel can be bought at ``planet`` right now.

        True only with a live resting ask. Actors trade before ships each
        turn, so the book a ship reads already holds this turn's supply: an
        empty ask side means there is nothing to lift, whatever traded
        earlier. Recent volume used to count as availability, but the trade
        that shows up in the window is usually the one that emptied the
        book, which let ships fly into planets that could not refuel them.
        Use :meth:`fuel_traded_recently` where the softer signal is wanted.
        """
        cached = self._fuel_purchasable.get(planet)
        if cached is not None:
            return cached
        fuel = self.fuel_commodity()
        if fuel is None:
            result = False
        else:
            _, ask = planet.market.get_bid_ask_spread(fuel)
            result = ask is not None and ask > 0
        self._fuel_purchasable[planet] = result
        return result

    def fuel_traded_recently(self, planet: "Planet") -> bool:
        """Whether fuel changed hands at ``planet`` in the recent window.

        A weaker signal than :meth:`fuel_purchasable_at`: it says a supplier
        exists here who may answer a standing bid, not that fuel can be
        lifted this turn. Only last-resort choices, such as picking somewhere
        to sit out a fuel shortage, may rely on it; plan gates must not.
        """
        fuel = self.fuel_commodity()
        if fuel is None:
            return False
        market = planet.market
        if not market.has_price_signal(fuel):
            return False
        recent = market.volume_history.get(fuel, [])[-FUEL_MARKET_RECENCY_TURNS:]
        return any(v > 0 for v in recent)

    def fuel_ask_depth_at(self, planet: "Planet") -> int:
        """Units of fuel resting on the ask side at ``planet``.

        How much a ship arriving now could actually buy. Top of book says
        only that some fuel is for sale; a single unit does not refill a
        tank. Cached with the other market facts for the turn.
        """
        cached = self._fuel_ask_depth.get(planet)
        if cached is not None:
            return cached
        fuel = self.fuel_commodity()
        if fuel is None:
            depth = 0
        else:
            depth = sum(
                order.quantity
                for order in planet.market.sell_orders.get(fuel, [])
                if not order.cancelled
            )
        self._fuel_ask_depth[planet] = depth
        return depth

    def nearest_fuel_source_distance(self, planet: "Planet") -> Optional[float]:
        """Distance from ``planet`` to the nearest other fuel-selling planet.

        Returns None when fuel is purchasable nowhere else in the galaxy.
        Fuel cost is monotone in distance, so the nearest source also
        minimizes any ship's escape-fuel requirement.
        """
        if planet in self._nearest_fuel_distance:
            return self._nearest_fuel_distance[planet]
        result: Optional[float] = None
        for other in self.planets_by_proximity(planet):
            if self.fuel_purchasable_at(other):
                result = self.distance(planet, other)
                break
        self._nearest_fuel_distance[planet] = result
        return result

    def fuel_station_at(self, planet: "Planet") -> bool:
        """Whether ``planet`` is a fuel market a ship can plan around.

        True when at least :data:`FUEL_STATION_MIN_DEPTH` units rest on the
        ask side, from any seller, and the best ask is no more than
        :data:`FUEL_STATION_MAX_PRICE_MULT` times
        :meth:`fuel_value_reference`. With no reference, depth alone decides.

        Stricter than :meth:`fuel_purchasable_at`, which answers whether a
        single unit can be lifted right now. A ship that commits its escape
        fuel to reach a planet needs the planet to be able to fill the tank
        at a price it can pay, so a one-unit trickle ask or a spiked book
        does not count.
        """
        cached = self._fuel_station.get(planet)
        if cached is not None:
            return cached
        fuel = self.fuel_commodity()
        result = False
        if (
            fuel is not None
            and self.fuel_ask_depth_at(planet) >= FUEL_STATION_MIN_DEPTH
        ):
            _, ask = planet.market.get_bid_ask_spread(fuel)
            if ask is not None and ask > 0:
                reference = self.fuel_value_reference()
                result = (
                    reference is None or ask <= FUEL_STATION_MAX_PRICE_MULT * reference
                )
        self._fuel_station[planet] = result
        return result

    def nearest_fuel_station_distance(self, planet: "Planet") -> Optional[float]:
        """Distance from ``planet`` to the nearest other fuel station.

        Returns None when no other planet in the galaxy is a station. Fuel
        cost is monotone in distance, so the nearest station also minimizes
        any ship's escape-fuel requirement.
        """
        if planet in self._nearest_station_distance:
            return self._nearest_station_distance[planet]
        result: Optional[float] = None
        for other in self.planets_by_proximity(planet):
            if self.fuel_station_at(other):
                result = self.distance(planet, other)
                break
        self._nearest_station_distance[planet] = result
        return result

    def fuel_station_planets(self) -> List["Planet"]:
        """Every planet that is a fuel station right now."""
        return [
            planet
            for planet, _ in self._fuel_market_scan().asks
            if self.fuel_station_at(planet)
        ]

    def fuel_ask_planets(self) -> List[Tuple["Planet", int]]:
        """Every planet with a resting fuel ask, as (planet, ask) pairs."""
        return self._fuel_market_scan().asks

    def producer_fuel_ask_planets(self) -> List[Tuple["Planet", int, int]]:
        """Planets whose fuel supply is not a dealer's, as (planet, ask, depth).

        The ask is the best non-dealer ask on the planet and the depth is how
        many units all non-dealer asks there hold. "Dealer" means a SERVICE
        actor, i.e. a market maker or a spaceport operator: its ask is a markup
        on the same delivered-price anchor this scan feeds, so treating it as
        an independent supply signal closes a price loop. A ship's tank fuel is
        real supply and counts as a producer ask.
        """
        return self._fuel_market_scan().producer_asks

    def cheapest_fuel_ask(self) -> Optional[int]:
        """The lowest resting fuel ask anywhere, or None if there is none."""
        return self._fuel_market_scan().cheapest_ask

    def fuel_value_reference(self) -> Optional[float]:
        """Typical believable fuel valuation across the galaxy.

        The median, over the planets that have believable evidence, of each
        planet's own fuel valuation. A planet is believable when it has a
        real price signal, in which case it contributes its 30-day average
        price, or failing that when a non-dealer ask rests there with more
        than a single unit of depth, in which case it contributes that ask.
        Planets whose only evidence is a one-unit probe ask, or a dealer's
        markup on this same reference, contribute nothing. Returns None when
        no planet has any believable evidence.

        This was a galaxy-wide *minimum* over asks and averages, meant to
        stop a ship filling its tank at a local panic price. With a hundred
        planets that minimum is pinned by whoever is cheapest anywhere: a
        market maker's one-unit discovery ask or one washed-out outlier held
        it near 17 while fuel actually traded around 31 and the median ask
        sat near 42. Every planet then looked scarcity-priced, no ship ever
        bunkered, tanks ran at a handful of units, and each plan had to fund
        its whole round trip from the origin ask. A central statistic keeps
        the original protection - a genuine spike is far above the median -
        without declaring the whole galaxy expensive.
        """
        return self._fuel_market_scan().reference

    def exportable_commodities(
        self, planet: "Planet"
    ) -> FrozenSet["CommodityDefinition"]:
        """Commodities plausibly acquirable at ``planet`` right now.

        A commodity qualifies with a resting ask or an active local flow,
        meaning a real price signal plus recent traded volume. This is a
        superset filter: plan evaluation still verifies exact prices, but
        commodities outside this set cannot be acquired and can be skipped.
        Served from the per-turn trade-signal index.
        """
        return self._trade_signal_index().exportable_by_planet.get(planet, frozenset())

    def has_any_trade_signal(self) -> bool:
        """Whether any commodity has both an export source and a demand planet.

        False means the galaxy is cold, typically during the pre-market
        bootstrap: no trade plan can exist anywhere, so ship planning can
        early-out for the turn instead of surveying every market.
        """
        return self._trade_signal_index().has_any_trade_signal()

    def candidate_destinations(
        self, origin: "Planet", commodity: "CommodityDefinition"
    ) -> Tuple["Planet", ...]:
        """Shortlist of destination planets worth evaluating for ``commodity``.

        The union of the :data:`DESTINATION_TOP_K` demand planets with the
        highest demand value, the better of best resting bid and recent
        clearing price, and the :data:`DESTINATION_NEAREST_M` demand planets
        nearest ``origin``, excluding ``origin`` itself. When the commodity
        has at most K + M demand planets the shortlist is all of them, so
        small galaxies keep exhaustive-survey behavior. Results are memoized
        in the per-turn index and shared by every ship.
        """
        index = self._trade_signal_index()
        memo_key = (origin, commodity)
        cached = index.candidate_memo.get(memo_key)
        if cached is not None:
            return cached
        pool = [
            planet
            for planet in index.demand_ranked.get(commodity, ())
            if planet is not origin
        ]
        if len(pool) <= DESTINATION_TOP_K + DESTINATION_NEAREST_M:
            result = tuple(pool)
        else:
            chosen = pool[:DESTINATION_TOP_K]
            chosen_set = set(chosen)
            demand_planets = index.demand_planets.get(commodity, frozenset())
            found_near = 0
            for planet in self.planets_by_proximity(origin):
                if found_near >= DESTINATION_NEAREST_M:
                    break
                if planet in demand_planets:
                    found_near += 1
                    if planet not in chosen_set:
                        chosen.append(planet)
                        chosen_set.add(planet)
            result = tuple(chosen)
        index.candidate_memo[memo_key] = result
        return result

    # ------------------------------------------------------------------
    # Fuel pricing (market-derived)
    # ------------------------------------------------------------------

    def fuel_bid_ceiling(self) -> int:
        """Highest price any fuel bid should carry.

        :data:`FUEL_BID_CEILING_MULT` times :meth:`fuel_value_reference`, or
        :data:`FUEL_BID_CEILING_UNBOUNDED` when no planet has believable fuel
        evidence and there is therefore no spike to measure. A seller hits the best resting bid at the bid price, so an
        unbounded bid buys its own escalation: the fill becomes the next
        average price and the next bid is higher still. Measured over 400
        turns that loop took one planet's fuel price from 363 to 1020 a unit
        with no change in real supply.
        """
        reference = self.fuel_value_reference()
        if reference is None:
            return FUEL_BID_CEILING_UNBOUNDED
        return max(1, math.ceil(FUEL_BID_CEILING_MULT * reference))

    def fuel_delivery_bid_price(self, planet: "Planet", quantity: int) -> int:
        """Price for a standing fuel bid at ``planet`` that makes delivery pay.

        Anchors on the cheapest *fillable producer* ask anywhere else in the
        galaxy plus the deliverer's round-trip burn at worst-case efficiency,
        amortized over ``quantity``, marked up by :data:`FUEL_BID_MARGIN` so
        the delivery clears the arbitrage threshold a trader applies. With no
        ask anywhere, falls back to :meth:`local_fuel_reference_price`.

        Fillable means the source's resting ask depth covers ``quantity``. Top
        of book alone is a bad anchor: market makers post one-unit discovery
        asks at a few credits, and a galaxy-wide minimum over those prices a
        40-unit delivery as if it could be bought for 2 credits a unit, which
        no deliverer would ever accept.

        Producer means the ask was not posted by a SERVICE actor. A dealer's
        ask is a markup on its own cost basis, and that basis comes from
        buying at exactly this delivered price: anchoring on it closes a
        feedback loop where each operator's bid raises the next operator's
        anchor, which raised the galaxy fuel VWAP sixfold over a hundred turns
        with no change in real supply. A ship's tank fuel is not a dealer ask
        and still anchors, because it is real supply someone chose to carry.

        Preference order: producer asks deep enough to fill the order, then
        producer asks of any depth, then - only when the galaxy holds no
        producer ask at all - any ask, deep ones first, and finally the local
        reference price.

        Lives on the navigator rather than on a ship because it is a fact
        about the galaxy's fuel geography, and both a stranded ship and a
        spaceport operator need the same number.

        Args:
            planet: Where the bid would rest, i.e. the delivery destination.
            quantity: Units bid for; the round trip is amortized over it, so
                a bigger bid tolerates a lower price per unit. Also the depth
                a source must have to count as fillable.

        Returns:
            A price of at least 1.
        """
        # Imported here, not at module scope: ship.py imports this module, so
        # a top-level import would be circular. Only the shared burn formula
        # is wanted, and it is a staticmethod.
        from spacesim2.core.ship import FUEL_MILLI, Ship

        if self.fuel_commodity() is None:
            return FUEL_BID_FALLBACK_FLOOR

        needed_depth = max(quantity, 1)

        def delivered_cost(source: "Planet", ask: int) -> float:
            distance = self.distance(source, planet)
            # Millifuel for the leg, one ceiling, as Ship.fuel_required does
            # it; divided by FUEL_MILLI so the result stays in credits.
            leg_fuel_milli = math.ceil(
                Ship.calculate_fuel_needed(distance) / DELIVERER_WORST_FUEL_EFFICIENCY
            )
            leg_fuel_units = leg_fuel_milli / FUEL_MILLI
            return ask + (2 * leg_fuel_units * ask) / needed_depth

        producer_fillable: Optional[float] = None
        producer_any: Optional[float] = None
        for source, ask, depth in self.producer_fuel_ask_planets():
            if source is planet:
                continue
            cost = delivered_cost(source, ask)
            if producer_any is None or cost < producer_any:
                producer_any = cost
            if depth >= needed_depth and (
                producer_fillable is None or cost < producer_fillable
            ):
                producer_fillable = cost

        best_delivered_cost = (
            producer_fillable if producer_fillable is not None else producer_any
        )

        if best_delivered_cost is None:
            # No producer sells fuel anywhere. Dealers are then the only
            # supply there is, so anchoring on them beats having no anchor.
            best_fillable: Optional[float] = None
            best_any: Optional[float] = None
            for source, ask in self.fuel_ask_planets():
                if source is planet:
                    continue
                cost = delivered_cost(source, ask)
                if best_any is None or cost < best_any:
                    best_any = cost
                if self.fuel_ask_depth_at(source) >= needed_depth and (
                    best_fillable is None or cost < best_fillable
                ):
                    best_fillable = cost
            best_delivered_cost = (
                best_fillable if best_fillable is not None else best_any
            )

        if best_delivered_cost is not None:
            price = max(1, math.ceil(best_delivered_cost * (1.0 + FUEL_BID_MARGIN)))
            return min(price, self.fuel_bid_ceiling())

        # No ask anywhere: fall back to what a local producer would need.
        return min(self.local_fuel_reference_price(planet), self.fuel_bid_ceiling())

    def local_fuel_reference_price(self, planet: "Planet") -> int:
        """Scarcity-escalated price a local fuel producer would plausibly take.

        Anchors on a real local signal when one exists, never the fabricated
        default average, and escalates with the market's scarcity pressure,
        which grows each turn local demand goes unmet.

        Args:
            planet: The market to read.

        Returns:
            A price of at least 1.
        """
        fuel_commodity = self.fuel_commodity()
        if fuel_commodity is None:
            return FUEL_BID_FALLBACK_FLOOR
        market = planet.market
        reference = float(FUEL_BID_FALLBACK_FLOOR)
        if market.has_price_signal(fuel_commodity):
            reference = max(reference, float(market.get_avg_price(fuel_commodity)))
        escalated = reference * (1.0 + market.scarcity_pressure_for(fuel_commodity))
        return max(1, min(math.ceil(escalated), self.fuel_bid_ceiling()))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _trade_signal_index(self) -> _TradeSignalIndex:
        """The turn's trade-signal index, built lazily on first use.

        One O(planets x commodities) pass over every market collecting the
        per-planet exportable summaries and, per commodity, the export
        planets and the demand planets ranked by demand value: the better of
        the best resting bid and the recent clearing price, when a real price
        signal backs it. Cached until the next :meth:`refresh_market_facts`,
        so for the rest of the turn.
        """
        if self._trade_index is not None:
            return self._trade_index
        tradeable = self.tradeable_commodities()
        exportable_by_planet: Dict["Planet", FrozenSet["CommodityDefinition"]] = {}
        export_lists: Dict["CommodityDefinition", List["Planet"]] = {
            commodity: [] for commodity in tradeable
        }
        # Per commodity, (negated demand value, planet) rows. Sorting is
        # stable, so ties keep simulation planet order without comparing
        # Planet objects.
        demand_rows: Dict["CommodityDefinition", List[Tuple[float, "Planet"]]] = {
            commodity: [] for commodity in tradeable
        }
        for planet in self._sim.planets:
            market = planet.market
            exportable: List["CommodityDefinition"] = []
            for commodity in tradeable:
                best_bid, ask = market.get_bid_ask_spread(commodity)
                has_signal = market.has_price_signal(commodity)
                if (ask is not None and ask > 0) or (
                    has_signal
                    and any(
                        v > 0
                        for v in market.volume_history.get(commodity, [])[
                            -FLOW_RECENCY_TURNS:
                        ]
                    )
                ):
                    exportable.append(commodity)
                    export_lists[commodity].append(planet)
                if best_bid is not None or has_signal:
                    value = float(best_bid or 0)
                    if has_signal:
                        value = max(value, float(market.get_avg_price(commodity)))
                    demand_rows[commodity].append((-value, planet))
            exportable_by_planet[planet] = frozenset(exportable)
        demand_ranked: Dict["CommodityDefinition", Tuple["Planet", ...]] = {}
        demand_planets: Dict["CommodityDefinition", FrozenSet["Planet"]] = {}
        for commodity, rows in demand_rows.items():
            rows.sort(key=lambda row: row[0])
            demand_ranked[commodity] = tuple(planet for _, planet in rows)
            demand_planets[commodity] = frozenset(demand_ranked[commodity])
        self._trade_index = _TradeSignalIndex(
            exportable_by_planet=exportable_by_planet,
            export_planets={
                commodity: frozenset(planets)
                for commodity, planets in export_lists.items()
            },
            demand_ranked=demand_ranked,
            demand_planets=demand_planets,
        )
        return self._trade_index

    def _fuel_market_scan(self) -> "_FuelScan":
        """One sweep over every market collecting galaxy-wide fuel facts.

        Collects the planets with resting asks, the best non-dealer ask and
        non-dealer depth per planet, the cheapest ask, and the median
        believable valuation. Cached until the next refresh.
        """
        if self._fuel_scan is not None:
            return self._fuel_scan
        # Imported here, not at module scope, to keep this module importable
        # from anywhere in core without an import cycle through actor.py.
        from spacesim2.core.actor import Actor, ActorType

        asks: List[Tuple["Planet", int]] = []
        producer_asks: List[Tuple["Planet", int, int]] = []
        cheapest_ask: Optional[int] = None
        # One believable valuation per planet; see fuel_value_reference.
        believable: List[float] = []
        fuel = self.fuel_commodity()
        if fuel is not None:
            for planet in self._sim.planets:
                market = planet.market
                best_producer: Optional[int] = None
                producer_depth = 0
                _, ask = market.get_bid_ask_spread(fuel)
                if ask is not None and ask > 0:
                    asks.append((planet, ask))
                    if cheapest_ask is None or ask < cheapest_ask:
                        cheapest_ask = ask
                    for order in market.sell_orders.get(fuel, []):
                        if order.cancelled:
                            continue
                        seller = order.actor
                        if (
                            isinstance(seller, Actor)
                            and seller.actor_type is ActorType.SERVICE
                        ):
                            continue
                        producer_depth += order.quantity
                        if best_producer is None or order.price < best_producer:
                            best_producer = order.price
                    if best_producer is not None:
                        producer_asks.append((planet, best_producer, producer_depth))
                avg_30 = (
                    market.get_30_day_average_price(fuel)
                    if market.has_price_signal(fuel)
                    else 0.0
                )
                if avg_30 > 0:
                    # Traded evidence beats a resting quote.
                    believable.append(float(avg_30))
                elif best_producer is not None and producer_depth > 1:
                    believable.append(float(best_producer))
        reference: Optional[float] = None
        if believable:
            believable.sort()
            middle = len(believable) // 2
            reference = (
                believable[middle]
                if len(believable) % 2
                else (believable[middle - 1] + believable[middle]) / 2.0
            )
        self._fuel_scan = _FuelScan(
            asks=asks,
            producer_asks=producer_asks,
            cheapest_ask=cheapest_ask,
            reference=reference,
        )
        return self._fuel_scan


def leg_fuel_cost(
    navigator: Navigator, origin: "Planet", destination: "Planet"
) -> float:
    """Credits of fuel one leg from ``origin`` to ``destination`` burns.

    The baseline burn at efficiency 1.0, valued at the galaxy's typical fuel
    valuation, so an efficient ship keeps the difference. Before anything has
    traded there is no reference and :data:`FUEL_BID_FALLBACK_FLOOR` stands
    in, the same fabricated-price guard the fuel bids use.

    This is what a leg costs a carrier, and it is the base of every price
    paid to one: government freight in ``core/government.py`` and passage in
    ``core/migration.py`` each add their own margin to it.
    """
    from spacesim2.core.ship import FUEL_MILLI, Ship

    reference = navigator.fuel_value_reference()
    if reference is None:
        reference = float(FUEL_BID_FALLBACK_FLOOR)
    # calculate_fuel_needed is millifuel; the reference prices a market unit.
    fuel_needed = Ship.calculate_fuel_needed(navigator.distance(origin, destination))
    return fuel_needed * reference / FUEL_MILLI


_navigators: "WeakKeyDictionary[Simulation, Navigator]" = WeakKeyDictionary()


def get_navigator(sim: "Simulation") -> Navigator:
    """Return the shared :class:`Navigator` for ``sim``, creating it on demand.

    Kept in a weak registry so a navigator lives as long as its simulation,
    without the simulation knowing about navigation.
    """
    navigator = _navigators.get(sim)
    if navigator is None:
        navigator = Navigator(sim)
        _navigators[sim] = navigator
    return navigator
