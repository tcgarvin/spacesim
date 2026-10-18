"""PixelLab (pixel-art) provider for the offline asset pipeline.

Talks to the PixelLab v2 REST API with a bearer token, so the tool needs no
pixellab SDK dependency. The pipeline uses two operations: create one
transparent-background image (goods icons and the east-facing ship base image)
and rotate an existing image to a new facing (the other seven ship frames).
Dev-only; the shipped runtime never imports this.

Env (loaded from ``tools/assetgen/.env`` or the repo-root ``.env``):
    PIXELLAB_API_KEY   PixelLab API token (Bearer auth).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_BASE_URL = "https://api.pixellab.ai/v2"

# Pipeline facing labels -> PixelLab's ``Direction`` vocabulary (hyphenated).
_DIRECTIONS: dict[str, str] = {
    "e": "east",
    "ne": "north-east",
    "n": "north",
    "nw": "north-west",
    "w": "west",
    "sw": "south-west",
    "s": "south",
    "se": "south-east",
}


def _load_env() -> None:
    """Populate os.environ from a .env file if the key isn't already set."""
    if os.environ.get("PIXELLAB_API_KEY"):
        return
    here = Path(__file__).resolve()
    for candidate in (here.parent / ".env", here.parents[2] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip())


def _api_key() -> str:
    _load_env()
    key = os.environ.get("PIXELLAB_API_KEY")
    if not key:
        raise RuntimeError("PIXELLAB_API_KEY not set (put it in tools/assetgen/.env)")
    return key


def _direction(label: str) -> str:
    """Map a pipeline facing label (e, ne, ...) to a PixelLab Direction name."""
    name = _DIRECTIONS.get(label)
    if name is None:
        raise ValueError(
            f"unknown direction label {label!r}; expected one of {sorted(_DIRECTIONS)}"
        )
    return name


def _post(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST a JSON body to a PixelLab endpoint and return the decoded response.

    Raises ``RuntimeError`` with a response-body snippet on any non-200 status so
    failures surface loudly rather than writing a corrupt sprite.
    """
    request = urllib.request.Request(
        f"{_BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"PixelLab {path} failed ({exc.code}): {detail}") from exc
    return payload


def _write_image(payload: dict[str, Any], out_path: Path) -> Path:
    """Decode the base64 image from a PixelLab response and write it to disk."""
    image = payload.get("image")
    if not isinstance(image, dict) or "base64" not in image:
        raise RuntimeError(f"response had no image field: {json.dumps(payload)[:400]}")
    data = str(image["base64"])
    if data.startswith("data:"):
        # Strip a "data:image/png;base64," data-URI prefix if present.
        data = data.partition(",")[2]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data))
    return out_path


def generate_object(
    prompt: str,
    out_path: Path,
    *,
    size: int,
    no_background: bool = True,
    view: str = "",
    direction: str = "",
    text_guidance_scale: float = 8.0,
    timeout: float = 120.0,
) -> Path:
    """Generate one transparent pixel-art image and write the PNG to ``out_path``.

    Uses the Pixflux create-image endpoint, which supports native transparent
    backgrounds (``no_background``) at the small sizes this pipeline targets.
    ``view`` and ``direction`` are optional weak guidance (used for the
    east-facing ship base; left empty for goods icons).
    """
    body: dict[str, Any] = {
        "description": prompt,
        "image_size": {"width": size, "height": size},
        "no_background": no_background,
        "text_guidance_scale": text_guidance_scale,
    }
    if view:
        body["view"] = view
    if direction:
        body["direction"] = _direction(direction)
    return _write_image(_post("/create-image-pixflux", body, timeout), out_path)


def rotate(
    image_path: Path,
    out_path: Path,
    *,
    from_direction: str,
    to_direction: str,
    size: int,
    view: str = "high top-down",
    timeout: float = 120.0,
) -> Path:
    """Rotate ``image_path`` to a new facing and write the PNG to ``out_path``.

    ``from_direction``/``to_direction`` use the pipeline's short facing labels
    (e, ne, n, nw, w, sw, s, se); they are mapped to PixelLab Direction names.
    """
    source = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {
        "image_size": {"width": size, "height": size},
        "from_image": {"type": "base64", "base64": source},
        "from_view": view,
        "to_view": view,
        "from_direction": _direction(from_direction),
        "to_direction": _direction(to_direction),
    }
    return _write_image(_post("/rotate", body, timeout), out_path)
