"""Generate raw provider art for manifest entries and post-process to candidates.

    uv run python tools/assetgen/generate.py                 # all entries
    uv run python tools/assetgen/generate.py --id terran     # one entry
    uv run python tools/assetgen/generate.py --category good
    uv run python tools/assetgen/generate.py --force         # regenerate raw frames

Writes raw output under staging/raw/ and the cleaned sprite to
staging/candidates/<id>.png. Review candidates, flip ``status: approved`` in the
manifest, then run promote.py. Rotation entries skip raw frames that already
exist so a partially-failed run resumes without paying for them twice.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from postprocess import process_file, process_strip

_HERE = Path(__file__).resolve().parent

# House-style manifest key appended to a prompt, keyed by asset category.
_STYLE_KEYS: dict[str, str] = {
    "planet": "planet_style",
    "good": "good_style",
    "ship": "ship_style",
}

# Ship facings, counterclockwise from east (increasing angle with y-up). The
# base image faces east; the rest are derived by rotation and packed in order.
_ROTATION_ORDER: tuple[str, ...] = ("e", "ne", "n", "nw", "w", "sw", "s", "se")


def _load_manifest() -> dict:
    manifest: dict = yaml.safe_load((_HERE / "manifest.yaml").read_text())
    return manifest


def _full_prompt(manifest: dict, entry: dict) -> str:
    prompt = str(entry["prompt"]).strip()
    style_key = _STYLE_KEYS.get(str(entry["category"]))
    if style_key is None:
        return prompt
    return f"{prompt} {str(manifest[style_key]).strip()}"


def _require_int(entry: dict, field: str) -> int:
    if field not in entry:
        raise SystemExit(f"manifest entry {entry.get('id')!r} missing '{field}'")
    return int(entry[field])


def _generate_object(manifest: dict, entry: dict, *, force: bool) -> None:
    from pixellab import generate_object

    entry_id = str(entry["id"])
    size = _require_int(entry, "size")
    raw = _HERE / "staging" / "raw" / f"{entry_id}.png"
    candidate = _HERE / "staging" / "candidates" / f"{entry_id}.png"
    if force or not raw.exists():
        generate_object(_full_prompt(manifest, entry), raw, size=size)
    process_file(raw, candidate, size=size, kind="object")
    print(f"  -> {candidate}")


def _generate_rotation(manifest: dict, entry: dict, *, force: bool) -> None:
    from pixellab import generate_object, rotate

    entry_id = str(entry["id"])
    size = _require_int(entry, "size")
    directions = _require_int(entry, "directions")
    if directions != len(_ROTATION_ORDER):
        raise SystemExit(
            f"{entry_id}: only {len(_ROTATION_ORDER)}-direction rotation is supported"
        )
    raw_dir = _HERE / "staging" / "raw" / entry_id
    base = raw_dir / "e.png"
    if force or not base.exists():
        generate_object(
            _full_prompt(manifest, entry),
            base,
            size=size,
            view="high top-down",
            direction="e",
        )
    for facing in _ROTATION_ORDER[1:]:
        frame = raw_dir / f"{facing}.png"
        if not force and frame.exists():
            continue
        rotate(base, frame, from_direction="e", to_direction=facing, size=size)
    candidate = _HERE / "staging" / "candidates" / f"{entry_id}.png"
    process_strip([raw_dir / f"{d}.png" for d in _ROTATION_ORDER], candidate, size)
    print(f"  -> {candidate}")


def _generate_entry(manifest: dict, entry: dict, *, force: bool) -> None:
    provider = str(entry["provider"])
    kind = str(entry["kind"])
    print(f"generating {entry['id']} ({provider}/{kind}) ...")
    if provider == "nanobanana":
        from nanobanana import generate_image

        raw = _HERE / "staging" / "raw" / f"{entry['id']}.png"
        candidate = _HERE / "staging" / "candidates" / f"{entry['id']}.png"
        generate_image(_full_prompt(manifest, entry), raw)
        process_file(raw, candidate, size=_require_int(entry, "size"))
        print(f"  -> {candidate}")
    elif provider == "pixellab":
        if kind == "object":
            _generate_object(manifest, entry, force=force)
        elif kind == "rotation":
            _generate_rotation(manifest, entry, force=force)
        else:
            raise SystemExit(f"pixellab provider cannot handle kind '{kind}'")
    else:
        raise SystemExit(f"unknown provider '{provider}' for {entry['id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="generate only this asset id")
    parser.add_argument("--category", help="generate only this category")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate raw frames that already exist on disk",
    )
    args = parser.parse_args()

    manifest = _load_manifest()
    entries = manifest["assets"]
    if args.id:
        entries = [e for e in entries if e["id"] == args.id]
    if args.category:
        entries = [e for e in entries if e["category"] == args.category]
    if not entries:
        raise SystemExit("no matching manifest entries")

    for entry in entries:
        _generate_entry(manifest, entry, force=args.force)


if __name__ == "__main__":
    main()
