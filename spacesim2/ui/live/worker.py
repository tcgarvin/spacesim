"""Runs simulation turns off the render thread and publishes frames.

The contract between the UI and the simulation:

* The worker owns the mutable simulation. ``run_turn`` and every snapshot
  builder run under ``_sim_lock`` on the worker thread. The render thread reads
  only :attr:`SimulationWorker.latest_frame`, an immutable
  :class:`~spacesim2.ui.live.frame.TurnFrame` swapped in by reference.
* The director paces turns through :meth:`request_turn`. The worker runs at
  most one turn at a time and remembers at most one pending request, so a
  simulation slower than the requested rate runs flat out instead of queuing
  catch-up turns that would freeze the window.
* Drill-down detail is a subscription. The scene subscribes to its selection
  and every published frame carries details for subscribed entities only. A
  subscription made while the worker is idle is serviced at once under the
  lock so the panel opens without waiting for the next turn. One made mid-turn
  is picked up at the turn boundary.

Without :meth:`start` the worker is synchronous: :meth:`request_turn` runs the
turn on the calling thread. This is the headless and test mode; it keeps tests
deterministic without sleeping on a thread.
"""

from __future__ import annotations

import threading
from typing import FrozenSet, Set

from spacesim2.core.simulation import Simulation
from spacesim2.ui.live.frame import Subscription, TurnFrame, build_frame, with_details
from spacesim2.ui.live.history import HistoryRecorder
from spacesim2.ui.live.view_model import GalaxyViewModel, planet_wellbeing_by_name

# Seconds stop() waits for an in-flight turn before giving up on the join.
STOP_JOIN_TIMEOUT_S = 30.0


class SimulationWorker:
    def __init__(
        self,
        simulation: Simulation,
        view_model: GalaxyViewModel,
        history: HistoryRecorder,
    ) -> None:
        self._sim = simulation
        self._vm = view_model
        self.history = history
        # Held for the whole of run_turn plus frame build. subscribe() takes
        # it non-blockingly to service a selection while the sim is idle.
        self._sim_lock = threading.Lock()
        self._subscriptions: Set[Subscription] = set()
        self._subscriptions_lock = threading.Lock()
        self._wake = threading.Condition()
        self._turn_requested = False
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="spacesim2-simulation", daemon=True
        )
        # The recorder already holds its turn-0 baseline, so the startup frame
        # runs its own sweep without recording a second point.
        with self._sim_lock:
            self._latest = build_frame(
                self._sim, self._vm, frozenset(), planet_wellbeing_by_name(self._sim)
            )

    # -- Frames ---------------------------------------------------------

    @property
    def latest_frame(self) -> TurnFrame:
        """The most recently published frame. Safe to read from any thread."""
        return self._latest

    @property
    def busy(self) -> bool:
        """True while a turn is running on the worker thread."""
        return self._sim_lock.locked()

    def _snapshot_subscriptions(self) -> FrozenSet[Subscription]:
        with self._subscriptions_lock:
            return frozenset(self._subscriptions)

    def _step(self) -> None:
        """Run one turn and publish its frame. Runs on whichever thread calls it."""
        with self._sim_lock:
            self._sim.run_turn()
            # One actor sweep feeds both the history point and the frame.
            wellbeing = planet_wellbeing_by_name(self._sim)
            self.history.record(wellbeing)
            self._latest = build_frame(
                self._sim, self._vm, self._snapshot_subscriptions(), wellbeing
            )

    # -- Turn pacing ----------------------------------------------------

    def request_turn(self) -> None:
        """Ask for one more turn. Idempotent while a request is pending."""
        if not self._thread.is_alive():
            self._step()
            return
        with self._wake:
            self._turn_requested = True
            self._wake.notify()

    def run_one_turn_now(self) -> None:
        """Run a turn synchronously on the caller (tests, headless drivers)."""
        self._step()

    # -- Subscriptions --------------------------------------------------

    def subscribe(self, target: Subscription) -> None:
        with self._subscriptions_lock:
            if target in self._subscriptions:
                return
            self._subscriptions.add(target)
        self._refresh_details_if_idle()

    def unsubscribe(self, target: Subscription) -> None:
        with self._subscriptions_lock:
            self._subscriptions.discard(target)
        self._refresh_details_if_idle()

    def _refresh_details_if_idle(self) -> None:
        """Republish the current turn's frame with the new detail set.

        Only when the sim is idle. If a turn is in flight the boundary builds
        the details anyway, and blocking the render thread on the lock would
        be the stall this worker exists to prevent.
        """
        if not self._sim_lock.acquire(blocking=False):
            return
        try:
            self._latest = with_details(
                self._latest, self._vm, self._snapshot_subscriptions()
            )
        finally:
            self._sim_lock.release()

    # -- Thread lifecycle -----------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop after any in-flight turn and join the thread."""
        with self._wake:
            self._stopping = True
            self._wake.notify()
        if self._thread.is_alive():
            self._thread.join(STOP_JOIN_TIMEOUT_S)

    def _run(self) -> None:
        while True:
            with self._wake:
                while not (self._turn_requested or self._stopping):
                    self._wake.wait()
                if self._stopping:
                    return
                self._turn_requested = False
            self._step()
