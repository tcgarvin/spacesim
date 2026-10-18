"""Post-processing for raw provider output -> committed game sprites.

Nano Banana returns a painterly planet rendered on a (roughly) black void. The
runtime wants a transparent, square, centered planet disc it can blit and tint.
This module finds the lit disc, builds a feathered circular alpha mask, recenters
it on a square canvas, and resizes to the target sprite size.

Pure Pillow + numpy; no network, no provider knowledge. Safe to unit-test on a
fixture PNG.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Pixels dimmer than this (0-255 luminance) are treated as background void.
_DISC_LUMA_THRESHOLD = 28
# Alpha ramps from 1 -> 0 between these fractions of the detected disc radius.
_ALPHA_INNER = 0.99
_ALPHA_OUTER = 1.06


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def disc_alpha_crop(img: Image.Image, size: int = 256) -> Image.Image:
    """Detect the planet disc on a dark background and return an RGBA sprite.

    The disc is located by thresholding luminance; its centroid and radius set a
    feathered circular alpha mask. The result is recentered on a transparent
    square canvas and resized to ``size`` x ``size``.
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.float64)
    h, w = rgb.shape[:2]
    lit = _luma(rgb) > _DISC_LUMA_THRESHOLD
    if not lit.any():
        raise ValueError("no lit disc found; image looks fully dark")

    ys, xs = np.nonzero(lit)
    cy = (ys.min() + ys.max()) / 2.0
    cx = (xs.min() + xs.max()) / 2.0
    # Radius from the tighter of the two extents so a non-square frame doesn't
    # stretch the disc; the lit region of a planet is near-circular anyway.
    radius = min(ys.max() - ys.min(), xs.max() - xs.min()) / 2.0
    radius = max(radius, 1.0)

    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / radius
    alpha = np.clip((_ALPHA_OUTER - dist) / (_ALPHA_OUTER - _ALPHA_INNER), 0.0, 1.0)

    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = rgb.astype(np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    out = Image.fromarray(rgba, mode="RGBA")

    # Crop to a square around the disc, with a little margin for the feather.
    side = int(radius * 2 * _ALPHA_OUTER) + 2
    left = int(round(cx - side / 2))
    top = int(round(cy - side / 2))
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(out.crop((left, top, left + side, top + side)), (0, 0))
    return square.resize((size, size), Image.Resampling.LANCZOS)


def _alpha_min(img: Image.Image) -> int:
    return int(np.asarray(img.convert("RGBA"))[..., 3].min())


def key_background(img: Image.Image, tolerance: int = 16) -> Image.Image:
    """Make a uniform background transparent by keying the top-left corner colour.

    Fallback for providers that return art on a solid fill instead of true
    transparency. PixelLab's ``no_background`` yields real alpha, so the object
    path only invokes this when an image arrives fully opaque.
    """
    arr = np.asarray(img.convert("RGBA"), dtype=np.int16)
    key = arr[0, 0, :3]
    distance = np.abs(arr[..., :3] - key).sum(axis=-1)
    keyed = arr.copy()
    keyed[distance <= tolerance, 3] = 0
    return Image.fromarray(keyed.astype(np.uint8), mode="RGBA")


def trim_pad_square(
    img: Image.Image, size: int, *, margin: float = 0.08
) -> Image.Image:
    """Trim to the alpha bounding box, centre on a square, resize to ``size``.

    Existing transparency is preserved; a fully-opaque image (a provider that
    ignored the transparent-background request) is keyed first. Resampling uses
    NEAREST so pixel-art edges stay crisp, and only runs when the padded square
    differs from ``size``.
    """
    rgba = img.convert("RGBA")
    if _alpha_min(rgba) == 255:
        rgba = key_background(rgba)

    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("image is fully transparent; nothing to trim")
    cropped = rgba.crop(bbox)

    w, h = cropped.size
    side = max(int(round(max(w, h) * (1.0 + 2.0 * margin))), w, h, 1)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - w) // 2, (side - h) // 2))
    if square.size != (size, size):
        square = square.resize((size, size), Image.Resampling.NEAREST)
    return square


def pack_strip(frames: list[Image.Image], size: int) -> Image.Image:
    """Compose equal square frames left-to-right into one RGBA strip.

    The strip is ``len(frames) * size`` wide and ``size`` tall; frame order is
    preserved (the caller supplies facings already ordered E, NE, N, ...).
    """
    if not frames:
        raise ValueError("pack_strip needs at least one frame")
    strip = Image.new("RGBA", (size * len(frames), size), (0, 0, 0, 0))
    for i, frame in enumerate(frames):
        cell = frame.convert("RGBA")
        if cell.size != (size, size):
            cell = cell.resize((size, size), Image.Resampling.NEAREST)
        strip.paste(cell, (i * size, 0))
    return strip


def process_file(
    src: Path, dst: Path, size: int = 256, *, kind: str = "painterly"
) -> Path:
    """Post-process one raw image into a committed sprite.

    ``kind`` routes the transform: ``painterly`` keeps the planet disc-crop path
    unchanged; ``object`` applies the pixel-art trim/pad/square path.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    if kind == "painterly":
        sprite = disc_alpha_crop(img, size=size)
    elif kind == "object":
        sprite = trim_pad_square(img, size)
    else:
        raise ValueError(f"unknown postprocess kind {kind!r}")
    sprite.save(dst)
    return dst


def process_strip(frame_paths: list[Path], dst: Path, size: int) -> Path:
    """Trim/pad each rotation frame and pack them into one horizontal strip."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    frames = [trim_pad_square(Image.open(p), size) for p in frame_paths]
    pack_strip(frames, size).save(dst)
    return dst


if __name__ == "__main__":
    import sys

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 256
    print("wrote", process_file(src, dst, size))
