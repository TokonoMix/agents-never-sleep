"""Heartbeat — liveness signal so an external watchdog can detect a hung PARENT.

The orchestrator beats once per ticket-step (ticket id + phase + monotonic counter + wall time).
A watchdog (see harness/watchdog.py) polls the file; if it goes stale beyond a threshold the
parent is presumed hung — the failure mode the Stop-hook is blind to (a hang is not a stop).

Writes are atomic so a crash mid-beat never leaves a torn file the watchdog misreads.
"""
from __future__ import annotations

import json
import os
import tempfile
import time


class Heartbeat:
    def __init__(self, path: str, clock=time.time):
        self.path = path
        self._clock = clock
        self._n = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def beat(self, ticket_id: str = "", phase: str = "") -> None:
        self._n += 1
        rec = {"ts": self._clock(), "n": self._n, "ticket": ticket_id, "phase": phase,
               "pid": os.getpid()}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".hb-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def age_seconds(path: str, now: float | None = None) -> float | None:
        """Seconds since the last beat, or None if no (readable) heartbeat exists."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                ts = json.load(fh).get("ts")
        except (OSError, json.JSONDecodeError):
            return None
        if ts is None:
            return None
        return (now if now is not None else time.time()) - ts
