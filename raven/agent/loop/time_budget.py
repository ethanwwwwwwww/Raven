"""Wall-clock budget awareness for the agent loop.

An agent running under an external deadline (benchmark harness, cron slot) is
otherwise blind to it: it cannot decide to start a long build now instead of
deliberating, because nothing tells it time is passing. When a deadline is
provided, this emits a short reminder line the first time consumption crosses
each threshold; the loop appends it to the latest tool result.

Pure decision logic, no I/O, mirroring recovery.py.
"""

from __future__ import annotations

import os

DEADLINE_ENV = "RAVEN_TASK_DEADLINE_EPOCH"
# Opt-in: also ask for a durable checkpoint once the budget is mostly spent.
# Off by default because it costs a turn, and the turn is only worth spending
# where unpersisted work is actually lost -- a harness that grades the live
# environment already sees the work, one that grades a committed diff does not.
CHECKPOINT_ENV = "RAVEN_DEADLINE_CHECKPOINT"

_DEFAULT_THRESHOLDS = (0.5, 0.75, 0.9)
# From this fraction of the budget on, the reminder also asks for a durable
# checkpoint of the work so far.
_CHECKPOINT_THRESHOLD = 0.75


class TimeBudgetReminder:
    """Emits one reminder per crossed budget threshold."""

    def __init__(self, deadline_epoch: float, start_epoch: float, checkpoint: bool = False):
        self.deadline = deadline_epoch
        self.start = start_epoch
        self.checkpoint = checkpoint
        self._pending = [t for t in _DEFAULT_THRESHOLDS if deadline_epoch > start_epoch]

    @classmethod
    def from_env(cls, now: float) -> "TimeBudgetReminder | None":
        raw = os.environ.get(DEADLINE_ENV)
        if not raw:
            return None
        try:
            deadline = float(raw)
        except ValueError:
            return None
        if deadline <= now:
            return None
        return cls(deadline, now, checkpoint=bool(os.environ.get(CHECKPOINT_ENV)))

    def poll(self, now: float) -> str | None:
        """Return a reminder line when a new threshold was crossed, else None."""
        if not self._pending:
            return None
        total = self.deadline - self.start
        if total <= 0:
            return None
        frac = (now - self.start) / total
        crossed: float | None = None
        while self._pending and frac >= self._pending[0]:
            crossed = self._pending.pop(0)
        if crossed is None:
            return None
        remaining_min = max(0, int((self.deadline - now) / 60))
        line = (
            f"[time budget] About {remaining_min} minutes of wall-clock remain for "
            "this task. If long-running work (builds, training, downloads, servers) "
            "is still needed, start it now and verify while it runs; keep further "
            "deliberation short."
        )
        # Work the deadline cuts off is only worth what has been persisted, so
        # from the second threshold on, ask for a durable checkpoint. An agent
        # that saves at 75% and again at 90% still leaves a usable result when
        # it never reaches its own finish line. Phrased around "where the task
        # is evaluated" rather than around git: committing is the right move
        # only when the task asks for it, and a needless commit costs a turn
        # exactly when turns are scarcest.
        if self.checkpoint and crossed >= _CHECKPOINT_THRESHOLD:
            line += (
                " Checkpoint your work NOW before continuing: put whatever "
                "already works where the task will be evaluated -- final file "
                "paths, and a commit on the required branch if the task asks "
                "for committed changes, rather than waiting until everything "
                "is finished. Work left only in scratch space or uncommitted "
                "does not count if you run out of time. After checkpointing, "
                "keep going and checkpoint again as you make progress."
            )
        return line
