"""Spiral-galaxy layout and the star-lane network ships travel along.

Planets sit on the arms of a spiral galaxy and are joined by star lanes.
Ships travel only along lanes, so the distance between two planets is the
length of the shortest lane route between them, as computed by
:class:`~spacesim2.core.navigation.Navigator`, not the straight-line distance.

Two structural guarantees matter to the rest of the simulation and are covered
by tests:

- Connected: every planet can reach every other planet.
- Planar: no two lanes cross. Lanes are a subset of the Delaunay
  triangulation of the planet positions, and Delaunay edges never cross.

:func:`generate_spiral_layout` builds the layout in three steps:

1. Scatter planets along ``arms`` logarithmic-spiral arms with a small core.
2. Triangulate them with a pure-Python Bowyer-Watson Delaunay.
3. Keep the minimum spanning tree for connectivity, plus the fraction
   ``lane_density`` of the remaining Delaunay edges that pass the Gabriel
   test, which selects short, local lanes.

:class:`StarLaneNetwork` is the runtime object the simulation holds: it maps
:class:`~spacesim2.core.planet.Planet` objects to their lane neighbours.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from spacesim2.core.planet import Planet

# Area per planet along the arms, sized from a 100x100 map holding 5 planets.
# Held constant as galaxies grow so nearest-neighbour lane lengths, and so
# fuel economics, stay the same at any scale.
AREA_PER_PLANET = (100.0 * 100.0) / 5
MIN_PLANET_DISTANCE = 10.0
DEFAULT_ARMS = 3
# Fraction of the non-tree Gabriel lanes kept. 1.0 keeps every local lane,
# for a mean degree of about 3.5; 0.0 leaves a bare spanning tree.
DEFAULT_LANE_DENSITY = 0.6
# Share of planets placed in the galactic core rather than on an arm.
CORE_FRACTION = 0.08
# Total angular sweep of each arm from its root to its tip, in radians.
ARM_SWEEP = 2.2 * math.pi

Point = Tuple[float, float]


@dataclass(frozen=True)
class GalaxyLayout:
    """Pure-data result of galaxy generation, before any Planet exists.

    ``positions[i]`` is planet ``i``'s map coordinate; all are non-negative
    and inside a ``width`` x ``height`` box. ``lanes`` holds ``(i, j)`` index
    pairs with ``i < j``. ``arm_of[i]`` is the arm each planet was seeded on,
    ``-1`` for core planets; it is informational, for rendering and analysis.
    """

    positions: Tuple[Point, ...]
    lanes: Tuple[Tuple[int, int], ...]
    arm_of: Tuple[int, ...]
    width: float
    height: float


@dataclass(frozen=True)
class StarLane:
    """One undirected lane between two planets."""

    a: "Planet"
    b: "Planet"
    length: float

    def other(self, planet: "Planet") -> "Planet":
        """The endpoint that is not ``planet``."""
        if planet is self.a:
            return self.b
        if planet is self.b:
            return self.a
        raise ValueError(f"{planet.name} is not an endpoint of this lane")


@dataclass
class StarLaneNetwork:
    """The undirected star-lane graph over a simulation's planets.

    Held by :class:`~spacesim2.core.simulation.Simulation`. Queried by the
    navigator for shortest routes, ships on departure, the live view for lane
    rendering, and the exporter. Lanes are added once at setup and never
    removed.
    """

    lanes: List[StarLane] = field(default_factory=list)
    _adjacency: Dict["Planet", List[StarLane]] = field(default_factory=dict)

    @classmethod
    def complete(cls, planets: Sequence["Planet"]) -> "StarLaneNetwork":
        """A lane between every pair of planets.

        Route distances then equal straight-line distances, which is what
        hand-built test worlds expect.
        """
        network = cls()
        for i, a in enumerate(planets):
            for b in planets[i + 1 :]:
                network.add_lane(a, b)
        return network

    def add_lane(self, a: "Planet", b: "Planet") -> StarLane:
        """Connect ``a`` and ``b``; the lane length is their Euclidean distance."""
        if a is b:
            raise ValueError(f"cannot add a lane from {a.name} to itself")
        if any(lane.other(a) is b for lane in self._adjacency.get(a, [])):
            raise ValueError(f"lane {a.name} <-> {b.name} already exists")
        lane = StarLane(a, b, math.hypot(b.x - a.x, b.y - a.y))
        self.lanes.append(lane)
        self._adjacency.setdefault(a, []).append(lane)
        self._adjacency.setdefault(b, []).append(lane)
        return lane

    def lanes_from(self, planet: "Planet") -> List[StarLane]:
        """Every lane touching ``planet``; empty if it has none."""
        return self._adjacency.get(planet, [])

    def neighbors(self, planet: "Planet") -> List["Planet"]:
        """Planets one lane away from ``planet``."""
        return [lane.other(planet) for lane in self.lanes_from(planet)]

    def has_lane(self, a: "Planet", b: "Planet") -> bool:
        return any(lane.other(a) is b for lane in self.lanes_from(a))

    def __len__(self) -> int:
        return len(self.lanes)


# ---------------------------------------------------------------------------
# Layout generation
# ---------------------------------------------------------------------------


def generate_spiral_layout(
    num_planets: int,
    arms: int = DEFAULT_ARMS,
    lane_density: float = DEFAULT_LANE_DENSITY,
    min_distance: float = MIN_PLANET_DISTANCE,
    rng: Optional[random.Random] = None,
) -> GalaxyLayout:
    """Lay ``num_planets`` planets on a spiral and join them with star lanes.

    Args:
        num_planets: Planets to place (>= 1).
        arms: Number of spiral arms (>= 1).
        lane_density: Fraction in [0, 1] of the optional local lanes to keep
            on top of the spanning tree. Higher means more redundancy and
            shorter routes.
        min_distance: Minimum pairwise separation between planets.
        rng: Random source; defaults to the module-level ``random``.

    Returns:
        A connected, planar :class:`GalaxyLayout`.

    Raises:
        ValueError: On invalid arguments, or if planets could not be separated
            by ``min_distance``, which needs a very large value for the
            galaxy radius.
    """
    if num_planets < 1:
        raise ValueError(f"num_planets must be >= 1, got {num_planets}")
    if arms < 1:
        raise ValueError(f"arms must be >= 1, got {arms}")
    if not 0.0 <= lane_density <= 1.0:
        raise ValueError(f"lane_density must be in [0, 1], got {lane_density}")
    source = rng if rng is not None else random.Random(random.random())

    points, arm_of = _spiral_positions(num_planets, arms, min_distance, source)
    triangulation = _delaunay_edges(points)
    lanes = _select_lanes(points, triangulation, lane_density, source)

    # Shift into the positive quadrant with a margin so every consumer can
    # treat coordinates as a plain width x height box.
    margin = min_distance
    min_x = min(p[0] for p in points) - margin
    min_y = min(p[1] for p in points) - margin
    shifted = tuple((x - min_x, y - min_y) for x, y in points)
    width = max(p[0] for p in shifted) + margin
    height = max(p[1] for p in shifted) + margin
    return GalaxyLayout(
        positions=shifted,
        lanes=tuple(sorted(lanes)),
        arm_of=tuple(arm_of),
        width=width,
        height=height,
    )


def _galaxy_radius(num_planets: int) -> float:
    """Outer radius that keeps along-arm planet density roughly constant.

    Arms fill only part of the disk, so the disk is sized for AREA_PER_PLANET
    with a modest inflation factor rather than the full disk area.
    """
    return max(50.0, 1.15 * math.sqrt(AREA_PER_PLANET * num_planets / math.pi))


def _spiral_positions(
    num_planets: int, arms: int, min_distance: float, rng: random.Random
) -> Tuple[List[Point], List[int]]:
    """Scatter planets along spiral arms plus a small core, enforcing separation.

    Each planet draws an arm and a parameter ``t`` in [0, 1] along it. The arm
    is a logarithmic spiral ``r = r0 * exp(k * theta)`` with Gaussian scatter
    across the arm that widens towards the rim. Candidates closer than
    ``min_distance`` to an accepted planet are redrawn; after a bounded number
    of failures the scatter widens so generation always terminates.
    """
    radius = _galaxy_radius(num_planets)
    core_radius = 0.18 * radius
    # Logarithmic spiral parameters: start the arms at the core edge and reach
    # the rim after ARM_SWEEP radians.
    r0 = core_radius
    growth = math.log(radius / r0) / ARM_SWEEP
    num_core = int(round(num_planets * CORE_FRACTION)) if num_planets >= 12 else 0

    points: List[Point] = []
    arm_of: List[int] = []
    scatter_boost = 1.0
    failures = 0
    while len(points) < num_planets:
        index = len(points)
        if index < num_core:
            # Core: uniform in a disk.
            angle = rng.uniform(0.0, 2.0 * math.pi)
            r = core_radius * math.sqrt(rng.random())
            candidate = (r * math.cos(angle), r * math.sin(angle))
            arm = -1
        else:
            arm = (index - num_core) % arms
            # Sample the radius uniformly: a log spiral's arc length grows
            # with r, so uniform-in-angle sampling would crowd the core.
            r = rng.uniform(r0, radius)
            theta = math.log(r / r0) / growth
            t = theta / ARM_SWEEP
            base_angle = theta + (2.0 * math.pi * arm) / arms
            # Gaussian scatter around the arm centreline, wider towards the
            # rim so the outer arms fray.
            width = (0.025 + 0.035 * t) * radius * scatter_boost
            angle = rng.uniform(0.0, 2.0 * math.pi)
            off = abs(rng.gauss(0.0, width))
            candidate = (
                r * math.cos(base_angle) + off * math.cos(angle),
                r * math.sin(base_angle) + off * math.sin(angle),
            )
        if all(
            math.hypot(candidate[0] - px, candidate[1] - py) >= min_distance
            for px, py in points
        ):
            points.append(candidate)
            arm_of.append(arm)
            failures = 0
            continue
        failures += 1
        if failures > 200:
            # Widen the arms so a crowded galaxy can still fit.
            scatter_boost *= 1.25
            failures = 0
            if scatter_boost > 64.0:
                raise ValueError(
                    f"could not place {num_planets} planets with "
                    f"min_distance={min_distance}; galaxy is too dense"
                )
    return points, arm_of


# --- Delaunay triangulation (Bowyer-Watson) ---------------------------------


def _circumcircle(a: Point, b: Point, c: Point) -> Tuple[float, float, float]:
    """Center (x, y) and squared radius of the circle through a, b, c."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        # Collinear triangle: treat as an infinite circle so the next
        # insertion always invalidates it.
        return (0.0, 0.0, float("inf"))
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return (ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2)


