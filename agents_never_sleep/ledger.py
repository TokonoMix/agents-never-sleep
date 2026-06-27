"""Attempt ledger + state-loop detection (council A6 + missing-#4).

Two failure modes a heartbeat watchdog is blind to:
  * a ticket that crashes and gets restarted forever (cross-resume retry with no cap),
  * a ticket that loops FAST under budget, failing the same way each time.

The ledger persists, across resumes, how many times each ticket was attempted and how often a
given failure signature recurred. The orchestrator consults it to force-PARK a ticket that has
exceeded its attempt cap or is provably looping, instead of burning the night on one cursed item.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile


def failure_signature(text: str) -> str:
    """A stable fingerprint of WHAT failed, robust to volatile noise.

    Hashing raw gate output is fragile: durations ("Ran 2 tests in 0.002s"), temp paths, memory
    addresses and line ordering all vary run-to-run and would make "the same failure" look
    different, defeating loop detection. So we first try to extract the stable failure IDENTIFIERS
    a test runner prints (FAIL:/ERROR: lines, exception types), normalize and sort them, and hash
    those. Only if none are found do we fall back to a heavily-normalized full text.
    """
    import re

    def _norm(s: str) -> str:
        s = re.sub(r"0x[0-9a-fA-F]+", "", s)          # memory addresses
        s = re.sub(r"/[\w./\-]+", "", s)               # absolute paths
        s = re.sub(r"\d+\.\d+s?", "", s)               # durations / floats
        s = re.sub(r"\b\d+\b", "", s)                  # line numbers / counts
        return s.strip()

    markers = []
    for line in text.splitlines():
        if re.match(r"\s*(FAIL|ERROR|FAILED|PASSED|FAILURES?):", line) or \
                re.search(r"\b\w*(Error|Exception)\b\s*:", line):
            markers.append(_norm(line))

    basis = "\n".join(sorted(set(m for m in markers if m))) if markers else _norm(text)
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]


class AttemptLedger:
    def __init__(self, path: str):
        self.path = path
        self._data = {"attempts": {}, "signatures": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = loaded
            except (json.JSONDecodeError, OSError):
                pass
        # Normalize regardless of how (or whether) the file loaded, so a partial/corrupt record
        # can never KeyError later in record_failure/record_attempt and crash the run.
        self._data.setdefault("attempts", {})
        self._data.setdefault("signatures", {})

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".ledger-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def record_attempt(self, ticket_id: str) -> int:
        n = self._data["attempts"].get(ticket_id, 0) + 1
        self._data["attempts"][ticket_id] = n
        self._flush()
        return n

    def attempts(self, ticket_id: str) -> int:
        return self._data["attempts"].get(ticket_id, 0)

    def over_cap(self, ticket_id: str, cap: int) -> bool:
        return self.attempts(ticket_id) >= cap

    def reset_attempts(self, ticket_id: str) -> int:
        # INT-1675 P3: a first-class operator escape hatch for the documented "kill+resume / tooling
        # round-trip inflated the attempt counter -> healthy ticket force-parked at the cap" case, so
        # operators stop hand-editing ledger.json. Returns the prior count (0 if it had none).
        prior = self._data["attempts"].pop(ticket_id, 0)
        if prior:
            self._flush()
        return prior

    def record_failure(self, ticket_id: str, signature: str) -> int:
        key = f"{ticket_id}:{signature}"
        n = self._data["signatures"].get(key, 0) + 1
        self._data["signatures"][key] = n
        self._flush()
        return n

    def loop_detected(self, ticket_id: str, signature: str, threshold: int = 2) -> bool:
        key = f"{ticket_id}:{signature}"
        return self._data["signatures"].get(key, 0) >= threshold
