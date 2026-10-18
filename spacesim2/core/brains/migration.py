"""Migration reasoning: when an actor wants to leave, and for where.

``core/migration.py`` executes a move and knows nothing about why. Every
reason lives here, as plain functions both ``ColonistBrain`` and
``IndustrialistBrain`` call. Neither inherits from anything in this module.

The shape of the decision:

1. Pressure. One number in [0, 1] summarizing how badly the actor's current
   planet is serving it: unmet needs, missing prosperity, and land worse
   than the planet's own average.
2. A staggered check. Scoring 100 planets every turn for every actor is not
   affordable, so each actor scores only on the turns matching its own
   offset, once every ``MIGRATION_CHECK_INTERVAL``.
3. An intent, with hysteresis. Crossing ``ENTER_THRESHOLD`` on a check, and
   passing a propensity-weighted roll, starts an intent to leave. Falling
   below ``EXIT_THRESHOLD`` on a later check ends it. The gap between the
   two stops an actor thrashing on drive noise.
4. A waiting period. The actor holds the intent for ``MIN_INTENT_TURNS``
   before it asks to depart, which is the window in which it liquidates
   what it cannot carry and saves the fare.
5. A request, repeated every turn until a ship carries the actor away. The
   fare offered rises while no ship takes the contract, and two expiries
   in a row end the intent.
"""

import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Mapping, Optional, Protocol, Union

from spacesim2.core.contracts import ContractStatus
from spacesim2.core.drives.prosperity_drive import prosperity_index
from spacesim2.core.migration import (
    NO_MIGRATION,
    MigrationDecision,
    MigrationRequest,
    PlanetStats,
    live_passage_contract,
    passage_fare,
)
from spacesim2.core.navigation import get_navigator
from spacesim2.core.planet_attributes import RESOURCE_ATTRIBUTES

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.navigation import Navigator
    from spacesim2.core.planet import Planet


# Turns between migration checks for one actor. Scoring every planet for
# every actor every turn is the whole cost of this feature, so each actor
# scores on one turn in ten, staggered by a per-actor offset so the
# population's checks spread evenly over the interval instead of landing on
# the same turn.
MIGRATION_CHECK_INTERVAL = 10

# Weight of the prosperity shortfall in migration pressure. Small: an actor
# whose needs are met but who owns no luxuries is mildly dissatisfied, not
# driven out. Only applied when the actor has prosperity drives at all;
# without them there is no shortfall to measure, only a missing signal.
PRESSURE_PROSPERITY_WEIGHT = 0.2

# Weight of the land shortfall. An actor whose own extraction coefficients
# sit well below the planet's mean drew badly, and the draw is fixed for
# life: no amount of trade or skill fixes it, so moving is the only remedy.
# Weighted under the need term because poor land is survivable where unmet
# needs are not.
PRESSURE_LAND_WEIGHT = 0.3

# Pressure at which an actor may form an intent to leave, and the lower
# level at which it abandons one. The band between them is hysteresis: a
# drive debt that oscillates around a single threshold would otherwise start
# and cancel an intent every check.
ENTER_THRESHOLD = 0.35
EXIT_THRESHOLD = 0.2

# Per-check probability of forming an intent, before the actor's own
# propensity and current pressure scale it.
#
# Target: a chronically deprived actor (pressure 0.8) with the median
# propensity (0.6) forms an intent with about 50% probability within 150
# turns. 150 turns is 15 checks. Per check the survival probability must be
# 0.5 ** (1 / 15) = 0.9548, so p_leave = 0.0452. With the scaling factor
# 0.8 * 0.6 = 0.48, BASE_LEAVE_RATE = 0.0452 / 0.48 = 0.094.
BASE_LEAVE_RATE = 0.094

# Range of the fixed per-brain propensity to migrate. Drawn once at brain
# construction, so some actors are rooted and others restless under the same
# pressure. The floor is well above zero so nobody is permanently immobile.
PROPENSITY_MIN = 0.2
PROPENSITY_MAX = 1.0

# Turns an intent is held before the actor asks to depart. The window is
# what the liquidation sweep needs to sell a facility and a stock of goods
# at a price worth taking, and what a poor actor needs to save the fare.
MIN_INTENT_TURNS = 30

# Ceiling on the fare offer, as a multiple of ``passage_fare``, and the
# turns the offer takes to climb there from the estimate. The estimate is
# already the leg's fuel plus half, so the ceiling is fuel times 2.25. No
# ship is obliged
# to fly a passage, so an offer that nobody takes has to rise until one
# does; the ceiling keeps a desperate actor from bidding away the money it
# needs to restart at the far end. Both bounds also apply against the
# actor's own money.
FARE_HEADROOM = 1.5
FARE_ESCALATION_TURNS = 20

