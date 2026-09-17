import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from spacesim2.core.actor import Actor, ActorType
from spacesim2.core.brains import (
    ColonistBrain,
    IndustrialistBrain,
    MarketMakerBrain,
    SpaceportOperatorBrain,
    TradingCompanyBrain,
)
from spacesim2.core.commodity import CommodityRegistry
from spacesim2.core.contracts import ContractStatus, PassengerPayload
from spacesim2.core.data_logger import DataLogger
from spacesim2.core.drives import (
    ActorDrive,
    ClothingDrive,
    FacilityUpkeepDrive,
    FoodDrive,
    HealthDrive,
    ShelterDrive,
    prosperity_drives,
    random_tastes,
)
from spacesim2.core.facility import FacilityRegistry
from spacesim2.core.galaxy import (
    DEFAULT_ARMS,
    DEFAULT_LANE_DENSITY,
    GalaxyLayout,
    StarLaneNetwork,
    generate_spiral_layout,
)
from spacesim2.core.government import refresh_government_jobs
from spacesim2.core.market import Market
from spacesim2.core.migration import (
    MigrationEvent,
    PlanetStats,
    refresh_planet_stats,
    run_migration_phase,
)
from spacesim2.core.navigation import get_navigator
from spacesim2.core.planet import Planet
from spacesim2.core.planet_attributes import PlanetAttributes
from spacesim2.core.process import ProcessRegistry
from spacesim2.core.ship import (
    BASE_FUEL_CAPACITY,
    SHIP_CAPITAL_FLOOR,
    Ship,
    fuel_capacity_for,
    initial_fuel_for,
    retained_capital_for,
    starting_capital,
)
from spacesim2.core.ship_spawn import ShipSpawnQueue
from spacesim2.core.skill import SkillsRegistry

if TYPE_CHECKING:
    from spacesim2.analysis.export.exporter import SimulationExporter

# Syllable pools for procedural planet names, used once the curated list is
# exhausted.
_NAME_ONSETS = [
    "Kar",
    "Vel",
    "Thal",
    "Zor",
    "Mira",
    "Ques",
    "Dra",
    "Sol",
    "Nem",
    "Ory",
    "Bel",
    "Xan",
    "Tyr",
    "Hal",
    "Jun",
    "Kel",
    "Vor",
    "Ash",
    "Ryn",
    "Ost",
]
_NAME_MIDDLES = [
    "a",
    "e",
    "i",
    "o",
    "u",
    "ar",
    "en",
    "ir",
    "or",
    "un",
    "al",
    "eth",
]
_NAME_CODAS = [
    "dor",
    "vex",
    "mir",
    "thos",
    "rin",
    "nax",
    "lis",
    "gar",
    "von",
    "tis",
    "kar",
    "dune",
    "prime",
    "moor",
    "reth",
    "sil",
    "quor",
    "bane",
    "loth",
    "wyn",
]

# Market makers quote both sides of every transportable commodity, including
# upper-tier goods with no organic supply yet, so their starting capital scales
# with the number of markets served and per-market depth holds as the
# commodity tree grows. MarketMakerBrain allocates the pool.
MARKET_MAKER_CAPITAL_PER_MARKET = 100

# Land pool per planet for a set-up galaxy. Larger than the 100-actor default
# so migration has somewhere to land; ``LANDS_PER_PLANET`` stays the bare
# ``Planet`` default. See core/land.py.
DEFAULT_LANDS_PER_PLANET = 200

# Cargo hold of a ship a trading company builds, matching the ``Ship`` default
# every ship in a set-up galaxy carries.
SPAWNED_SHIP_CARGO_CAPACITY = 100

# Fuel efficiency of a ship a trading company builds, drawn per ship over the
# same range setup uses.
SPAWNED_SHIP_EFFICIENCY_RANGE = (0.8, 1.2)


