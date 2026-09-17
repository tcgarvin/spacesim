# Commodities and Processes

Commodities and production processes are data: `data/commodities.yaml` and
`data/processes.yaml`. The economy can be extended without code changes.

## Commodities

Each entry has `id`, `name`, `transportable` (can ships carry it; false for
facilities), an optional `tradable`, and `description`. `CommodityRegistry`
(`core/commodity.py`) loads them. Everywhere in the codebase a commodity is a
`CommodityDefinition` object, never a string id.

`tradable` is whether the good can be bought and sold on a planet market at
all. It defaults to `transportable`, so only a good that breaks the two apart
has to say so. `ship_hull` is the only one: too large to load as cargo, but
sold on the market of the planet that built it. A transportable good that is
not tradable is rejected at load time, since ships buy and sell everything
they carry.

The market itself enforces nothing here: `place_buy_order` and
`place_sell_order` accept any commodity. What keeps facilities off the book is
the brains, which skip an untradable good when they list surplus stock, and
value an untradable recipe output at `FACILITY_NOTIONAL_VALUE` instead of a
market price. Market makers and ship brains iterate the transportable goods,
so neither quotes nor hauls a hull.

## Processes

Each entry has `id`, `name`, `inputs` and `outputs` (commodity id to
quantity), `tools_required`, `facilities_required`, `labor`,
`relevant_skills` and `description`. `ProcessRegistry` (`core/process.py`)
loads them and needs a `CommodityRegistry` to resolve ids to
`CommodityDefinition` objects. Full schema: the `commodity-process-design`
skill.

## Inventory

`Inventory` (`core/commodity.py`) tracks commodities for actors and ships.
All methods take `CommodityDefinition` objects.

| Method | Purpose |
|--------|---------|
| `add_commodity(commodity, quantity)` | Add stock |
| `remove_commodity(commodity, quantity)` | Remove stock |
| `has_quantity(commodity, quantity)` | Enough in stock? |
| `get_quantity(commodity)` | Total held |
| `get_available_quantity(commodity)` | Unreserved amount |
| `reserve_commodity(commodity, quantity)` | Hold stock for a market order |
| `unreserve_commodity(commodity, quantity)` | Release a hold |

## Process execution

`ProcessCommand.execute(actor)` in `core/commands.py` runs a process. It
checks inputs, tools and facilities, applies the skill check
(`docs/skills.md`) and the planet resource attribute, then consumes inputs
and adds outputs. Brains pick processes by market profitability; see
`_find_most_profitable_process` in `core/brains/colonist.py`.

## Market

Orders, transactions and price histories are keyed by `CommodityDefinition`.
Market makers quote liquidity from their inventory levels. Sellers reserve
inventory while an order rests and unreserve on cancel.

## Facilities

Facilities are commodities with `transportable: false` that appear in a
process's `facilities_required`. They are not consumed when the process runs.
The tier-2 facilities are `textile_mill`, `chemistry_lab`, `chemical_plant`,
`precision_forge`, `electronics_workshop` and `farm`; each has a `build_*`
process taking simple building materials and tier-1 inputs. The tier-3
facilities are `advanced_factory`, `shipyard` and the operator-owned
`spaceport`. Only the spaceport has a maintenance drive; a facility an
industrialist builds for itself has no upkeep.

`heavy_machinery` is a tier-1-to-2 industrial good, transportable and
consumed, not a facility itself: `build_chemical_plant` and `build_farm` both
take it as a build input, and `process_food` and `farm_biomass` both draw on
it as upkeep (a 0.01 per-run chance of consuming 1 unit; see "Upkeep" in
CLAUDE.md).

## Capital

A process may carry a `capital` mapping of commodity id to an output bonus
fraction, the same shape as `upkeep`:

```yaml
capital:
  computers: 0.25
```

Holding at least 1 unit of the good multiplies the run's output by
`1 + bonus`; holding more adds nothing, and bonuses from several capital
goods sum. Capital is not a precondition and is never consumed by a run: it
breaks with probability `CAPITAL_BREAK_PROBABILITY` (0.005) after a
successful run, a mean life of 200 runs, five times a tool's. Upkeep draws
are never scaled by the bonus.

Every process with a non-empty `facilities_required` lists
`computers: 0.25`, 21 processes in all. Gathering and hand recipes list none:
a computer does not help pick berries. `IndustrialistBrain` keeps 1 unit of
each capital good its chosen recipe lists, bids for it at the extra output
over its expected life (`_capital_willingness_to_pay`), and never lists that
unit for sale. Colonists do not buy computers.

## Shipbuilding

A starship hull is assembled in two stages, so no single run has to be funded
in one lump:

| Recipe | Facility | Inputs | Imputed cost |
|--------|----------|--------|--------------|
| `make_ship_hull_section` | `advanced_factory` | 3 advanced_building_materials, 4 common_metal, 2 precision_parts, 1 electronics | 652 |
| `make_ship_hull` | `shipyard` | 5 ship_hull_section, 2 ship_components, 1 computers, 2 heavy_machinery | 5531 |
| `build_shipyard` | none | 10 simple_building_materials, 8 advanced_building_materials, 2 heavy_machinery, simple_tools | 918 |

Costs are imputed at the 100-planet trade prices (computers 502, electronics
194, heavy_machinery 138, common_metal 31, simple_building_materials 12) with
sections at 780 and ship_components at 420. A hull therefore clears the
industrialist's 1.2x entry margin against a bid of about 6,600 and up.

`ship_hull_section` is an ordinary transportable good, so sections can be made
on one planet and shipped to a shipyard on another. The hull cannot.

## Adding commodities or processes

Use the `commodity-process-design` skill. It covers the schemas, validation,
the bootstrap-path check and the dependency graph.