# Passage contracts allowed to expire unclaimed before the intent is given
# up. One expiry is a quiet stretch on a route ships do use; two in a row
# says no ship is coming, and holding the intent longer only keeps the actor
# out of the local economy.
MAX_PASSAGE_EXPIRIES = 2

# Destination score weights. Land quality and the residents' wellbeing carry
# the choice; prosperity is a tiebreaker between planets that feed people
# equally well; the fare term is relative to the actor's money, so a poor
# actor weighs a long trip heavily and a rich one barely notices it.
DESTINATION_LAND_WEIGHT = 1.0
DESTINATION_WELLBEING_WEIGHT = 1.0
DESTINATION_PROSPERITY_WEIGHT = 0.2
DESTINATION_FARE_WEIGHT = 0.5

# Least improvement in destination score over the score of staying that
# justifies an intent. Pressure is a push with no pull: in the first hundred
# turns of a run every actor everywhere carries a need debt of 1.0 while the
# economy bootstraps, and a push alone would move most of the population
# between planets that are all equally bad. A move must promise a better
# place, not just an escape from a bad one.
MIN_MIGRATION_GAIN = 0.1

# Softmax temperature for the destination draw. Low, so a clearly better
# planet is usually chosen, but not argmax: every actor scoring the same
# planets with argmax would send the whole wave to one destination, fill its
# land pool, and strand the rest.
DESTINATION_TEMPERATURE = 0.1


class NoDestination:
    """Sentinel: no planet is worth moving to."""

    def __repr__(self) -> str:
        return "NO_DESTINATION"


NO_DESTINATION = NoDestination()

DestinationChoice = Union["Planet", NoDestination]


@dataclass
class MigrationIntent:
    """A brain's standing decision to leave, held across turns.

    ``destination`` is the one Optional in this module. The intent exists
    before a destination is meaningful (``active`` False), and the field is
    read only while ``active`` is True, where it is always a ``Planet``.
    Modelling the inactive state with a second sentinel type would force
    every read site through an isinstance check for a state the state
    machine already rules out.

    ``first_request_turn`` is -1 until the actor first asks for passage;
    from then on it anchors the fare escalation. ``expiries`` counts the
    passage contracts that stood unclaimed to their expiry, and
    ``last_expired_id`` is what stops one expiry being counted on every
    later turn.
    """

    active: bool = False
    since_turn: int = 0
    destination: Optional["Planet"] = None
    first_request_turn: int = -1
    expiries: int = 0
    last_expired_id: str = ""


class MigrationMind(Protocol):
    """What a brain must carry for the functions here to drive it."""

    migration_intent: MigrationIntent
    migration_propensity: float


def draw_propensity() -> float:
    """Fixed per-actor willingness to uproot, drawn at brain construction."""
    return random.uniform(PROPENSITY_MIN, PROPENSITY_MAX)


def is_leaving(brain: MigrationMind) -> bool:
    """Whether the brain is currently planning to leave its planet.

    Brains consult this to stop investing in the planet they are quitting:
    no new tools, no new facilities, and liquidate what they hold.
    """
    return brain.migration_intent.active


def migration_pressure(actor: "Actor") -> float:
    """How badly this planet is serving this actor, in [0, 1].

    Three terms, largest need first:

    * The worst need-drive debt. Debt is accumulated neglect, so it reads
      chronic deprivation rather than a single missed meal, which is exactly
      the signal that should move someone.
    * The prosperity shortfall, at a low weight: wanting better goods is a
      reason to move, but a much weaker one than going hungry. Skipped
      entirely when the actor has no prosperity drives, since then the index
      is absent rather than zero.
    * The land shortfall, the largest gap between the planet's mean
      availability for a resource and this actor's own coefficient. A bad
      draw is permanent, and the planet mean is what a fresh draw elsewhere
      would average, so the gap is what moving could recover.
    """
    planet = actor.planet
    if planet is None:
        return 0.0

    need_debt = max(
        (drive.metrics.debt for drive in actor.drives if drive.WELLBEING),
        default=0.0,
    )

    prosperity_shortfall = 0.0
    if any(not drive.WELLBEING for drive in actor.drives):
        prosperity_shortfall = PRESSURE_PROSPERITY_WEIGHT * (
            1.0 - prosperity_index(actor)
        )

    land_shortfall = 0.0
    for resource in RESOURCE_ATTRIBUTES:
        gap = planet.attributes.get_availability(
            resource
        ) - actor.land.get_availability(resource)
        if gap > land_shortfall:
            land_shortfall = gap
    land_shortfall *= PRESSURE_LAND_WEIGHT

    return min(1.0, max(0.0, need_debt + prosperity_shortfall + land_shortfall))


