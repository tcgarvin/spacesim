"""Tests for the offline asset pipeline. No network, no API keys.

Provider HTTP is mocked, so nothing here reaches PixelLab. The pipeline scripts
live in ``tools/assetgen`` and are not an installed package, so they are loaded
by path. The loader also registers them under their bare module names and puts
their directory on ``sys.path`` so sibling imports such as ``from postprocess
import ...`` resolve.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

_ASSETGEN = Path(__file__).resolve().parents[1] / "tools" / "assetgen"


def _load(name: str) -> types.ModuleType:
    if str(_ASSETGEN) not in sys.path:
        sys.path.insert(0, str(_ASSETGEN))
    spec = importlib.util.spec_from_file_location(name, _ASSETGEN / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _png_bytes(color: tuple[int, int, int, int] = (200, 60, 60, 255)) -> bytes:
    img = Image.new("RGBA", (8, 8), color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _sprite_png_bytes() -> bytes:
    """A small opaque blob on a transparent RGBA canvas, so it has an alpha bbox."""
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for y in range(4, 12):
        for x in range(4, 12):
            img.putpixel((x, y), (30, 200, 120, 255))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _b64_png() -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes()).decode("ascii")


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's return value."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


# --------------------------------------------------------------------------- #
# postprocess: pure image transforms
# --------------------------------------------------------------------------- #


def _planet_on_black(size: int = 200, radius: int = 70) -> Image.Image:
    yy, xx = np.mgrid[0:size, 0:size]
    inside = (yy - size / 2) ** 2 + (xx - size / 2) ** 2 <= radius**2
    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    rgb[inside] = (120, 160, 220)
    return Image.fromarray(rgb, mode="RGB")


def _sprite_on_alpha(w: int, h: int, box: tuple[int, int, int, int]) -> Image.Image:
    """An opaque coloured rectangle on an otherwise transparent RGBA canvas."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            img.putpixel((x, y), (30, 200, 120, 255))
    return img


def test_disc_alpha_crop_makes_corners_transparent_and_center_opaque() -> None:
    module = _load("postprocess")
    sprite = module.disc_alpha_crop(_planet_on_black(), size=128)

    assert sprite.size == (128, 128)
    assert sprite.mode == "RGBA"
    alpha = np.asarray(sprite)[..., 3]
    assert alpha[0, 0] == 0
    assert alpha[-1, -1] == 0
    assert alpha[64, 64] == 255


def test_disc_alpha_crop_rejects_fully_dark_image() -> None:
    module = _load("postprocess")
    black = Image.new("RGB", (64, 64), (0, 0, 0))
    with pytest.raises(ValueError):
        module.disc_alpha_crop(black)


def test_trim_pad_square_preserves_alpha_and_is_square_and_crisp() -> None:
    module = _load("postprocess")
    # Off-centre, non-square content inside a larger transparent frame.
    src = _sprite_on_alpha(40, 40, (4, 8, 20, 16))
    out = module.trim_pad_square(src, 32)

    assert out.size == (32, 32)
    assert out.mode == "RGBA"
    arr = np.asarray(out)
    # Content survives and is opaque somewhere; the border stays transparent.
    assert arr[..., 3].max() == 255
    assert arr[0, 0, 3] == 0
    assert arr[-1, -1, 3] == 0
    # NEAREST keeps hard edges, so alpha is binary with no anti-aliased ramp.
    assert set(np.unique(arr[..., 3]).tolist()) <= {0, 255}


def test_trim_pad_square_rejects_fully_transparent() -> None:
    module = _load("postprocess")
    with pytest.raises(ValueError):
        module.trim_pad_square(Image.new("RGBA", (16, 16), (0, 0, 0, 0)), 32)


def test_key_background_makes_uniform_corner_transparent() -> None:
    module = _load("postprocess")
    img = Image.new("RGBA", (10, 10), (10, 20, 30, 255))
    img.putpixel((5, 5), (200, 60, 60, 255))
    keyed = module.key_background(img)
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # background keyed out
    assert arr[5, 5, 3] == 255  # foreground kept


def test_pack_strip_width_and_frame_order() -> None:
    module = _load("postprocess")
    colors = [(i * 30, 0, 0, 255) for i in range(1, 5)]
    frames = [Image.new("RGBA", (16, 16), c) for c in colors]
    strip = module.pack_strip(frames, 16)

    assert strip.size == (16 * 4, 16)
    arr = np.asarray(strip)
    # Each 16px column block keeps its source frame's red value in order.
    for i, color in enumerate(colors):
        assert arr[8, i * 16 + 8, 0] == color[0]


