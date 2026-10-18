"""Nano Banana (Gemini image) provider for the offline asset pipeline.

Talks to the Vertex AI *express-mode* REST endpoint with a plain API key (the
``GOOGLE_CLOUD_API_KEY`` express keys use), so the tool needs no google-genai
dependency and no OAuth. Dev-only; the shipped runtime never imports this.

Env (loaded from ``tools/assetgen/.env`` or the repo-root ``.env``):
    GOOGLE_CLOUD_API_KEY   Vertex express API key.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

# Nano Banana 2.
DEFAULT_MODEL = "gemini-3-pro-image-preview"
_ENDPOINT = (
    "https://aiplatform.googleapis.com/v1/publishers/google/models/"
    "{model}:generateContent?key={key}"
)


def _load_env() -> None:
    """Populate os.environ from a .env file if the key isn't already set."""
    if os.environ.get("GOOGLE_CLOUD_API_KEY"):
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
    key = os.environ.get("GOOGLE_CLOUD_API_KEY")
    if not key:
        raise RuntimeError(
            "GOOGLE_CLOUD_API_KEY not set (put it in tools/assetgen/.env)"
        )
    return key


def generate_image(
    prompt: str,
    out_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "1:1",
    timeout: float = 120.0,
) -> Path:
    """Generate one image from ``prompt`` and write the raw PNG to ``out_path``."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }
    url = _ENDPOINT.format(model=model, key=_api_key())
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    candidates = payload.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"no candidates returned: {json.dumps(payload)[:400]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData")
        if inline:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(inline["data"]))
            return out_path
    raise RuntimeError(
        f"response had no image part (finish={candidates[0].get('finishReason')})"
    )
