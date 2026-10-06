"""Pure CPU-idle window logic for post-test stall detection.

No clock, no I/O: the guard samples group CPU times and feeds them here.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Mapping


def idle_threshold_s(window_s: float) -> float:
    """CPU progress below which a window of ``window_s`` counts as idle."""
    return max(0.5, 0.02 * window_s)


class IdleWindow:
    """Sliding idle detector over cumulative per-member CPU samples.

    A sample maps a member key ``(pid, create_time)`` to cumulative CPU
    seconds. Progress accumulates per-key positive deltas; a new key adds
    its full value and a disappearing key adds nothing. A ``None`` sample
    (an unreadable member) counts as busy and restarts the window.
    """

    def __init__(self, window_s: float) -> None:
        self._window_s = window_s
        self._baseline: dict[Hashable, float] = {}
        self._armed = False
        self._idle = False
        self._total = 0.0
        self._history: deque[tuple[float, float]] = deque()

    def observe(
        self,
        now: float,
        sample: Mapping[Hashable, float] | None,
    ) -> bool:
        """Feed one sample taken at ``now``; True means the group is idle."""
        if sample is None:
            self._baseline = {}
            self._armed = False
            self._idle = False
            self._total = 0.0
            self._history.clear()
            return False
        threshold = idle_threshold_s(self._window_s)
        if not self._armed:
            self._armed = True
            self._baseline = dict(sample)
            self._total = 0.0
            self._history = deque([(now, 0.0)])
            return False
        gained = 0.0
        for key, value in sample.items():
            previous = self._baseline.get(key)
            if previous is None:
                gained += value
            elif value > previous:
                gained += value - previous
        self._baseline = dict(sample)
        self._total += gained
        self._history.append((now, self._total))
        cutoff = now - self._window_s
        # Keep the newest pre-cutoff sample as the window's baseline.
        # Dropping every older point would shrink the retained span below
        # the window on every pass, so an idle group could never be
        # recognised. Measuring from the retained baseline can only add
        # pre-window progress, delaying the verdict by at most one sample:
        # the safe direction for a kill decision.
        while len(self._history) > 1 and self._history[1][0] < cutoff:
            self._history.popleft()
        if gained >= threshold:
            # Real progress: busy now, whatever the window says.
            self._idle = False
            return False
        if self._idle:
            # Still silent since the idle verdict: stay idle without
            # re-measuring, so repeated polls cannot flap the verdict.
            return True
        oldest_at, oldest_total = self._history[0]
        self._idle = (now - oldest_at >= self._window_s
                      and self._total - oldest_total < threshold)
        return self._idle
