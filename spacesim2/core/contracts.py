"""Contracts: paid transport jobs a ship carries for someone else.

One data type, ``Contract``, covers every job a ship can be paid to fly:
a migrant's passage, an abstract government freight lot, and later real
goods. Core owns what a contract *does*; brains own whether to post one
and whether to accept one, the same split as migration and market orders.
Nothing in this module decides anything.

Lifecycle:

===============================  =====================================
Call                             Effect
===============================  =====================================
``board.post(contract)``         reserve the poster's money, OPEN
``board.accept(contract, ship)`` ACCEPTED, counts against the hold
``load_contract(ship, c)``       payload boards, advance paid, LOADED
``deliver_contract(ship, c)``    payload delivered, on_delivery paid
``board.release(contract)``      an ACCEPTED contract back to OPEN
``board.cancel(contract)``       an OPEN contract off the board
``board.expire(turn)``           OPEN contracts past expiry, EXPIRED
``strand_contract(ship, c, p)``  payload unloaded short of destination
===============================  =====================================

Money. An actor poster reserves ``advance + on_delivery`` at posting, the
way a bid reserves money, and gets it back on cancel, expiry, or the
refund half of a stranding. The government reserves nothing; its payouts
are created, the same way the government wage is. Every payment goes to
the carrier in full.

A contract lives on its origin planet's board from posting until it is
delivered, cancelled, or expired, so core functions reach the board
through ``contract.origin.contracts``.
"""

import enum
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Dict, List, Optional, Union

if TYPE_CHECKING:
    from spacesim2.core.actor import Actor
    from spacesim2.core.commodity import CommodityDefinition
    from spacesim2.core.land import Land
    from spacesim2.core.planet import Planet
    from spacesim2.core.ship import Ship


# Cargo units one migrant occupies on a ship. At this size a passenger is
# a rider on a trade the ship was making anyway rather than a whole trip,
# and no passenger fits beside a government lot of 85 to 100 units, so the
# two kinds of contract never share a hold.
MIGRANT_CARGO_UNITS = 20

# Docked turns a ship may sit somewhere that is not a loaded contract's
# destination before core strands the payload there. The ship is supposed
# to be pinned to the destination, so reaching this means the departure
# gate keeps refusing and the payload is better off unloaded than riding
# along indefinitely.
CONTRACT_STRAND_PATIENCE = 10


class ContractStatus(enum.Enum):
    """Where a contract is in its lifecycle."""

    OPEN = "open"
    ACCEPTED = "accepted"
    LOADED = "loaded"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SuppliedBy(enum.Enum):
    """Who provides the goods of a ``CargoPayload``."""

    POSTER = "poster"
    CARRIER = "carrier"


@dataclass(frozen=True)
class PassengerPayload:
    """One regular actor riding to the destination.

    ``land`` is the destination land the passenger will settle on. It is
    None until the payload loads, which is where the land is claimed: an
    open contract must hold nothing at the destination, or a queue of
    unaccepted contracts could exhaust a land pool nobody is travelling
    to. This is one of the few places an Optional carries real meaning,
    and the two states are distinguished by the contract's status rather
    than by guessing.

    Payloads are frozen, so claiming or releasing a land produces a new
    payload that is assigned to the contract; see ``load_contract``.
    """

    actor: "Actor"
    land: "Land | None" = None

    @property
    def hold_units(self) -> int:
        """Hold space the passenger occupies."""
        return MIGRANT_CARGO_UNITS


@dataclass(frozen=True)
class ConsignmentPayload:
    """An abstract lot that exists only inside the contract.

    Government freight. It never touches the goods economy: nothing is
    bought at the origin and nothing is sold at the destination, so the
    only thing that moves is the hold space and the payment.
    """

    units: int

    @property
    def hold_units(self) -> int:
        """Hold space the lot occupies."""
        return self.units


@dataclass(frozen=True)
class CargoPayload:
    """Real goods moved under contract.

    ``POSTER`` is freight: the poster hands the goods over at load.
    ``CARRIER`` is procurement: the ship sources them and is paid on
    delivery. Defined so the type does not have to change when a brain
    wants one; no brain posts one yet, and loading or delivering one
    raises.
    """

    commodity: "CommodityDefinition"
    quantity: int
    supplied_by: SuppliedBy

    @property
    def hold_units(self) -> int:
        """Hold space the goods occupy."""
        return self.quantity


Payload = Union[PassengerPayload, ConsignmentPayload, CargoPayload]


class Government:
    """Sentinel poster for jobs the government pays for.

    It holds no money: its payouts are created when they are made, the
    way the government wage is, so it reserves nothing at posting.
    """

    def __repr__(self) -> str:
        return "GOVERNMENT"


GOVERNMENT = Government()

Poster = Union["Actor", Government]