def choose_destination(
    actor: "Actor",
    stats: Mapping["Planet", PlanetStats],
    navigator: "Navigator",
    fare_weight: float = DESTINATION_FARE_WEIGHT,
) -> DestinationChoice:
    """Pick a planet to move to, or ``NO_DESTINATION`` if none is worth it.

    Candidates are every planet other than the current one with a free land
    to claim; a planet whose pool is full cannot house the actor at all, so
    scoring it would only produce a request core must refuse. A candidate
    must also beat ``staying_score`` by ``MIN_MIGRATION_GAIN``: the same
    scoring applied to the origin, with the actor's own land in place of
    the pool mean, since that is what the actor gives up.

    The score trades expected land quality and the residents' wellbeing off
    against the fare as a share of the actor's money. ``fare_weight`` is
    zero when an existing intent is re-aimed: the actor has already decided
    to pay, and a shrinking balance while it saves must not read as the
    destination getting worse. The draw is a softmax,
    not an argmax: pressure tends to rise on many planets at once, and every
    actor scoring the same best destination would send them all to one place.
    """
    origin = actor.planet
    if origin is None or origin not in stats:
        return NO_DESTINATION

    floor = staying_score(actor, stats[origin]) + MIN_MIGRATION_GAIN
    candidates: List["Planet"] = []
    scores: List[float] = []
    money = max(actor.money, 1)
    for planet, planet_stats in stats.items():
        if planet is origin or planet_stats.free_land_count <= 0:
            continue
        fare = passage_fare(navigator, origin, planet)
        score = _score_planet(
            _expected_land_quality(planet_stats), planet_stats, fare, money, fare_weight
        )
        if score < floor:
            continue
        candidates.append(planet)
        scores.append(score)

    if not candidates:
        return NO_DESTINATION

    # Subtract the max before exponentiating: scores divided by a temperature
    # of 0.1 overflow exp() otherwise, and the shift leaves the distribution
    # unchanged.
    top = max(scores)
    weights = [math.exp((score - top) / DESTINATION_TEMPERATURE) for score in scores]
    return random.choices(candidates, weights=weights, k=1)[0]


def staying_score(actor: "Actor", origin_stats: PlanetStats) -> float:
    """What the actor's current planet scores, on the destination scale.

    The land term is the actor's own draw, averaged over every resource the
    same way ``_expected_land_quality`` averages a pool, so a bad draw shows
    up as a low score to stay and a planet-mean draw elsewhere as a gain.
    """
    own_land = sum(
        actor.land.get_availability(resource) for resource in RESOURCE_ATTRIBUTES
    ) / len(RESOURCE_ATTRIBUTES)
    return _score_planet(own_land, origin_stats, fare=0, money=1, fare_weight=0.0)


def _score_planet(
    land_quality: float,
    planet_stats: PlanetStats,
    fare: int,
    money: int,
    fare_weight: float,
) -> float:
    """The destination score for one planet; see ``choose_destination``."""
    return (
        DESTINATION_LAND_WEIGHT * land_quality
        + DESTINATION_WELLBEING_WEIGHT * (1.0 - planet_stats.median_need_debt)
        + DESTINATION_PROSPERITY_WEIGHT * planet_stats.median_prosperity
        - fare_weight * fare / money
    )


def _expected_land_quality(planet_stats: PlanetStats) -> float:
    """Mean free-land coefficient over the planet's resources, in [0, 1].

    A flat mean over every resource, not only the ones the actor extracts
    today: the actor is choosing a place to live for the rest of the run and
    will follow whatever that planet is good at. Zero when the pool is empty,
    which only reaches here for a planet already filtered out as full.
    """
    values = list(planet_stats.free_land_mean.values())
    if not values:
        return 0.0
    return sum(values) / len(values)


