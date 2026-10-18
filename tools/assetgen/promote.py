"""Promote approved candidate sprites into the shipped runtime asset tree.

Copies staging/candidates/<id>.png -> spacesim2/ui/live/assets/<category>s/<id>.png
for every manifest entry with ``status: approved``, rewrites the per-category
index the runtime loader reads, and records provenance in ATTRIBUTION.md.

Index formats:
  planets, goods   {"ids": [...]}                              (loaded by name/hash)
  ships            {"ships": [{"id", "frames", "size"}, ...]}  (strip PNG alongside)

    uv run python tools/assetgen/promote.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
_ASSET_ROOT = _HERE.parents[1] / "spacesim2" / "ui" / "live" / "assets"


def promote(manifest: dict, candidates_dir: Path, asset_root: Path) -> None:
    """Promote approved candidates from ``candidates_dir`` into ``asset_root``."""
    approved = [e for e in manifest["assets"] if e.get("status") == "approved"]
    if not approved:
        raise SystemExit("no approved assets in manifest")

    id_index: dict[str, list[str]] = {}  # category -> ids (planets, goods)
    ships: list[dict[str, Any]] = []
    attribution: list[str] = []
    for entry in approved:
        entry_id = str(entry["id"])
        category = str(entry["category"])
        candidate = candidates_dir / f"{entry_id}.png"
        dest = asset_root / f"{category}s" / f"{entry_id}.png"
        if candidate.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, dest)
            print(f"promoted {category}/{entry_id}")
        elif not dest.exists():
            # No fresh candidate and never promoted: nothing to index yet.
            print(f"skip {entry_id}: no candidate (run generate.py first)")
            continue
        if category == "ship":
            ships.append(
                {
                    "id": entry_id,
                    "frames": int(entry["directions"]),
                    "size": int(entry["size"]),
                }
            )
        else:
            id_index.setdefault(category, []).append(entry_id)
        attribution.append(
            f"- `{category}/{entry_id}.png` — {entry['provider']}: "
            f"{str(entry['prompt']).strip()}"
        )

    for category, ids in id_index.items():
        index = asset_root / f"{category}s" / "index.json"
        index.write_text(json.dumps({"ids": sorted(ids)}, indent=2) + "\n")

    if ships:
        index = asset_root / "ships" / "index.json"
        ships.sort(key=lambda s: str(s["id"]))
        index.write_text(json.dumps({"ships": ships}, indent=2) + "\n")

    (asset_root / "ATTRIBUTION.md").write_text(
        "# Asset attribution\n\n"
        "Generated offline by tools/assetgen. Provider + prompt per asset:\n\n"
        + "\n".join(sorted(attribution))
        + "\n"
    )


def main() -> None:
    manifest = yaml.safe_load((_HERE / "manifest.yaml").read_text())
    promote(manifest, _HERE / "staging" / "candidates", _ASSET_ROOT)


if __name__ == "__main__":
    main()