class Simulation:
    """Main simulation controller."""

    def __init__(self) -> None:
        self.planets: List[Planet] = []
        # Star lanes joining the planets; ships travel only along lanes. See
        # core/galaxy.py. Empty until setup_simple builds the galaxy; a
        # hand-built world must populate it. StarLaneNetwork.complete gives
        # the any-to-any model.
        self.star_lanes = StarLaneNetwork()
        # Bounding box of the planet layout (map units), for renderers.
        self.galaxy_size: Tuple[float, float] = (100.0, 100.0)
        self.actors: List[Actor] = []
        self.ships: List[Ship] = []
        # Ships trading companies bought this turn, built serially between the
        # actor and ship phases. See core/ship_spawn.py.
        self.pending_ship_spawns = ShipSpawnQueue()
        # Galaxy-derived ship sizing, fixed once the layout exists: what a new
        # ship launches with and how big its tank is. _setup_ships records
        # them at mean efficiency so the spawn path need not recompute the
        # galaxy geometry. The defaults are the small-galaxy baselines, for a
        # hand-built world that never calls _setup_ships.
        self.ship_starting_capital: int = SHIP_CAPITAL_FLOOR
        self.ship_fuel_capacity: int = BASE_FUEL_CAPACITY
        self.current_turn = 0
        # Conservation check: no actor is ever created or destroyed after
        # setup, only moved between ``self.actors`` and a ship's hold (see
        # ``population()``). Recorded lazily on the first ``run_turn`` call
        # rather than at the end of setup, since some tests build a
        # ``Simulation`` by hand and append actors after construction but
        # before the first turn; ``None`` means "not yet recorded".
        self.expected_population: Optional[int] = None
        # Migration state; see core/migration.py. planet_stats is rebuilt at
        # the top of every turn. A departure is a passenger boarding a ship
        # and an arrival is one being put down, so the gap between the two
        # counts is the passengers in flight.
        self.planet_stats: Dict[Planet, PlanetStats] = {}
        self.migration_log: List[MigrationEvent] = []
        self.migration_departures: int = 0
        self.migration_arrivals: int = 0
        # Turns from posting a passage contract to boarding, one per
        # departure, and passage contracts nobody took before expiry.
        self.passage_wait_turns: List[int] = []
        self.passage_expired: int = 0
        # Transport contracts; see core/contracts.py. The three endings are
        # counted by core as they happen; ``contracts_posted`` is counted by
        # whoever posts, since a board has no simulation to reach.
        self.contracts_posted: int = 0
        self.contracts_delivered: int = 0
        self.contracts_stranded: int = 0
        self.contracts_expired: int = 0
        # Money the government has created paying carriers for freight; see
        # core/government.py.
        self.government_payouts: int = 0
        # Threaded actor phase (core/parallel.py). Above 1, planets are
        # sharded across a thread pool; a real speedup needs a free-threaded
        # interpreter. See docs/performance.md.
        self.parallel_workers: int = 1
        self._actor_phase_pool: Optional[ThreadPoolExecutor] = None
        self._actor_phase_pool_size: int = 0

        data_dir = Path("data")
        commodities_path = data_dir / "commodities.yaml"
        processes_path = data_dir / "processes.yaml"
        skills_path = data_dir / "skills.yaml"
        facilities_path = data_dir / "facilities.yaml"

        self.commodity_registry = CommodityRegistry()
        self.commodity_registry.load_from_file(commodities_path)

        self.process_registry = ProcessRegistry(self.commodity_registry)
        self.process_registry.load_from_file(processes_path)

        self.facility_registry = FacilityRegistry(self.commodity_registry)
        self.facility_registry.load_from_file(facilities_path)

        self.skills_registry = SkillsRegistry()
        self.skills_registry.load_from_file(skills_path)

        self.data_logger = DataLogger()
        self.exporter: Optional["SimulationExporter"] = None

    def _generate_planet_names(self, num_planets: int) -> List[str]:
        """Pick ``num_planets`` unique planet names.

        Curated names come first, then procedural ones, so any galaxy size
        gets unique, readable names.
        """
        fictional_names = [
            "Drakoon",
            "Veyrahn",
            "Korrith",
            "Jandrak",
            "Thazuun",
            "Marrakos",
            "Zethar",
            "Xoroq",
            "Bal’drahn",
            "Quorrin",
            "Endahlis",
            "Veyndor",
            "Liranna",
            "Koshava",
            "Bren’drel",
            "Orivaan",
            "Sylith",
            "Quenara",
            "Thaelwyn",
            "Yorrak",
            "Thalassa",
            "Myrren",
            "Korvannis",
            "Aelithar",
            "Voruna",
            "Kalyss",
            "Drennok",
            "Oquara",
            "Zorathis",
            "Fen’lor",
            "Vrynnos",
            "Shalvek",
            "Droskaar",
            "Arveth",
            "Krylos",
            "Vintarra",
            "Malqor",
            "Zeyrik",
            "Korenth",
            "Thaazek",
            "Dravik",
            "Corvenn",
            "Helkross",
            "Tyrranos",
            "Velcorra",
            "Oxenn Prime",
            "Grenthis",
            "Calbrak",
            "Drosmir",
            "Xyphara",
            "Zalith",
            "Onakorr",
            "Yrivaan",
            "Votharis",
            "Kaelthuun",
            "Ombera",
            "Kythorr",
            "Ruunas",
            "Elvorra",
            "Shyrrak",
            "Branoth",
            "Jekarra",
            "Zyndralis",
            "Vornakk",
            "Pellosh",
            "Quorrahn",
            "Tavrelis",
            "Korrithos",
            "Drethuun",
            "Xeylon",
            "Alvara",
            "Serenith",
            "Valdorei",
            "Olyssian",
            "Trinorra",
            "Eryllan",
            "Feyriss",
            "Solvane",
            "Quenalis",
            "Marrosyn",
            "Krassith",
            "Vondrak",
            "Barrekos",
            "Dravoss",
            "Ulthar",
            "Zorran Prime",
            "Makreth",
            "Syrrik",
            "Kolthuun",
            "Vashrik",
            "Iyora",
            "Qethar",
            "Zhaluun",
            "Drevari",
            "Othwyn",
            "Pyrassos",
            "Venorra",
            "Kyrathis",
            "Jorvess",
            "Thaelkor",
        ]

        if num_planets < 1:
            raise ValueError(f"num_planets must be >= 1, got {num_planets}")

        if num_planets <= len(fictional_names):
            return random.sample(fictional_names, num_planets)
        selected_names = random.sample(fictional_names, len(fictional_names))
        used = set(selected_names)
        while len(selected_names) < num_planets:
            name = self._generate_procedural_name(used)
            used.add(name)
            selected_names.append(name)
        return selected_names

    @staticmethod
    def _generate_procedural_name(used: set[str]) -> str:
        """Generate a readable planet name not in ``used``.

        Joins onset, optional middle, and coda syllables, such as "Karendor"
        or "Velvex". Collisions retry; after 100 attempts a numeric suffix
        guarantees uniqueness.
        """
        for _ in range(100):
            name = random.choice(_NAME_ONSETS)
            if random.random() < 0.5:
                name += random.choice(_NAME_MIDDLES)
            name += random.choice(_NAME_CODAS)
            name = name.capitalize()
            if name not in used:
                return name
        # Fallback: a numeric suffix guarantees uniqueness.
        base = name
        suffix = 2
        while f"{base}-{suffix}" in used:
            suffix += 1
        return f"{base}-{suffix}"

    def setup_simple(
        self,
        num_planets: int = 2,
        num_regular_actors: int = 4,
        num_market_makers: int = 1,
        num_spaceport_operators: int = 2,
        num_ships: int = 2,
        arms: int = DEFAULT_ARMS,
        lane_density: float = DEFAULT_LANE_DENSITY,
        lands_per_planet: int = DEFAULT_LANDS_PER_PLANET,
    ) -> None:
        """Build a galaxy of planets, actors, and ships.

        Planets lie on a spiral and are joined by star lanes; ships travel
        only along lanes. See ``core/galaxy.py``.

        Args:
            num_regular_actors: Regular actors per planet.
            num_market_makers: Market makers per planet.
            num_spaceport_operators: Spaceport operators per planet.
            num_ships: Ships per planet.
            arms: Spiral arm count.
            lane_density: Fraction of optional local lanes kept beyond the
                spanning tree; 0 is tree only, 1 is every local lane.
            lands_per_planet: Size of each planet's land pool. It must
                exceed ``num_regular_actors`` or nobody can ever move in.
        """
        names = self._generate_planet_names(num_planets)
        layout = generate_spiral_layout(
            num_planets, arms=arms, lane_density=lane_density
        )
        planet_data = [(name, x, y) for name, (x, y) in zip(names, layout.positions)]

        # Guarantee at least one abundant fuel source. nova_fuel_ore rolls
        # are bimodal, so a galaxy can come up all-poor, with no viable fuel
        # production anywhere and every ship eventually stranded.
        attribute_rolls = [PlanetAttributes.generate_random() for _ in planet_data]
        if attribute_rolls and all(a.nova_fuel_ore < 0.7 for a in attribute_rolls):
            random.choice(attribute_rolls).nova_fuel_ore = random.uniform(0.7, 1.0)

        for (name, x, y), attributes in zip(planet_data, attribute_rolls):
            planet_market = Market()
            planet_market.commodity_registry = (
                self.commodity_registry
            )  # Give market access to commodity registry
            # Record order events only for logged actors. This is a live
            # view, so a --log-actors selection made after setup is honored.
            # See Market.order_event_filter.
            planet_market.order_event_filter = self.data_logger.logged_actor_names()

            planet = Planet(
                name,
                planet_market,
                x=x,
                y=y,
                attributes=attributes,
                num_lands=lands_per_planet,
            )
            self.planets.append(planet)

            self._setup_planet_actors(
                planet=planet,
                num_regular_actors=num_regular_actors,
                num_market_makers=num_market_makers,
                num_spaceport_operators=num_spaceport_operators,
                actor_name_prefix=name,
            )

        self._apply_layout_lanes(layout)

        self._setup_ships(num_ships)

    def _apply_layout_lanes(self, layout: GalaxyLayout) -> None:
        """Build ``star_lanes`` from a layout whose indices match ``planets``."""
        if len(layout.positions) != len(self.planets):
            raise ValueError(
                f"layout has {len(layout.positions)} positions for "
                f"{len(self.planets)} planets"
            )
        self.star_lanes = StarLaneNetwork()
        for i, j in layout.lanes:
            self.star_lanes.add_lane(self.planets[i], self.planets[j])
        self.galaxy_size = (layout.width, layout.height)

    def _setup_planet_actors(
        self,
        planet: Planet,
        num_regular_actors: int,
        num_market_makers: int,
        num_spaceport_operators: int,
        actor_name_prefix: str,
    ) -> None:
        """Create a planet's regular actors and its service actors."""
        # Regular actors split evenly between colonists and industrialists.
        num_colonists = num_regular_actors // 2
        num_industrialists = num_regular_actors - num_colonists

        # Same for every actor on the planet, so computed once.
        all_skills = list(self.skills_registry._skills.keys())

        regular_actor_specs = [
            ("Colonist", ColonistBrain, num_colonists),
            ("Industrialist", IndustrialistBrain, num_industrialists),
        ]
        for role_name, brain_class, count in regular_actor_specs:
            for i in range(1, count + 1):
                initial_skills = self._random_initial_skills(all_skills)

                tastes = random_tastes()
                # Needs first: keep levels and budget priority both take the
                # first drive that lists a material.
                drives: list[ActorDrive] = [
                    Drive(commodity_registry=self.commodity_registry)
                    for Drive in (FoodDrive, ClothingDrive, ShelterDrive, HealthDrive)
                ]
                drives.extend(prosperity_drives(self.commodity_registry, tastes))

                actor = Actor(
                    name=f"{actor_name_prefix}{role_name}-{i}",
                    sim=self,
                    planet=planet,
                    actor_type=ActorType.REGULAR,
                    brain=brain_class(),
                    drives=drives,
                    initial_money=50,
                    initial_skills=initial_skills,
                    tastes=tastes,
                )
                self.actors.append(actor)
                planet.add_actor(actor)

        num_markets = sum(
            1 for c in self.commodity_registry.all_commodities() if c.transportable
        )
        market_maker_capital = MARKET_MAKER_CAPITAL_PER_MARKET * max(1, num_markets)

        for i in range(num_market_makers):
            initial_skills = {skill_id: 1.0 for skill_id in all_skills}

            actor = Actor(
                name=f"{actor_name_prefix}MarketMaker-{i + 1}",
                sim=self,
                planet=planet,
                drives=[],
                actor_type=ActorType.SERVICE,
                brain=MarketMakerBrain(),
                initial_money=market_maker_capital,
                initial_skills=initial_skills,
            )
            self.actors.append(actor)
            planet.add_actor(actor)

        self._setup_spaceport_operators(
            planet=planet,
            count=num_spaceport_operators,
            all_skills=all_skills,
            actor_name_prefix=actor_name_prefix,
        )

        self._setup_trading_company(planet)

    def _setup_trading_company(self, planet: Planet) -> Actor:
        """Create the planet's one trading company, owner of its ships.

        It starts with no money and no goods: its whole income is what its
        ships remit. See core/brains/trading_company.py.
        """
        company = Actor(
            name=f"{planet.name} Trading Co",
            sim=self,
            planet=planet,
            drives=[],
            actor_type=ActorType.SERVICE,
            brain=TradingCompanyBrain(),
            initial_money=0,
        )
        self.actors.append(company)
        planet.add_actor(company)
        return company

    @staticmethod
    def _trading_company_on(planet: Planet) -> Tuple[Actor, TradingCompanyBrain]:
        """The planet's trading company and its brain."""
        for actor in planet.actors:
            brain = actor.brain
            if isinstance(brain, TradingCompanyBrain):
                return actor, brain
        raise ValueError(f"{planet.name} has no trading company")

    def _setup_spaceport_operators(
        self,
        planet: Planet,
        count: int,
        all_skills: List[str],
        actor_name_prefix: str,
    ) -> None:
        """Create a planet's spaceport operators, each with a pre-built port.

        Level 1 spaceports are pre-built rather than constructed: ships need
        fuel from turn 1 and building materials take roughly fifty turns to
        exist anywhere. Operators get no capital injection; they bootstrap on
        the government wage, so their stock builds slowly and compounds from
        ship sales. See docs/spaceport-design.md.
        """
        spaceport = self.commodity_registry.get_commodity("spaceport")
        facility = self.facility_registry.get_facility("spaceport")
        if spaceport is None or facility is None:
            return

        for i in range(1, count + 1):
            actor = Actor(
                name=f"{actor_name_prefix}SpaceportOperator-{i}",
                sim=self,
                planet=planet,
                drives=[FacilityUpkeepDrive(self.commodity_registry, facility)],
                actor_type=ActorType.SERVICE,
                brain=SpaceportOperatorBrain(),
                initial_money=50,
                initial_skills={skill_id: 1.0 for skill_id in all_skills},
            )
            actor.inventory.add_commodity(spaceport, 1)
            self.actors.append(actor)
            planet.add_actor(actor)

    @staticmethod
    def _random_initial_skills(all_skills: List[str]) -> Dict[str, float]:
        """Roll initial skill levels with 1-3 specialties.

        Specialties rate 1.0-2.0; the rest 0.5-1.0.
        """
        num_specialties = random.randint(1, min(3, len(all_skills)))
        specialty_skills = set(random.sample(all_skills, num_specialties))
        return {
            skill_id: random.uniform(1.0, 2.0)
            if skill_id in specialty_skills
            else random.uniform(0.5, 1.0)
            for skill_id in all_skills
        }

    def _setup_ships(self, num_ships: int) -> None:
        """Give every planet ``num_ships`` ships, owned by its trading company."""
        if not self.planets:
            return

        # Capital and tank scale with the galaxy: a trade in a 100-planet
        # galaxy burns roughly six times the fuel of one in a five-planet
        # galaxy, and a ship that cannot fund round-trip fuel is locked out
        # of the planner entirely. Geometry is fixed after setup, so this is
        # computed once.
        mean_distance = get_navigator(self).mean_pair_distance()
        self.ship_starting_capital = starting_capital(mean_distance, 1.0)
        self.ship_fuel_capacity = fuel_capacity_for(mean_distance, 1.0)

        # Ships the galaxy starts with keep the flat "Trader-N" naming. Only a
        # ship a company builds carries the company's name, which is what
        # separates the two in exports and in the summary's fleet block.
        hull_number = 0
        for planet in self.planets:
            company, brain = self._trading_company_on(planet)
            for _ in range(num_ships):
                hull_number += 1
                efficiency = random.uniform(*SPAWNED_SHIP_EFFICIENCY_RANGE)
                capital = starting_capital(mean_distance, efficiency)
                ship = Ship(
                    name=f"Trader-{hull_number}",
                    simulation=self,
                    planet=planet,
                    fuel_capacity=fuel_capacity_for(mean_distance, efficiency),
                    fuel_efficiency=efficiency,
                    initial_money=capital,
                    owner=company,
                    retained_capital=retained_capital_for(capital),
                )
                brain.register_ship(ship)
                ship.fuel = initial_fuel_for(mean_distance, efficiency)

                self.ships.append(ship)
                planet.add_ship(ship)

    def _spawn_pending_ships(self) -> None:
        """Build the ships trading companies bought during the actor phase.

        Runs serially between the actor and ship phases, so a ship bought this
        turn flies this turn. The endowment already left the buyer's money; it
        arrives here, so the pair conserves credits. A new ship launches with
        an empty tank and buys fuel like any other docked ship.
        """
        for spawn in self.pending_ship_spawns.drain():
            brain = spawn.owner.brain
            if not isinstance(brain, TradingCompanyBrain):
                raise TypeError(
                    f"{spawn.owner.name} filed a ship spawn but is not a "
                    f"trading company (brain {type(brain).__name__})"
                )
            efficiency = random.uniform(*SPAWNED_SHIP_EFFICIENCY_RANGE)
            ship = Ship(
                name="",
                simulation=self,
                planet=spawn.planet,
                cargo_capacity=SPAWNED_SHIP_CARGO_CAPACITY,
                fuel_capacity=self.ship_fuel_capacity,
                fuel_efficiency=efficiency,
                initial_money=spawn.endowment,
                owner=spawn.owner,
                retained_capital=retained_capital_for(spawn.endowment),
            )
            ship.name = f"{spawn.owner.name}-{brain.register_ship(ship)}"
            ship.fuel = 0
            self.ships.append(ship)
            spawn.planet.add_ship(ship)

    def actors_aboard(self) -> int:
        """Regular actors currently riding a ship as a loaded passenger.

        O(ships + contracts aboard), not a scan of every actor: a boarded
        passenger is removed from ``self.actors`` (see
        ``contracts._board_passenger``) and put back by ``relocate_actor``
        on delivery or stranding, so it exists only as a LOADED contract's
        ``PassengerPayload`` in the meantime.
        """
        return sum(
            1
            for ship in self.ships
            for contract in ship.contracts
            if contract.status is ContractStatus.LOADED
            and isinstance(contract.payload, PassengerPayload)
        )

    def population(self) -> int:
        """Every actor that exists: on a planet's roster or aboard a ship.

        The invariant ``run_turn`` enforces: this never changes across a
        turn, since actors are neither created nor destroyed, only moved
        between ``self.actors`` and a ship's hold.
        """
        return len(self.actors) + self.actors_aboard()

    def run_turn(self) -> None:
        """Run one turn: actors, then ships, then market matching."""
        if self.expected_population is None:
            self.expected_population = self.population()

        self.current_turn += 1
        self.data_logger.set_turn(self.current_turn)
        for planet in self.planets:
            planet.market.set_current_turn(self.current_turn)

        # Destination scoring reads these, so they must be current before any
        # brain decides anything.
        refresh_planet_stats(self)

        # Stale offers come off the boards before any brain reads one. An
        # expired passage is counted apart: it is a migrant no ship would
        # carry, which is the failure migration v2 has to watch for.
        for planet in self.planets:
            expired = planet.contracts.expire(self.current_turn)
            self.contracts_expired += len(expired)
            self.passage_expired += sum(
                1
                for contract in expired
                if isinstance(contract.payload, PassengerPayload)
            )

        # Every board back to its quota, so an idle ship always has a job.
        refresh_government_jobs(self)

        if self.parallel_workers > 1 and len(self.planets) >= 2:
            from spacesim2.core.parallel import run_actor_phase_threaded

            run_actor_phase_threaded(self, self.parallel_workers)
        else:
            random.shuffle(self.actors)

            for actor in self.actors:
                actor.take_turn()

        # Ships bought during the actor phase join the fleet before it runs.
        self._spawn_pending_ships()

        random.shuffle(self.ships)

        for ship in self.ships:
            ship.take_turn()

        # After the ship phase, so a passenger that boarded this turn is not
        # still asking for passage, and before matching, so a contract
        # posted now reserves its money out of the same turn's book.
        run_migration_phase(self)

        self._process_markets()

        current_population = self.population()
        if current_population != self.expected_population:
            raise RuntimeError(
                f"actor conservation violated at turn {self.current_turn}: "
                f"expected {self.expected_population}, got {current_population} "
                f"({len(self.actors)} on a roster, {self.actors_aboard()} aboard)"
            )

        if self.exporter:
            self.exporter.export_turn(self, self.current_turn)

    def _process_markets(self) -> None:
        """Match orders on every market at the end of the turn."""
        for planet in self.planets:
            planet.market.match_orders()
