"""Main CLI entry point for SpaceSim2."""

import argparse
import sys
from typing import Optional

from spacesim2.cli.commands import run, ui
from spacesim2.cli.commands.dev import ab, analyze, check, graph


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="spacesim2",
        description="SpaceSim2 - Interplanetary Economic Simulation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(
        dest="command", help="Available commands", required=True
    )

    ui.add_parser(subparsers)
    run.add_parser(subparsers)

    dev_parser = subparsers.add_parser("dev", help="Development tools")
    dev_subparsers = dev_parser.add_subparsers(
        dest="dev_command", help="Development subcommands", required=True
    )
    graph.add_parser(dev_subparsers)
    analyze.add_parser(dev_subparsers)
    check.add_parser(dev_subparsers)
    ab.add_parser(dev_subparsers)

    return parser


def main(argv: Optional[list] = None) -> int:
    """Run the CLI and return the exit code.

    ``argv`` defaults to ``sys.argv``.
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
