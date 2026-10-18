"""Migration: the mechanics of an actor moving between planets.

The split is deliberate. Brains decide whether, when, and where an actor
moves (``ActorBrain.decide_migration``); this module and the simulation own
what a move does. Nothing here reads a drive or a price to decide anything.

Flow:

1. During its turn an actor's brain returns a ``MigrationRequest`` or
   ``NO_MIGRATION``. ``Actor.take_turn`` stores the result on
   ``actor.migration_request``. The actor phase is threaded by planet, so
   nothing moves here.
2. After the ship phase and before market matching,
   ``run_migration_phase`` turns each standing request into an open passage
   contract on the origin planet's board, re-prices one the actor has
   raised its offer on, and cancels one the actor no longer wants. Nobody
   moves here either.
3. A ship brain accepts the contract, and ``Ship.start_journey`` loads the
   passenger: the actor leaves its planet and ``sim.actors``, travels with
   nothing, and the fare is paid to the carrier.
4. ``Ship.update_journey`` delivers on arrival, which calls
   ``relocate_actor``: the actor joins the destination with the land the
   load claimed for it and a fresh brain cache, and its brain's
   ``on_relocated`` hook runs.

A passenger aboard a ship is in neither ``sim.actors`` nor any
``planet.actors``, which keeps the ``core/parallel.py`` invariant that the
two cover each other. Its ``planet`` still points at the origin;
``actor.in_transit`` is the authoritative flag.

``refresh_planet_stats`` builds one ``PlanetStats`` per planet at the start
of each turn so brains can score destinations without walking every actor.
The stats are mechanical aggregates only; how to weigh them is brain logic.
"""

import math
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Union

from spacesim2.core.contracts import (
    Contract,
    ContractStatus,
    PassengerPayload,
)
from spacesim2.core.navigation import leg_fuel_cost

# ``Actor`` imports this module for ``NO_MIGRATION``, so anything reachable
# from ``core.actor`` is imported inside the function that needs it.

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.land import Land
    from spacesim2.core.navigation import Navigator
    from spacesim2.core.planet import Planet
    from spacesim2.core.simulation import Simulation


# Fraction above the leg's fuel cost the fare estimate offers, and the floor
# under it for a route whose fuel is negligible. This is only the estimate a
# brain opens its offer at; what a passage actually costs is the advance on
# the contract a ship agrees to fly, and that goes to the carrier.
#
# The margin is double a government job's 0.25. A job is a ship's fallback
# when it has nothing better, so it only has to beat idling; a passenger is
# asking a ship to fly a destination the ship did not choose, and the fare
# has to cover the maintenance the trip risks as well as its fuel.
PASSAGE_FARE_MARGIN = 0.5
PASSAGE_FARE_MINIMUM = 20

# Turns a passage contract stands before the board expires it. Long enough
# that a ship several lanes away can route to the planet, short enough that
# an offer nobody takes is re-priced rather than left standing forever.
PASSAGE_CONTRACT_TTL = 30

# MIGRANT_CARGO_UNITS, the hold space one migrant occupies, lives in
# core/contracts.py with the passage contract that carries the passenger.

# Statuses in which a posted contract is still the actor's live passage.
_LIVE_STATUSES = (
    ContractStatus.OPEN,
    ContractStatus.ACCEPTED,
    ContractStatus.LOADED,
)


@dataclass(frozen=True)
class MigrationRequest:
    """A brain's standing offer of ``fare_offer`` for passage to ``destination``.

    Restated every turn while the intent holds. The offer is what the actor
    is bidding now; the brain raises it while nobody takes the contract.
    """

    destination: "Planet"
    fare_offer: int


class NoMigration:
    """Sentinel: the brain wants the actor to stay."""

    def __repr__(self) -> str:
        return "NO_MIGRATION"


NO_MIGRATION = NoMigration()

MigrationDecision = Union[MigrationRequest, NoMigration]


