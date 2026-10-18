"""House palette, wellbeing colour ramp, and font loading for the live view.

Fonts are the bundled Space Grotesk OFL TTFs in ``assets/fonts/``, loaded by
path like every other committed asset here. Everything else is procedural
except the committed sprites promoted from the offline asset pipeline.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pygame

Color = Tuple[int, int, int]

_ASSET_ROOT = Path(__file__).resolve().parent / "assets"

# Dark void, cool chrome.
BACKGROUND: Color = (6, 7, 16)
STARFIELD_TINTS: Tuple[Color, ...] = (
    (180, 190, 220),  # cool white
    (140, 170, 230),  # blue
    (230, 210, 180),  # warm
)
NEBULA_TINTS: Tuple[Color, ...] = (
    (40, 30, 80),  # violet
    (20, 50, 90),  # teal-blue
    (70, 25, 55),  # magenta
)
TRADE_LANE: Color = (58, 70, 100)  # resting star lane, drawn under everything
LANE_HIGHLIGHT: Color = (120, 170, 235)  # selected planet's lanes
ROUTE_HIGHLIGHT: Color = (235, 205, 120)  # selected ship's flight path
ROUTE_HOVER: Color = (150, 165, 200)  # hovered ship's flight path
SHIP_BODY: Color = (200, 210, 235)
SHIP_ENGINE: Color = (120, 180, 255)
HUD_TEXT: Color = (190, 200, 225)

# Wellbeing ramp endpoints: famine red -> neutral amber -> thriving green.
_RAMP_LOW: Color = (210, 60, 50)
_RAMP_MID: Color = (220, 190, 90)
_RAMP_HIGH: Color = (90, 210, 120)


def lerp_color(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def wellbeing_color(wellbeing: float) -> Color:
    """Map a wellbeing score in [0, 1] to a famine->thriving colour."""
    w = max(0.0, min(1.0, wellbeing))
    if w < 0.5:
        return lerp_color(_RAMP_LOW, _RAMP_MID, w / 0.5)
    return lerp_color(_RAMP_MID, _RAMP_HIGH, (w - 0.5) / 0.5)


class PlanetSprites:
    """Baked planet sprites promoted from the offline asset pipeline.

    Loads every PNG listed in ``assets/planets/index.json`` once and hands one
    out per planet by a stable hash, so a world keeps the same look. Create
    after the display is initialised so ``convert_alpha`` works. When no assets
    are promoted the set is empty and callers fall back to the procedural
    placeholder sphere.
    """

    def __init__(self) -> None:
        self._sprites: List[pygame.Surface] = []
        index_path = _ASSET_ROOT / "planets" / "index.json"
        if not index_path.exists():
            return
        ids = json.loads(index_path.read_text()).get("ids", [])
        for asset_id in ids:
            png = _ASSET_ROOT / "planets" / f"{asset_id}.png"
            if png.exists():
                self._sprites.append(pygame.image.load(str(png)).convert_alpha())

    def __bool__(self) -> bool:
        return bool(self._sprites)

    def for_name(self, name: str) -> pygame.Surface:
        """Return the stable sprite for ``name``. Caller must check truthiness."""
        return self._sprites[abs(hash(name)) % len(self._sprites)]


def _slice_strip(strip: pygame.Surface, frames: int, size: int) -> List[pygame.Surface]:
    """Cut a horizontal sprite strip into ``frames`` square ``size``px surfaces.

    ``.copy()`` detaches each frame from the parent surface so later scaling
    never reaches back into the strip's pixel buffer.
    """
    out: List[pygame.Surface] = []
    for i in range(frames):
        frame = strip.subsurface(pygame.Rect(i * size, 0, size, size)).copy()
        out.append(frame)
    return out


def heading_to_frame(heading: float, frame_count: int) -> int:
    """Nearest baked facing for a screen-space ``heading`` (radians).

    The renderer's heading is ``atan2(dest_y - origin_y, dest_x - origin_x)``
    over map coordinates, and the camera maps larger map-y to larger screen-y,
    so heading is screen-space, y-down: 0 = east, +pi/2 = south, -pi/2 = north.

    The strip is ordered E, NE, N, NW, W, SW, S, SE: frame ``k`` faces the
    math-convention angle ``k * (2pi / frame_count)``, y-up, counterclockwise.
    Flipping y between the conventions negates the angle, so the nearest frame
    is ``round(-heading / step) mod frame_count``.
    """
    step = 2.0 * math.pi / frame_count
    return round(-heading / step) % frame_count


class ShipSprites:
    """Baked directional ship sprites promoted from the offline asset pipeline.

    ``assets/ships/index.json`` lists ship types, each a horizontal strip of
    evenly spaced facings. There is one type, the freighter, and every ship
    renders with it, so only the first entry's strip is loaded. When no assets
    are promoted the set is empty and ``ship_view`` falls back to the
    procedural arrow glyph.
    """

    def __init__(self) -> None:
        self._frames: List[pygame.Surface] = []
        index_path = _ASSET_ROOT / "ships" / "index.json"
        if not index_path.exists():
            return
        ships = json.loads(index_path.read_text()).get("ships", [])
        if not ships:
            return
        spec = ships[0]
        png = _ASSET_ROOT / "ships" / f"{spec['id']}.png"
        if not png.exists():
            return
        strip = pygame.image.load(str(png)).convert_alpha()
        self._frames = _slice_strip(strip, int(spec["frames"]), int(spec["size"]))

    def __bool__(self) -> bool:
        return bool(self._frames)

    def frame_for_heading(self, heading: float) -> pygame.Surface:
        """Nearest baked facing for ``heading``. Caller must check truthiness."""
        return self._frames[heading_to_frame(heading, len(self._frames))]


class GoodIcons:
    """Baked commodity icons promoted from the offline asset pipeline.

    ``assets/goods/index.json`` lists commodity ids, one 32x32 icon each, so
    new ids need no code changes. Icons are optional decoration: a lookup for a
    missing id returns ``None`` and callers render the text-only row.
    """

    def __init__(self) -> None:
        self._icons: Dict[str, pygame.Surface] = {}
        index_path = _ASSET_ROOT / "goods" / "index.json"
        if not index_path.exists():
            return
        ids = json.loads(index_path.read_text()).get("ids", [])
        for good_id in ids:
            png = _ASSET_ROOT / "goods" / f"{good_id}.png"
            if png.exists():
                self._icons[good_id] = pygame.image.load(str(png)).convert_alpha()

    def __bool__(self) -> bool:
        return bool(self._icons)

    def get(self, commodity_id: str) -> Optional[pygame.Surface]:
        """Icon for ``commodity_id``, or ``None`` if none was promoted."""
        return self._icons.get(commodity_id)


_FONT_DIR = _ASSET_ROOT / "fonts"
_REGULAR = _FONT_DIR / "SpaceGrotesk-Regular.ttf"
_MEDIUM = _FONT_DIR / "SpaceGrotesk-Medium.ttf"


class Fonts:
    """Font set. Must be created after ``pygame.font.init()``.

    Point sizes are chosen against Space Grotesk's metrics so small, normal,
    and large rasterise at about 13, 17, and 22 px tall. Headers use the Medium
    weight so section titles stand out from body text.
    """

    def __init__(self) -> None:
        self._fonts: Dict[str, pygame.font.Font] = {
            "small": pygame.font.Font(str(_REGULAR), 10),
            "normal": pygame.font.Font(str(_REGULAR), 13),
            "large": pygame.font.Font(str(_MEDIUM), 17),
        }

    def font(self, size: str = "normal") -> pygame.font.Font:
        return self._fonts.get(size, self._fonts["normal"])

    def render(
        self, text: str, size: str = "normal", color: Color = HUD_TEXT
    ) -> pygame.Surface:
        return self.font(size).render(text, True, color)