def test_pack_strip_rejects_empty() -> None:
    module = _load("postprocess")
    with pytest.raises(ValueError):
        module.pack_strip([], 16)


# --------------------------------------------------------------------------- #
# pixellab provider: mocked HTTP
# --------------------------------------------------------------------------- #


def _pixellab_with_key(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = _load("pixellab")
    monkeypatch.setattr(module, "_api_key", lambda: "test-token")
    return module


def test_generate_object_posts_and_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _pixellab_with_key(monkeypatch)
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float = 0.0) -> _FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["auth"] = request.headers.get("Authorization")  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return _FakeResponse({"image": {"base64": _b64_png()}})

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    out = tmp_path / "icon.png"
    result = module.generate_object("a glowing canister", out, size=32)

    assert result == out and out.exists()
    assert captured["url"] == "https://api.pixellab.ai/v2/create-image-pixflux"
    assert captured["auth"] == "Bearer test-token"
    body = captured["body"]
    assert body["description"] == "a glowing canister"  # type: ignore[index]
    assert body["image_size"] == {"width": 32, "height": 32}  # type: ignore[index]
    assert body["no_background"] is True  # type: ignore[index]
    # A valid PNG was decoded from the base64 payload.
    assert Image.open(out).size == (8, 8)


def test_rotate_posts_from_image_and_maps_directions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _pixellab_with_key(monkeypatch)
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float = 0.0) -> _FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return _FakeResponse({"image": {"base64": _b64_png()}})

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    base = tmp_path / "e.png"
    base.write_bytes(_png_bytes())
    out = tmp_path / "ne.png"
    module.rotate(base, out, from_direction="e", to_direction="ne", size=48)

    assert out.exists()
    assert captured["url"] == "https://api.pixellab.ai/v2/rotate"
    body = captured["body"]
    assert body["from_direction"] == "east"  # type: ignore[index]
    assert body["to_direction"] == "north-east"  # type: ignore[index]
    assert body["image_size"] == {"width": 48, "height": 48}  # type: ignore[index]
    assert body["from_image"]["type"] == "base64"  # type: ignore[index]


def test_rotate_rejects_unknown_direction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _pixellab_with_key(monkeypatch)
    base = tmp_path / "e.png"
    base.write_bytes(_png_bytes())
    with pytest.raises(ValueError):
        module.rotate(
            base, tmp_path / "x.png", from_direction="e", to_direction="up", size=48
        )


def test_provider_raises_on_non_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _pixellab_with_key(monkeypatch)

    def boom(request: object, timeout: float = 0.0) -> _FakeResponse:
        raise urllib.error.HTTPError(
            "https://api.pixellab.ai/v2/create-image-pixflux",
            422,
            "Unprocessable Entity",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"detail":"bad size"}'),
        )

    monkeypatch.setattr(module.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="422"):
        module.generate_object("x", tmp_path / "x.png", size=32)


# --------------------------------------------------------------------------- #
# generate dispatch: providers monkeypatched, no HTTP
# --------------------------------------------------------------------------- #


def _rotation_manifest() -> dict:
    return {
        "ship_style": "STYLE",
        "assets": [
            {
                "id": "freighter",
                "category": "ship",
                "provider": "pixellab",
                "kind": "rotation",
                "size": 48,
                "directions": 8,
                "status": "draft",
                "prompt": "a freighter",
            }
        ],
    }


def test_rotation_dispatch_makes_8_raw_frames_and_one_strip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pixellab = _load("pixellab")
    generate = _load("generate")
    monkeypatch.setattr(generate, "_HERE", tmp_path)

    calls: list[tuple[str, str]] = []

    def fake_generate_object(prompt: str, out_path: Path, **kw: object) -> Path:
        calls.append(("object", out_path.name))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_sprite_png_bytes())
        return out_path

    def fake_rotate(
        image_path: Path, out_path: Path, *, to_direction: str, **kw: object
    ) -> Path:
        calls.append(("rotate", to_direction))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_sprite_png_bytes())
        return out_path

    monkeypatch.setattr(pixellab, "generate_object", fake_generate_object)
    monkeypatch.setattr(pixellab, "rotate", fake_rotate)

    manifest = _rotation_manifest()
    generate._generate_entry(manifest, manifest["assets"][0], force=False)

    raw_dir = tmp_path / "staging" / "raw" / "freighter"
    names = sorted(p.name for p in raw_dir.glob("*.png"))
    assert names == [
        "e.png",
        "n.png",
        "ne.png",
        "nw.png",
        "s.png",
        "se.png",
        "sw.png",
        "w.png",
    ]

    candidate = tmp_path / "staging" / "candidates" / "freighter.png"
    assert candidate.exists()
    strip = Image.open(candidate)
    assert strip.size == (48 * 8, 48)  # 8 frames wide
    # One base create and seven rotations.
    assert calls[0] == ("object", "e.png")
    assert sum(1 for c in calls if c[0] == "rotate") == 7


