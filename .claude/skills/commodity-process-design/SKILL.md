---
name: commodity-process-design
description: Edit commodities and production processes for the economic simulation. Use when adding/modifying commodities, recipes, production chains, tool requirements, facility requirements, or resource attributes. Also covers generating dependency graphs.
---

# Commodity & Process Design

How to edit the simulation's commodities and production processes.

## Data Files

| File | Purpose |
|------|---------|
| `data/commodities.yaml` | Tradeable goods and facilities |
| `data/processes.yaml` | Production recipes |

`CLAUDE.md` has the bootstrap path, planet attributes and drive mappings.
`docs/needs.md` lists which commodity each drive consumes.

## Commodity Schema

```yaml
- id: commodity_id           # snake_case, unique identifier
  name: Display Name         # Human-readable name
  transportable: true/false  # Can ships carry this? (false for facilities)
  tradable: true/false       # Optional; can a market trade it. Defaults to
                             # transportable. Only ship_hull sets it apart.
  description: Text          # What is this commodity?
```

## Process Schema

```yaml
- id: process_id
  name: Display Name
  inputs:                    # Commodities consumed (empty {} for gathering)
    commodity_id: quantity
  outputs:                   # Commodities produced
    commodity_id: quantity
  tools_required: []         # List of tool commodity IDs, or empty []
  facilities_required: []    # List of facility commodity IDs, or empty []
  labor: 1                   # Labor units required
  relevant_skills:           # Skills that improve efficiency
    - skill_name
  description: Text
  resource_attribute:        # Optional: for gathering/mining processes only
    commodity: attribute_id  # Planet attribute to check (see CLAUDE.md)
    effect: output|success   # output=reduced yield, success=may fail
  upkeep:                    # Optional: per-run chance of consuming 1 unit
    commodity_id: 0.01       # Probability in (0, 1]
  capital:                   # Optional: output bonus for holding 1 unit
    computers: 0.25          # Fraction in (0, 1]
```

## Constraints

- **Bootstrap path.** Actors start with nothing and must be able to reach
  tools without tools. After any process change, confirm a tool-free path
  to the first tools still exists; the graph below shows it.
- **Resource attributes.** A new gathering or extraction process tied to a
  planet resource needs an attribute on `PlanetAttributes` in
  `core/planet_attributes.py` and a `resource_attribute` field on the
  process.
- **Upkeep.** An `upkeep` entry is rolled once per run. A hit needs 1 unit
  on hand or the run fails with no side effects, so upkeep goods must be
  buyable on the planets that run the process. Keep the probability low
  enough that the expected cost per run stays below the recipe's margin.
- **Capital.** A `capital` entry raises the run's output by its fraction
  while the actor holds 1 unit of the good. It is never consumed by the run
  and breaks at `CAPITAL_BREAK_PROBABILITY` (0.005) per successful run.
  Every facility-gated process lists `computers: 0.25`; gathering and hand
  recipes list none.
- **Drives.** Changing the commodity a drive consumes means updating that
  drive class in `core/drives/`.

## Workflow

### 1. Edit
Edit `data/commodities.yaml` and/or `data/processes.yaml`.

### 2. Verify syntax
```bash
uv run python -c "import yaml; yaml.safe_load(open('data/commodities.yaml')); yaml.safe_load(open('data/processes.yaml')); print('OK')"
```

### 3. Generate the dependency graph
```bash
uv run spacesim2 dev graph
```

Outputs `tmp/commodity-graph.svg` and `tmp/commodity-graph.mmd`.

### 4. Review the graph

Node colors: green for consumables, amber for tools (`tools_required`, not
consumed), blue for facilities (`facilities_required`, not consumed).
Process labels show `[Tool, Facility]` requirements below the name.

Check that:
- every commodity connects to at least one process
- no process references a missing input commodity
- a path from nothing to tools exists
- there are no impossible cycles

### 5. Run tests
```bash
uv run pytest tests/ -v
```

### 6. Update documentation
If the bootstrap path or requirements changed, update the matching section
in `CLAUDE.md`.
