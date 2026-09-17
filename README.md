# SpaceSim2

A turn-based economic simulation of interplanetary trade with actors,
markets, and ships.

- **Planets** with their own resources and local markets
- **Actors** who produce, consume, and trade
- **Ships** that carry goods between planets along star lanes
- **Markets** that match buy and sell orders per commodity
- **Commodities** that are gathered, refined, and traded

Design: [docs/sim-design.md](docs/sim-design.md).

## Installation

```bash
# Install the package
uv sync

# Install with analysis dependencies (for batch analysis and notebooks)
uv sync --extra analysis
```

## Usage

```bash
# Live galaxy view (Pygame)
uv run spacesim2 ui

# Headless run with data export
uv run spacesim2 run --turns 200

# Compact KPI summary with PASS/WARN/FAIL verdict
uv run spacesim2 run --turns 200 --no-export --quiet --summary

# Same run on the free-threaded side venv with a threaded actor phase
# (faster, same KPIs; setup in docs/performance.md)
.venv-ft/bin/python -m spacesim2.cli.main run --workers 12 --turns 200 --no-export --quiet --summary

# Run an ad-hoc analysis script against the latest exported run
uv run spacesim2 dev analyze notebooks/healthcheck_probe.py

# Run with data export and open the Marimo dashboard notebook
uv run spacesim2 run --notebook

# Generate commodity/process dependency graph
uv run spacesim2 dev graph

# See all commands and options
uv run spacesim2 --help
```

Both `ui` and `run` take `--planets`, `--actors`, `--makers`, and `--ships`
to size the galaxy.

## Development

[CLAUDE.md](CLAUDE.md) has the full workflow: tests, type check, lint, and
the simulation evaluation loop. Quick start:

```bash
uv sync --all-extras       # dev + analysis dependencies
uv run pytest              # tests
uv run spacesim2 dev check # full verify: format, lint, types, tests, sim run
```