@dataclass
class Contract:
    """A paid job to carry ``payload`` from ``origin`` to ``destination``.

    Not frozen: ``status``, ``carrier``, and the payload's claimed land
    change as the job runs.
    """

    poster: Poster
    origin: "Planet"
    destination: "Planet"
    payload: Payload
    advance: int
    on_delivery: int
    posted_turn: int
    expires_turn: int
    contract_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: ContractStatus = ContractStatus.OPEN
    carrier: Optional["Ship"] = None

    @property
    def total_payment(self) -> int:
        """Everything the carrier is paid over the whole job."""
        return self.advance + self.on_delivery


def _reserve(poster: Poster, amount: int) -> None:
    """Hold ``amount`` of an actor poster's money against a contract.

    Mirrors what a market bid does: the money leaves ``money`` at once so
    nothing else can spend it, and sits in ``reserved_money`` until it is
    paid out or released. The government reserves nothing.
    """
    if isinstance(poster, Government):
        return
    poster.money -= amount
    poster.reserved_money += amount


def _release(poster: Poster, amount: int) -> None:
    """Return reserved money to an actor poster."""
    if isinstance(poster, Government):
        return
    poster.reserved_money -= amount
    poster.money += amount


def _pay(poster: Poster, amount: int, ship: "Ship") -> None:
    """Pay ``amount`` out of the reserve to ``ship``.

    Government money is created here rather than drawn from a reserve.
    """
    if amount <= 0:
        return
    if isinstance(poster, Government):
        ship.simulation.government_payouts += amount
    else:
        poster.reserved_money -= amount
    ship.money += amount


class ContractBoard:
    """The open contracts posted at one planet.

    One board per planet, next to its market. Ships read every board, as
    they already read every market, so a ship two lanes away can plan a
    trip around a contract.
    """

    def __init__(self, planet: "Planet") -> None:
        self.planet = planet
        self.contracts: Dict[str, Contract] = {}

    def post(self, contract: Contract) -> None:
        """Reserve the poster's money and put the contract up as OPEN."""
        if contract.origin is not self.planet:
            raise ValueError(
                f"{contract.contract_id} originates at {contract.origin.name}, "
                f"not {self.planet.name}"
            )
        if contract.contract_id in self.contracts:
            raise ValueError(f"{contract.contract_id} is already posted")
        _reserve(contract.poster, contract.total_payment)
        contract.status = ContractStatus.OPEN
        self.contracts[contract.contract_id] = contract

    def cancel(self, contract: Contract) -> bool:
        """Withdraw an OPEN contract and release its reserve.

        Returns False for a contract a carrier has already taken on;
        pulling those out from under a ship is what ``release`` is for.
        """
        if contract.status is not ContractStatus.OPEN:
            return False
        _release(contract.poster, contract.total_payment)
        contract.status = ContractStatus.CANCELLED
        self.contracts.pop(contract.contract_id, None)
        return True

    def accept(self, contract: Contract, ship: "Ship") -> bool:
        """Assign an OPEN contract to ``ship``.

        The hold units count against the ship's free hold from here, so a
        ship cannot accept more than it can carry and then buy cargo on
        top. No money moves until the payload loads.
        """
        if contract.status is not ContractStatus.OPEN:
            return False
        contract.status = ContractStatus.ACCEPTED
        contract.carrier = ship
        ship.contracts.append(contract)
        return True

    def release(self, contract: Contract) -> bool:
        """Put an ACCEPTED contract back on the board and off the ship.

        Only before the payload loads. Once loaded the ship is committed:
        the way out then is delivery or ``strand_contract``.
        """
        if contract.status is not ContractStatus.ACCEPTED:
            return False
        carrier = contract.carrier
        if carrier is not None and contract in carrier.contracts:
            carrier.contracts.remove(contract)
        contract.carrier = None
        contract.status = ContractStatus.OPEN
        return True

    def open_contracts(self) -> List[Contract]:
        """Every contract here that no carrier has taken on."""
        return [
            contract
            for contract in self.contracts.values()
            if contract.status is ContractStatus.OPEN
        ]

    def open_to(self, destination: "Planet") -> List[Contract]:
        """Open contracts bound for ``destination``."""
        return [
            contract
            for contract in self.open_contracts()
            if contract.destination is destination
        ]

    def expire(self, current_turn: int) -> List[Contract]:
        """Expire every OPEN contract past its expiry and refund its reserve.

        Accepted and loaded contracts are left alone: a carrier is
        working on them, and the expiry is a limit on how long an offer
        stands, not on how long a flight may take.
        """
        expired: List[Contract] = []
        for contract in list(self.contracts.values()):
            if contract.status is not ContractStatus.OPEN:
                continue
            if contract.expires_turn > current_turn:
                continue
            _release(contract.poster, contract.total_payment)
            contract.status = ContractStatus.EXPIRED
            del self.contracts[contract.contract_id]
            expired.append(contract)
        return expired