def decide_migration(brain: MigrationMind, actor: "Actor") -> MigrationDecision:
    """The whole migration decision for one actor-turn.

    Cheap on the common path: the state machine only advances on the
    actor's own check turn, and every other turn this either returns
    ``NO_MIGRATION`` or re-states the standing offer, raised a little.
    """
    if actor.planet is None:
        return NO_MIGRATION

    sim = actor.sim
    turn = sim.current_turn
    if turn % MIGRATION_CHECK_INTERVAL == _check_offset(actor):
        _advance_intent(brain, actor)

    intent = brain.migration_intent
    if not intent.active or intent.destination is None:
        return NO_MIGRATION
    if turn - intent.since_turn < MIN_INTENT_TURNS:
        return NO_MIGRATION
    if _count_expiry(brain, actor):
        return NO_MIGRATION

    fare = passage_fare(get_navigator(sim), actor.planet, intent.destination)
    # Money the actor's own open contract is holding is money it can offer:
    # cancelling to re-price hands the reserve straight back.
    live = live_passage_contract(actor)
    budget = actor.money + (live.total_payment if live is not None else 0)
    if budget < fare:
        # Not yet affordable. The intent stands and the actor keeps saving:
        # its liquidation sweep is still running, so the money is coming.
        return NO_MIGRATION

    if intent.first_request_turn < 0:
        intent.first_request_turn = turn
    return MigrationRequest(
        intent.destination, _fare_offer(fare, turn - intent.first_request_turn, budget)
    )


def _fare_offer(fare: int, turns_offering: int, budget: int) -> int:
    """The fare to offer now: the estimate, raised toward its ceiling.

    Linear from ``passage_fare`` at the first request to
    ``FARE_HEADROOM`` times it after ``FARE_ESCALATION_TURNS``. The rise is
    the only price discovery a passage gets; a flat offer on a route no
    ship wants would simply expire, over and over.
    """
    climb = min(1.0, max(0.0, turns_offering / FARE_ESCALATION_TURNS))
    offer = fare * (1.0 + (FARE_HEADROOM - 1.0) * climb)
    return min(budget, int(round(offer)))


def _count_expiry(brain: MigrationMind, actor: "Actor") -> bool:
    """Note a passage that expired unclaimed. True if the intent was dropped.

    The actor reposts after the first expiry, at the offer the escalation
    has reached by then. After ``MAX_PASSAGE_EXPIRIES`` it stays put.
    """
    contract = actor.passage_contract
    if contract is None or contract.status is not ContractStatus.EXPIRED:
        return False
    intent = brain.migration_intent
    if contract.contract_id == intent.last_expired_id:
        return intent.expiries >= MAX_PASSAGE_EXPIRIES

    intent.last_expired_id = contract.contract_id
    intent.expiries += 1
    if intent.expiries >= MAX_PASSAGE_EXPIRIES:
        clear_intent(brain)
        return True
    return False


def _check_offset(actor: "Actor") -> int:
    """The turn within each interval on which this actor scores.

    Derived from the name so it is fixed for the actor's life and spread
    evenly over the population.
    """
    return hash(actor.name) % MIGRATION_CHECK_INTERVAL


def _advance_intent(brain: MigrationMind, actor: "Actor") -> None:
    """Run one migration check: form, re-aim, or abandon the intent."""
    intent = brain.migration_intent
    pressure = migration_pressure(actor)

    if intent.active:
        if pressure < EXIT_THRESHOLD:
            intent.active = False
            intent.destination = None
            return
        # Re-aim. Land pools fill and planets recover while the actor waits
        # out MIN_INTENT_TURNS, so the destination picked at entry may no
        # longer accept it or no longer be worth the move. When nowhere
        # clears the gain floor the pull is gone, and the intent goes with
        # it: the push alone was never enough to form one.
        choice = choose_destination(
            actor, actor.sim.planet_stats, get_navigator(actor.sim), fare_weight=0.0
        )
        if isinstance(choice, NoDestination):
            intent.active = False
            intent.destination = None
        else:
            intent.destination = choice
        return

    if pressure < ENTER_THRESHOLD:
        return
    if random.random() >= BASE_LEAVE_RATE * brain.migration_propensity * pressure:
        return
    choice = choose_destination(actor, actor.sim.planet_stats, get_navigator(actor.sim))
    if isinstance(choice, NoDestination):
        return
    intent.active = True
    intent.since_turn = actor.sim.current_turn
    intent.destination = choice
    # A fresh start: the escalation clock has not begun, and an expiry from
    # an intent the actor gave up long ago must not count against this one.
    intent.first_request_turn = -1
    intent.expiries = 0
    intent.last_expired_id = (
        actor.passage_contract.contract_id if actor.passage_contract else ""
    )


def clear_intent(brain: MigrationMind) -> None:
    """Drop any intent to leave. Called from each brain's relocation hook."""
    brain.migration_intent = MigrationIntent()