def test_rotation_dispatch_resumes_without_regenerating_existing_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pixellab = _load("pixellab")
    generate = _load("generate")
    monkeypatch.setattr(generate, "_HERE", tmp_path)

    # Pre-seed the east base and the NE frame with a marker sprite distinct
    # from what the fakes write, so a skip is byte-verifiable.
    raw_dir = tmp_path / "staging" / "raw" / "freighter"
    raw_dir.mkdir(parents=True)
    _marker = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    _marker.putpixel((8, 8), (250, 10, 10, 255))
    _buffer = io.BytesIO()
    _marker.save(_buffer, format="PNG")
    marker = _buffer.getvalue()
    (raw_dir / "e.png").write_bytes(marker)
    (raw_dir / "ne.png").write_bytes(marker)

    rotated: list[str] = []
    created: list[str] = []

    def fake_generate_object(prompt: str, out_path: Path, **kw: object) -> Path:
        created.append(out_path.name)
        out_path.write_bytes(_sprite_png_bytes())
        return out_path

    def fake_rotate(
        image_path: Path, out_path: Path, *, to_direction: str, **kw: object
    ) -> Path:
        rotated.append(to_direction)
        out_path.write_bytes(_sprite_png_bytes())
        return out_path

    monkeypatch.setattr(pixellab, "generate_object", fake_generate_object)
    monkeypatch.setattr(pixellab, "rotate", fake_rotate)

    manifest = _rotation_manifest()
    generate._generate_entry(manifest, manifest["assets"][0], force=False)

    # The base was not regenerated, NE was skipped, the other six rotated.
    assert created == []
    assert "ne" not in rotated
    assert sorted(rotated) == ["n", "nw", "s", "se", "sw", "w"]
    assert (raw_dir / "e.png").read_bytes() == marker
    assert (raw_dir / "ne.png").read_bytes() == marker


# --------------------------------------------------------------------------- #
# promote: into a temp asset root
# --------------------------------------------------------------------------- #


def test_promote_routes_ships_and_goods_to_correct_indexes(tmp_path: Path) -> None:
    promote = _load("promote")

    candidates = tmp_path / "candidates"
    candidates.mkdir()
    # A ship candidate is a horizontal strip; a good is a single square icon.
    module = _load("postprocess")
    strip = module.pack_strip([Image.new("RGBA", (48, 48), (0, 0, 0, 0))] * 8, 48)
    strip.save(candidates / "freighter.png")
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(candidates / "nova_fuel.png")

    manifest = {
        "assets": [
            {
                "id": "freighter",
                "category": "ship",
                "provider": "pixellab",
                "kind": "rotation",
                "size": 48,
                "directions": 8,
                "status": "approved",
                "prompt": "a freighter",
            },
            {
                "id": "nova_fuel",
                "category": "good",
                "provider": "pixellab",
                "kind": "object",
                "size": 32,
                "status": "approved",
                "prompt": "a glowing canister",
            },
            {
                "id": "biomass",
                "category": "good",
                "provider": "pixellab",
                "kind": "object",
                "size": 32,
                "status": "draft",  # not approved, so ignored
                "prompt": "green matter",
            },
        ]
    }
    asset_root = tmp_path / "assets"
    promote.promote(manifest, candidates, asset_root)

    goods_index = json.loads((asset_root / "goods" / "index.json").read_text())
    assert goods_index == {"ids": ["nova_fuel"]}
    assert (asset_root / "goods" / "nova_fuel.png").exists()

    ships_index = json.loads((asset_root / "ships" / "index.json").read_text())
    assert ships_index == {"ships": [{"id": "freighter", "frames": 8, "size": 48}]}
    assert (asset_root / "ships" / "freighter.png").exists()

    attribution = (asset_root / "ATTRIBUTION.md").read_text()
    assert "ship/freighter.png" in attribution
    assert "good/nova_fuel.png" in attribution
