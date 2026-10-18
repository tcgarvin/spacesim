# Simulation Design

A turn-based economic simulation: planets with frictionless internal
economies, actors doing economic work, and ships trading between planets.

## Entities

### Actors

- Regular actors hold inventory and currency, perform one economic action
  per turn (production, labor, maintenance), and place several market orders
  per turn. When nothing else is profitable they do government work, which
  injects currency.
- Market makers are regular actors that run a market-making strategy. They
  have no special privileges.
- Ships carry commodities between planets. Each has a cargo capacity, fuel
  efficiency, and speed, and consumes refined fuel plus occasional
  maintenance commodities.

### Commodities

Defined in `data/commodities.yaml`. A commodity is transportable (tools,
raw materials, refined goods) or not (facilities).

```yaml
- id: common_metal
  name: Common Metal
  transportable: true
  description: Refined metal suitable for construction and tool-making.

- id: smelting_facility
  name: Smelting Facility
  transportable: false
  description: Infrastructure for refining metal ores.
```

### Processes

Defined in `data/processes.yaml`. A process consumes inputs, labor, tools,
and facilities and produces commodities. Facilities hold the tools; actors
supply the labor.

```yaml
- id: refine_common_metal
  name: Refine Common Metal
  inputs:
    common_metal_ore: 3
  outputs:
    common_metal: 2
  tools_required: []
  facilities_required:
    - smelting_facility
  labor: 2
  description: Smelts common metal ore into usable metal.
```

Editing either file: see the `commodity-process-design` skill.

## Markets

- Each planet has one order-matching market.
- Orders persist across turns until cancelled.
- Matching runs at the end of the turn; goods and money from a match are
  usable next turn. Details in `docs/dev-guide-simulation.md`.

## Planets and galaxy

- Planets sit on a 2D spiral (configurable arm count, default 3) joined by
  star lanes: a planar, connected graph built at setup from Delaunay and
  Gabriel edges over a spanning tree. `--lane-density` controls how many
  extra local lanes are kept. See `core/galaxy.py`.
- Travel follows lanes only. Distance is the shortest lane route, computed
  by all-pairs Dijkstra in `core/navigation.py`.
- Every planet gets random resource availability ratings (0.0-1.0). Each is
  the mean of a per-resource land curve; the planet generates 100 lands from
  those curves, and every regular actor claims one at placement and keeps it
  for the run. Biomass is drawn uniform(-0.2, 1.0) and clamped at 0.0, so
  1/6 of planets have no biomass. Hand-made food breaks even at biomass
  0.167, so a barren planet cannot feed itself: it imports food or loses its
  residents to migration. Gathering and mining yields read the actor's land, so a
  planet's supply of a resource is the sorted list of its residents' draws.
  See the Planet Attributes and Land section of `CLAUDE.md`,
  `core/planet_attributes.py`, and `core/land.py`.
- Populations are fixed. Actors aim to meet basic needs (`docs/needs.md`).

## Interplanetary trade

- Ships burn refined fuel and occasionally maintenance commodities.
- Travel time and fuel scale with lane-route length. There are no travel
  risks.
- A multi-lane journey is one flight: fuel for the whole route is loaded at
  departure and intermediate planets are passed without docking. Hop-by-hop
  refuelling and trading en route is future work.

## Money

Currency enters through government work, which pays a fixed daily wage.
Money is a debt owed by the government.

## Economic graph

Commodities and processes form a directed graph. Brains walk it to find
opportunities given market prices and inventory. Render it with
`uv run spacesim dev graph`.

## Turn execution

Each turn: actors act in random order, then ships, then every market
matches. Each actor performs one economic action and places several market
orders. Results of market actions are available next turn. The full order
is in `docs/dev-guide-simulation.md`.

## Future extensions

- Population growth and decay.
- Skill-based labor markets.
- Government controls (taxes, subsidies).
- Market information delays.
- Travel hazards and piracy.
