"""Graph generation command implementation."""

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from spacesim.cli.output import print_error, print_success


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore
    """Add the 'graph' dev subcommand parser."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "graph",
        help="Generate commodity/process dependency graph",
        description="Convert YAML commodity/process definitions to Mermaid diagrams and render",
    )

    parser.add_argument(
        "--out",
        "-o",
        type=str,
        default=None,
        help="Save output to this base filename (no extension)",
    )
    parser.add_argument(
        "--format",
        "-f",
        type=str,
        choices=["svg", "png", "pdf"],
        default="svg",
        help="Output format (default: svg)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the output diagram after rendering",
    )

    parser.set_defaults(func=execute)
    return parser


def _load_yaml(file_path: Path) -> List[Dict[str, Any]]:
    """Load a YAML file."""
    with open(file_path, "r") as f:
        return yaml.safe_load(f)  # type: ignore


def _categorize_commodities(
    things: List[Dict[str, Any]], processes: List[Dict[str, Any]]
) -> Dict[str, str]:
    """Map each commodity id to "consumable", "tool", or "facility"."""
    tools_used: set[str] = set()
    for process in processes:
        tools_used.update(process.get("tools_required", []))

    categories: Dict[str, str] = {}
    for thing in things:
        thing_id = thing["id"]
        if not thing.get("tradable", thing.get("transportable", True)):
            categories[thing_id] = "facility"
        elif thing_id in tools_used:
            categories[thing_id] = "tool"
        else:
            categories[thing_id] = "consumable"
    return categories


def _generate_mermaid_styles() -> List[str]:
    """Mermaid classDef lines for commodity categories."""
    return [
        "    %% Style definitions",
        "    classDef consumable fill:#e8f4ea,stroke:#2d6a4f",
        "    classDef tool fill:#fff3cd,stroke:#856404",
        "    classDef facility fill:#cce5ff,stroke:#004085",
    ]


def _generate_mermaid_things(
    things: List[Dict[str, Any]], categories: Dict[str, str]
) -> Tuple[List[str], Dict[str, str]]:
    """Mermaid node lines for commodities and facilities.

    Returns:
        ``(lines, id_to_label)``.
    """
    lines = ["", "    %% Commodities & Facilities"]
    id_to_label: Dict[str, str] = {}
    for thing in things:
        label = thing["name"].replace('"', '\\"')
        node_id = thing["id"]
        id_to_label[node_id] = label
        category = categories.get(node_id, "consumable")
        lines.append(f'    {node_id}["{label}"]:::{category}')
    return lines, id_to_label


def _generate_mermaid_processes(
    processes: List[Dict[str, Any]], id_to_label: Dict[str, str]
) -> Tuple[List[str], List[Tuple[str, Dict[str, Any]]]]:
    """Mermaid node lines for processes.

    Returns:
        ``(lines, process_ids)`` where each process id is paired with its data.
    """
    lines = ["\n    %% Recipes"]
    process_ids: List[Tuple[str, Dict[str, Any]]] = []
    for i, process in enumerate(processes, start=1):
        pid = f"R{i}"
        process_ids.append((pid, process))
        # Quoted labels allow special characters such as parentheses.
        label = process["name"].replace('"', '\\"')

        requirements: List[str] = []
        for tool in process.get("tools_required", []):
            requirements.append(id_to_label.get(tool, tool))
        for fac in process.get("facilities_required", []):
            requirements.append(id_to_label.get(fac, fac))

        # Requirements go in brackets under the process name.
        if requirements:
            req_text = ", ".join(requirements)
            label = f"{label}<br/>[{req_text}]"

        lines.append(f'    {pid}[["{label}"]]')
    return lines, process_ids


def _generate_mermaid_edges(
    process_ids: List[Tuple[str, Dict[str, Any]]], id_to_label: Dict[str, str]
) -> List[str]:
    """Mermaid edge lines for process inputs and outputs."""
    lines = ["\n    %% Graph Edges: Inputs and Outputs"]
    for pid, process in process_ids:
        for output in process.get("outputs", {}):
            lines.append(f"    {pid} --> {output}")
        for input_ in process.get("inputs", {}):
            lines.append(f"    {input_} --> {pid}")
        # Tool and facility requirements appear in process labels, not edges.
    return lines


def _generate_mermaid(things_yaml: Path, processes_yaml: Path) -> str:
    """Generate Mermaid diagram code from the commodity and process YAML."""
    things = _load_yaml(things_yaml)
    processes = _load_yaml(processes_yaml)

    categories = _categorize_commodities(things, processes)

    lines = ["flowchart BT"]

    lines.extend(_generate_mermaid_styles())

    thing_lines, id_to_label = _generate_mermaid_things(things, categories)
    lines.extend(thing_lines)

    proc_lines, process_ids = _generate_mermaid_processes(processes, id_to_label)
    lines.extend(proc_lines)

    edge_lines = _generate_mermaid_edges(process_ids, id_to_label)
    lines.extend(edge_lines)

    return "\n".join(lines)


def _run_mmdc(
    args: list[str], tmpdir: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run mermaid-cli via npx.

    Args:
        tmpdir: Optional temp directory for config files.

    Raises:
        FileNotFoundError: If npx is not found.
        subprocess.CalledProcessError: If rendering fails.
    """
    extra_args: list[str] = []

    # Puppeteer config works around Chromium sandbox failures on Linux.
    if tmpdir is not None:
        puppeteer_config = tmpdir / "puppeteer-config.json"
        puppeteer_config.write_text(
            '{"args": ["--no-sandbox", "--disable-setuid-sandbox"]}'
        )
        extra_args = ["-p", str(puppeteer_config)]

    return subprocess.run(
        ["npx", "-y", "@mermaid-js/mermaid-cli", *args, *extra_args],
        check=True,
        capture_output=True,
    )


def _render_mermaid_to_file(mermaid_code: str, output_path: Path, format: str) -> bool:
    """Render mermaid code to ``output_path``. Returns True on success.

    The .mmd source is saved next to the output for inspection.
    """
    mmd_path = output_path.with_suffix(".mmd")
    mmd_path.write_text(mermaid_code)
    print_success(f"Mermaid source saved to {mmd_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        try:
            _run_mmdc(
                ["-i", str(mmd_path), "-o", str(output_path), "-f", format], tmpdir_path
            )
            print_success(f"Diagram saved to {output_path}")
            return True
        except FileNotFoundError:
            print_error(
                "npx not found. Please ensure Node.js is installed and npx is on PATH."
            )
            return False
        except subprocess.CalledProcessError as e:
            print_error(f"Rendering failed: {e.stderr.decode() if e.stderr else e}")
            return False


def _open_rendered_file(path: Path) -> None:
    """Open the rendered file in the editor."""
    try:
        subprocess.run(["code", str(path)], check=False)
    except Exception:
        print("Unable to open file automatically.")


def execute(args: argparse.Namespace) -> int:
    """Execute the graph command and return the exit code."""
    commodities_yaml = Path("data/commodities.yaml")
    processes_yaml = Path("data/processes.yaml")

    if not commodities_yaml.exists():
        print_error(f"Commodities file not found: {commodities_yaml}")
        return 1

    if not processes_yaml.exists():
        print_error(f"Processes file not found: {processes_yaml}")
        return 1

    mermaid_code = _generate_mermaid(commodities_yaml, processes_yaml)

    out_base = args.out if args.out else "tmp/commodity-graph"
    output_path = Path(f"{out_base}.{args.format}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    success = _render_mermaid_to_file(mermaid_code, output_path, args.format)
    if success and args.open:
        _open_rendered_file(output_path)
    return 0 if success else 1
