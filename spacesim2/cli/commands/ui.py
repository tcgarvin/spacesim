"""UI command implementation."""

import argparse

from spacesim2.cli.common import create_and_setup_simulation
from spacesim2.cli.output import print_error
from spacesim2.core.galaxy import DEFAULT_ARMS, DEFAULT_LANE_DENSITY


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'ui' subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "ui",
        help="Launch interactive Pygame UI",
        description="Run simulation with interactive graphical interface",
    )

    parser.add_argument(
        "--planets", type=int, default=100, help="Number of planets (default: 100)"
    )
    parser.add_argument(
        "--arms",
        type=int,
        default=DEFAULT_ARMS,
        help=f"Spiral arms in the galaxy layout (default: {DEFAULT_ARMS})",
    )
    parser.add_argument(
        "--lane-density",
        type=float,
        default=DEFAULT_LANE_DENSITY,
        help="Fraction of optional local star lanes kept beyond the spanning "
        f"tree, 0..1 (default: {DEFAULT_LANE_DENSITY})",
    )
    parser.add_argument(
        "--actors",
        type=int,
        default=50,
        help="Number of regular actors per planet (default: 50)",
    )
    parser.add_argument(
        "--makers",
        type=int,
        default=2,
        help="Number of market makers per planet (default: 2)",
    )
    parser.add_argument(
        "--operators",
        type=int,
        default=2,
        help="Number of spaceport operators per planet (default: 2)",
    )
    parser.add_argument(
        "--ships", type=int, default=1, help="Number of ships (default: 1)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Simulation speed in turns per second (default: 1.0)",
    )
    parser.add_argument(
        "--paused",
        action="store_true",
        help="Start paused (press Space to play)",
    )

    parser.set_defaults(func=execute)
    return parser


def execute(args: argparse.Namespace) -> int:
    """Execute the UI command and return the exit code."""
    # Imported lazily so pygame and its startup banner load only when the UI
    # is launched, not on every CLI invocation.
    from spacesim2.ui.live.app import PYGAME_AVAILABLE, LiveGalaxyApp

    if not PYGAME_AVAILABLE:
        print_error("pygame not available. Install with: uv pip install pygame")
        return 1

    simulation = create_and_setup_simulation(
        planets=args.planets,
        actors=args.actors,
        makers=args.makers,
        operators=args.operators,
        ships=args.ships,
        arms=args.arms,
        lane_density=args.lane_density,
    )

    app = LiveGalaxyApp(simulation, speed=args.speed, paused=args.paused)
    app.run()

    return 0