def _delaunay_edges(points: Sequence[Point]) -> List[Tuple[int, int]]:
    """Edges of the Delaunay triangulation of ``points``, as (i, j) with i < j.

    Bowyer-Watson: insert points one at a time into a super-triangle, remove
    every triangle whose circumcircle contains the new point, and
    re-triangulate the cavity. O(n^2) worst case, fine for the few hundred
    planets a galaxy holds. With fewer than three points the result is the
    complete graph.
    """
    n = len(points)
    if n < 3:
        return [(i, j) for i in range(n) for j in range(i + 1, n)]

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    mid_x = (max(xs) + min(xs)) / 2.0
    mid_y = (max(ys) + min(ys)) / 2.0
    big = 50.0 * span
    verts: List[Point] = list(points) + [
        (mid_x - big, mid_y - big),
        (mid_x + big, mid_y - big),
        (mid_x, mid_y + big),
    ]
    super_ids = (n, n + 1, n + 2)

    # Each triangle: (i, j, k, cx, cy, r2)
    triangles: List[Tuple[int, int, int, float, float, float]] = []
    cx, cy, r2 = _circumcircle(verts[n], verts[n + 1], verts[n + 2])
    triangles.append((n, n + 1, n + 2, cx, cy, r2))

    for p_index in range(n):
        px, py = verts[p_index]
        bad: List[Tuple[int, int, int, float, float, float]] = []
        keep: List[Tuple[int, int, int, float, float, float]] = []
        for tri in triangles:
            dx = px - tri[3]
            dy = py - tri[4]
            if dx * dx + dy * dy < tri[5]:
                bad.append(tri)
            else:
                keep.append(tri)
        # Boundary of the cavity: edges of bad triangles not shared by two
        # bad triangles.
        edge_count: Dict[Tuple[int, int], int] = {}
        for i, j, k, _, _, _ in bad:
            for u, v in ((i, j), (j, k), (k, i)):
                key = (u, v) if u < v else (v, u)
                edge_count[key] = edge_count.get(key, 0) + 1
        triangles = keep
        for (u, v), count in edge_count.items():
            if count == 1:
                cx, cy, r2 = _circumcircle(verts[u], verts[v], verts[p_index])
                triangles.append((u, v, p_index, cx, cy, r2))

    edges: set[Tuple[int, int]] = set()
    for i, j, k, _, _, _ in triangles:
        if i in super_ids or j in super_ids or k in super_ids:
            continue
        for u, v in ((i, j), (j, k), (k, i)):
            edges.add((u, v) if u < v else (v, u))
    # Every point must touch an edge. A point that appeared only in
    # super-triangle faces, which happens when all points are collinear, is
    # joined to its nearest neighbour so the caller's connectivity step can
    # succeed.
    touched = {u for u, _ in edges} | {v for _, v in edges}
    for i in range(n):
        if i not in touched:
            nearest = min(
                (j for j in range(n) if j != i),
                key=lambda j: _dist2(points[i], points[j]),
            )
            edges.add((i, nearest) if i < nearest else (nearest, i))
    return sorted(edges)