@dataclass(frozen=True)
class PlanetStats:
    """Per-planet aggregates over regular residents, rebuilt each turn.

    ``free_land_mean`` maps a resource id to the mean coefficient of the
    unclaimed lands, the expected draw for a newcomer; empty when the pool
    is empty. ``median_need_debt`` is the median over residents of each
    resident's maximum need-drive ``debt``. ``median_prosperity`` is the
    median ``prosperity_index``. Medians are 0.0 with no residents.
    """

    population: int
    free_land_count: int
    free_land_mean: Mapping[str, float]
    median_need_debt: float
    median_prosperity: float
    median_money: float


@dataclass(frozen=True)
class MigrationEvent:
    """One passenger boarding a ship, for the run log and post-hoc analysis."""

    turn: int
    actor_name: str
    origin_name: str
    destination_name: str
    fare: int


def passage_fare(
    navigator: "Navigator", origin: "Planet", destination: "Planet"
) -> int:
    """Estimated fare for passage from ``origin`` to ``destination``.

    The leg's fuel cost plus ``PASSAGE_FARE_MARGIN``, floored at
    ``PASSAGE_FARE_MINIMUM``: the same pricing a government job gets, at a
    wider margin. Pricing the fare off anything but what the leg costs the
    carrier leaves the passage break-even or worse, and a ship that values
    a passenger group at no profit flies something else.

    The opening offer a brain posts, not a price core charges: a ship is
    paid whatever advance it agrees to fly for.
    """
    return max(
        PASSAGE_FARE_MINIMUM,
        math.ceil(
            leg_fuel_cost(navigator, origin, destination) * (1 + PASSAGE_FARE_MARGIN)
        ),
    )


def refresh_planet_stats(sim: "Simulation") -> Dict["Planet", PlanetStats]:
    """Rebuild ``sim.planet_stats`` for every planet and return it.

    Runs at the top of every turn over every regular resident of every
    planet, so it walks the population once and sorts only what a median
    needs. ``free_land_mean`` is one pass over the unclaimed pool.
    """
    from spacesim2.core.drives.prosperity_drive import prosperity_index

    stats: Dict["Planet", PlanetStats] = {}
    for planet in sim.planets:
        debts: List[float] = []
        prosperities: List[float] = []
        monies: List[float] = []
        for actor in planet.actors:
            if not actor.claims_land:
                continue
            worst_debt = 0.0
            for drive in actor.drives:
                if drive.WELLBEING and drive.metrics.debt > worst_debt:
                    worst_debt = drive.metrics.debt
            debts.append(worst_debt)
            prosperities.append(prosperity_index(actor))
            monies.append(float(actor.money))

        free_lands = planet.free_lands
        sums: Dict[str, float] = {}
        for land in free_lands:
            for resource, value in land.coefficients.items():
                sums[resource] = sums.get(resource, 0.0) + value
        count = len(free_lands)
        free_land_mean = (
            {resource: total / count for resource, total in sums.items()}
            if count
            else {}
        )

        stats[planet] = PlanetStats(
            population=len(debts),
            free_land_count=count,
            free_land_mean=free_land_mean,
            median_need_debt=median(debts) if debts else 0.0,
            median_prosperity=median(prosperities) if prosperities else 0.0,
            median_money=median(monies) if monies else 0.0,
        )

    sim.planet_stats = stats
    return stats


def run_migration_phase(sim: "Simulation") -> None:
    """Bring every actor's passage contract in line with its standing request.

    Nobody moves here. The phase only keeps the boards honest: a request
    with no live contract posts one, a raised offer re-prices the open
    contract, a re-aimed destination replaces it, and a withdrawn request
    takes it down. A contract a ship has already taken on is left alone;
    pulling one out from under a carrier is what stranding is for.
    """
    for actor in list(sim.actors):
        _sync_passage_contract(sim, actor)