def _board_of(contract: Contract) -> ContractBoard:
    """The board a contract was posted to."""
    return contract.origin.contracts


def _finish(contract: Contract, status: ContractStatus) -> None:
    """Take a finished contract off its board and off its carrier."""
    contract.status = status
    carrier = contract.carrier
    if carrier is not None and contract in carrier.contracts:
        carrier.contracts.remove(contract)
    _board_of(contract).contracts.pop(contract.contract_id, None)


def load_contract(ship: "Ship", contract: "Contract") -> bool:
    """Board the payload and pay the advance. Returns whether it loaded.

    Called from :meth:`Ship.start_journey` once the departure is certain,
    so a load is never undone by a journey that does not happen. A
    passenger load can still fail, when the destination has no free land
    left; the contract then goes back on the board as OPEN for another
    ship or another turn.
    """
    if contract.status is not ContractStatus.ACCEPTED:
        return False
    payload = contract.payload

    if isinstance(payload, CargoPayload):
        raise NotImplementedError(
            "CargoPayload is defined but not carried yet; no brain posts one"
        )

    if isinstance(payload, PassengerPayload):
        from spacesim2.core.migration import record_passenger_departure

        if not _board_passenger(contract, payload):
            return False
        # Boarding is the moment a migration is certain, so it is what the
        # migration counters count.
        record_passenger_departure(payload.actor, contract, contract.advance)

    _pay(contract.poster, contract.advance, ship)
    contract.status = ContractStatus.LOADED
    return True


def _board_passenger(contract: Contract, payload: PassengerPayload) -> bool:
    """Take a passenger off its origin planet, with a land claimed ahead.

    The land is claimed here rather than at posting so an open contract
    holds nothing at the destination. An empty pool is a real outcome,
    not an error: the contract goes back to OPEN and nothing has moved.

    The passenger travels with nothing. A brain that wants value at the
    far end liquidates before it asks to move, which is also why the
    origin's resting orders go first: nothing it left in the book can
    fill once it has gone.
    """
    from spacesim2.core.migration import _cancel_all_orders

    destination = contract.destination
    if not destination.free_lands:
        _board_of(contract).release(contract)
        return False

    actor = payload.actor
    origin = contract.origin
    _cancel_all_orders(actor, origin)
    contract.payload = replace(payload, land=destination.claim_land())

    origin.remove_actor(actor)
    actor.sim.actors.remove(actor)
    actor.in_transit = True
    actor.inventory.clear()
    return True


def deliver_contract(ship: "Ship", contract: "Contract") -> bool:
    """Put the payload down at the destination and pay ``on_delivery``.

    Called from :meth:`Ship.update_journey` on arrival. Returns whether
    anything was delivered.
    """
    if contract.status is not ContractStatus.LOADED:
        return False
    payload = contract.payload

    if isinstance(payload, CargoPayload):
        raise NotImplementedError(
            "CargoPayload is defined but not carried yet; no brain posts one"
        )

    if isinstance(payload, PassengerPayload):
        from spacesim2.core.migration import relocate_actor

        if payload.land is None:
            raise ValueError(
                f"{contract.contract_id} is loaded but holds no destination land"
            )
        relocate_actor(payload.actor, contract.destination, payload.land)

    _pay(contract.poster, contract.on_delivery, ship)
    _finish(contract, ContractStatus.DELIVERED)
    ship.simulation.contracts_delivered += 1
    return True


def strand_contract(ship: "Ship", contract: "Contract", planet: "Planet") -> bool:
    """Unload a payload short of its destination, at ``planet``.

    The advance is not clawed back: the carrier flew the leg it was paid
    for. ``on_delivery`` is refunded, since the delivery did not happen.

    A passenger needs somewhere to live, so a stranding only happens
    where the local pool has a free land; otherwise the passenger stays
    aboard until the ship next docks somewhere that does, and this
    returns False. The land reserved at the destination goes back to the
    destination's pool either way it is used.
    """
    if contract.status is not ContractStatus.LOADED:
        return False
    payload = contract.payload

    if isinstance(payload, CargoPayload):
        raise NotImplementedError(
            "CargoPayload is defined but not carried yet; no brain posts one"
        )

    if isinstance(payload, PassengerPayload):
        from spacesim2.core.migration import relocate_actor

        if not planet.free_lands:
            return False
        if payload.land is None:
            raise ValueError(
                f"{contract.contract_id} is loaded but holds no destination land"
            )
        local_land = planet.claim_land()
        contract.destination.release_land(payload.land)
        contract.payload = replace(payload, land=None)
        relocate_actor(payload.actor, planet, local_land)

    _release(contract.poster, contract.on_delivery)
    _finish(contract, ContractStatus.CANCELLED)
    ship.simulation.contracts_stranded += 1
    return True