def _dist2(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


# --- Lane selection ---------------------------------------------------------


def _select_lanes(
    points: Sequence[Point],
    edges: Sequence[Tuple[int, int]],
    lane_density: float,
    rng: random.Random,
) -> List[Tuple[int, int]]:
    """Choose the lanes: the Euclidean MST plus a sample of Gabriel edges.

    Kruskal over the Delaunay edges gives the MST, which guarantees
    connectivity; the Euclidean MST is always a Delaunay subgraph. The
    Gabriel test, no other planet inside the circle whose diameter is the
    lane, drops long lanes that skim past intermediate planets and keeps the
    network local. ``lane_density`` then samples from those.
    """
    n = len(points)
    by_length = sorted(edges, key=lambda e: _dist2(points[e[0]], points[e[1]]))
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    chosen: List[Tuple[int, int]] = []
    optional: List[Tuple[int, int]] = []
    for u, v in by_length:
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
            chosen.append((u, v))
        else:
            optional.append((u, v))

    for u, v in optional:
        if not _is_gabriel(points, u, v):
            continue
        if rng.random() < lane_density:
            chosen.append((u, v))
    return chosen


def _is_gabriel(points: Sequence[Point], u: int, v: int) -> bool:
    """Whether no third point lies inside the circle with diameter ``uv``."""
    (ux, uy), (vx, vy) = points[u], points[v]
    mx, my = (ux + vx) / 2.0, (uy + vy) / 2.0
    r2 = _dist2(points[u], points[v]) / 4.0
    for i, (px, py) in enumerate(points):
        if i == u or i == v:
            continue
        if (px - mx) ** 2 + (py - my) ** 2 < r2:
            return False
    return True


# --- Validation helpers (used by tests and the setup path) -----------------


def is_connected(num_nodes: int, edges: Iterable[Tuple[int, int]]) -> bool:
    """Whether the undirected graph on ``num_nodes`` nodes is connected."""
    if num_nodes <= 1:
        return True
    adjacency: Dict[int, List[int]] = {i: [] for i in range(num_nodes)}
    for u, v in edges:
        adjacency[u].append(v)
        adjacency[v].append(u)
    seen = {0}
    stack = [0]
    while stack:
        node = stack.pop()
        for nxt in adjacency[node]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return len(seen) == num_nodes


def segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Whether open segments ab and cd properly intersect.

    Shared endpoints do not count as a crossing.
    """

    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def crossing_lane_pairs(
    points: Sequence[Point], lanes: Sequence[Tuple[int, int]]
) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Every pair of lanes that cross; empty for a planar layout."""
    crossings = []
    for i, (a, b) in enumerate(lanes):
        for c, d in lanes[i + 1 :]:
            if len({a, b, c, d}) < 4:
                continue  # share an endpoint
            if segments_cross(points[a], points[b], points[c], points[d]):
                crossings.append(((a, b), (c, d)))
    return crossings