def _sync_passage_contract(sim: "Simulation", actor: "Actor") -> None:
    """Post, re-price, replace, or withdraw one actor's passage contract."""
    live = live_passage_contract(actor)
    request = actor.migration_request

    if not isinstance(request, MigrationRequest):
        if live is not None and live.status is ContractStatus.OPEN:
            live.origin.contracts.cancel(live)
        return

    origin = actor.planet
    if origin is None or request.destination is origin:
        return

    # A re-price keeps the turn the actor first asked for passage. The offer
    # climbs by a credit or two a turn, so the contract is replaced most
    # turns of the climb, and a reset clock would measure the last re-price
    # rather than how long the actor has been waiting.
    posted_turn = sim.current_turn
    is_new = live is None
    if live is not None:
        if live.status is not ContractStatus.OPEN:
            return
        # Money the cancel would hand back is money the repost can offer.
        affordable = min(request.fare_offer, actor.money + live.total_payment)
        if live.destination is request.destination and affordable <= live.advance:
            return
        posted_turn = live.posted_turn
        live.origin.contracts.cancel(live)

    offer = min(request.fare_offer, actor.money)
    if offer <= 0:
        return

    contract = Contract(
        poster=actor,
        origin=origin,
        destination=request.destination,
        payload=PassengerPayload(actor=actor),
        advance=offer,
        on_delivery=0,
        posted_turn=posted_turn,
        expires_turn=sim.current_turn + PASSAGE_CONTRACT_TTL,
    )
    origin.contracts.post(contract)
    actor.passage_contract = contract
    # A re-price or re-aim replaces a contract; only a first posting is a
    # new contract for the summary's count.
    if is_new:
        sim.contracts_posted += 1


def live_passage_contract(actor: "Actor") -> Optional[Contract]:
    """The actor's passage contract while it is still running, else None.

    ``actor.passage_contract`` is never cleared: the contract's own status
    says whether it is still live, so expiry, cancellation, and delivery
    need no bookkeeping anywhere else, and the brain can read the ending
    off the last contract it posted.
    """
    contract = actor.passage_contract
    if contract is None or contract.status not in _LIVE_STATUSES:
        return None
    return contract


def record_passenger_departure(actor: "Actor", contract: Contract, fare: int) -> None:
    """Count a passenger boarding its ship: the departure, event, and wait.

    Called from ``contracts.load_contract``, the one moment a migration is
    certain: the actor has left its planet and the carrier has been paid.
    """
    sim = actor.sim
    sim.migration_departures += 1
    sim.passage_wait_turns.append(sim.current_turn - contract.posted_turn)
    sim.migration_log.append(
        MigrationEvent(
            turn=sim.current_turn,
            actor_name=actor.name,
            origin_name=contract.origin.name,
            destination_name=contract.destination.name,
            fare=fare,
        )
    )


def _cancel_all_orders(actor: "Actor", planet: "Planet") -> None:
    """Cancel every live order the actor holds, releasing money and goods."""
    market = planet.market
    for order_id in list(actor.active_orders):
        market.cancel_order(order_id)


def relocate_actor(actor: "Actor", destination: "Planet", land: "Land") -> None:
    """Attach an actor that has left its origin to ``destination`` with ``land``.

    The land was claimed from ``destination`` at departure, so this attaches
    it rather than drawing a second one. The brain cache is dropped whole:
    its ``yield_modifier`` group is keyed on the actor's land and is never
    otherwise reset, so a stale cache would keep valuing the old planet's
    yields. Brain-internal state is the brain's own to reset, via
    ``ActorBrain.on_relocated``.
    """
    destination.add_actor_with_land(actor, land)
    sim = actor.sim
    sim.actors.append(actor)
    actor.in_transit = False
    actor.brain._cache = None
    actor.last_market_check_turn = sim.current_turn
    actor.brain.on_relocated(actor)
    sim.migration_arrivals += 1
