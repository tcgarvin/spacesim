# Live Galaxy View

`uv run spacesim2 ui` opens the live galaxy view. It replaced the static
3-pane inspector; see `docs/decision-log.md` (2026-07-11).

## Directory map

```
spacesim2/ui/live/          shipped renderer
  app.py                    LiveGalaxyApp: window, main loop, event routing
  worker.py                 SimulationWorker: runs turns on its own thread, publishes TurnFrames
  frame.py                  TurnFrame: the immutable per-turn snapshot the renderer reads
  director.py               real-time → turn requests + per-frame ship interpolation between frames
  camera.py                 galaxy box (Simulation.galaxy_size) → screen; smooth zoom/pan
  view_model.py             snapshot builders over Simulation (worker thread only; no core changes)
  history.py                per-turn price/volume/wellbeing series for the charts (lock-guarded)
  assets.py                 sprite/font loading; procedural-placeholder fallback
  procgen/                  noise.py, nebula.py (backdrop + starfield), placeholders.py
  entities/                 planet_view.py, ship_view.py, lane_view.py
  scenes/                   galaxy_scene.py
  widgets/                  hud.py, chart.py, charts_panel.py, info_panel.py
  assets/                   committed PNGs: planets/ ships/ goods/ fonts/ + ATTRIBUTION.md

tools/assetgen/             offline AI asset pipeline (dev-only, never runs at runtime)
  manifest.yaml             one entry per asset: id, category, provider, prompt, seed, status
  pixellab.py, nanobanana.py  provider clients (PIXELLAB_API_KEY / GEMINI_API_KEY)
  generate.py               manifest → provider → staging/raw/
  postprocess.py            downscale, quantize, alpha-trim, pack sheets
  promote.py                approved staging → ui/live/assets/ + ATTRIBUTION.md
  staging/                  gitignored raw candidates
```

## Threading and frame protocol

A 100-planet turn takes about 1.3 s, so turns run off the render thread. The
UI and the simulation share one thin contract (decision log, 2026-09-02):

- **The worker owns the sim.** `SimulationWorker` runs `run_turn` on a daemon
  thread under `_sim_lock`, builds one immutable `TurnFrame`, and publishes
  it by reference swap (`latest_frame`). Readers hold a frozen object and
  need no lock.
- **A frame is sized to the screen, not the sim.** Per planet: position,
  wellbeing, population. Per ship: route waypoints and progress. Plus the
  HUD vitals. It never copies actor inventories or order books. The one
  expensive input, the per-planet wellbeing sweep
  (`planet_wellbeing_by_name`), runs once per turn and is shared with
  `HistoryRecorder.record`.
- **Detail is a subscription.** Setting `GalaxyScene.selection` subscribes
  the worker to that `(kind, name)`; later frames carry its
  `PlanetDetail` or `ShipDetail`. A subscription made while the worker is
  idle is serviced at once through a non-blocking `_sim_lock` try-acquire,
  so the panel opens immediately. One made mid-turn waits for the turn
  boundary and the panel does not draw until then. Hover never subscribes.
- **Pacing without debt.** The director converts wall time into
  `request_turn()` calls. The worker keeps at most one pending request, so a
  sim slower than the requested rate runs flat out instead of bursting
  several turns in one frame. Ships lerp between the previous and latest
  frame positions.
- **The render thread never touches core objects.** `GalaxyScene`, `hud`,
  `info_panel` and `Director` read only `TurnFrame`. `view_model.py`
  builders run only on the worker; lanes are the one static exception,
  cached once. `tests/test_live_worker.py` pins this by stubbing the sim out
  after a frame is built and rendering anyway.
- **Headless mode.** Until `start()` is called the worker is synchronous and
  `request_turn()` runs the turn on the caller. `LiveGalaxyApp.initialize`
  leaves it that way; `run()` starts the thread and `stop()` joins it on
  quit. Tests drive turns with `run_one_turn_now()`.

The GIL still applies. A pure-Python turn steals render slices, so the map is
choppier while a turn runs, but input, panning and the panel stay live.

## Star lanes and the spiral galaxy

Planets sit on a spiral (`core/galaxy.py`) inside a `width x height` box that
grows with planet count, and ships fly only along star lanes.

- **Camera** fits the whole `galaxy_size` box with a margin, not a fixed
  0..100 square. It reserves the charts strip along the bottom so the fitted
  galaxy is never hidden behind it. Min and max zoom derive from that fit.
- **Lanes** (`view_model.lanes()`, cached) are drawn first as thin dim
  lines. `ship_view` draws no origin-to-destination chord.
- **Ship position** is interpolated along the ship's lane route
  (`ShipSnapshot.waypoints`, from `ship.route`) by arc-length fraction
  (`director.polyline_point`). Heading follows the current segment, so ships
  turn at waypoints. Forced travel states with no core route fall back to
  the `(origin, dest)` chord.
- **Highlights**: selecting a ship draws its full route in gold, hovering in
  grey; selecting a planet draws its incident lanes in blue. The ship
  panel's route line lists every hop (`Vesper -> Kael -> Orin (42%)`).
- **Declutter**: planet labels render only once worlds are at least
  `LABEL_MIN_RADIUS_PX` on screen. Hovered and selected worlds are always
  labelled. Worlds never shrink below `MIN_PLANET_PX`, so they stay
  clickable at fit zoom on a 100-planet galaxy.

## Art cohesion rule

Two mechanisms keep output from different generators looking like one game:
a shared house-style reference image and fixed palette passed to both
generators (`style_ref` to PixelLab, reference image to Nano Banana/Gemini),
and a palette snap in `postprocess.py` that quantizes every non-pixel-native
output to that palette. The renderer falls back to procedural placeholders
for any asset not yet promoted, so code and art never block each other.
