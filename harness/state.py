"""Durable per-ticket outcome state machine.

This is the component the council named "the one thing to get right": every other
component reads/writes these records, and a crash mid-write must never corrupt them or
leave the run unable to resume. So writes are atomic (temp file + fsync + os.replace) and
every record carries the full set of fields a morning report / resume needs.

States (see BUILD-PLAN.md):
  DONE                 - completed, gates green
  DONE_LOW_CONFIDENCE  - completed but review coverage was degraded
  PARKED_DECISION      - a single decision deferred; run kept moving
  PARKED_FOUNDATIONAL  - foundational ambiguity; ticket parked + dependents quarantined
  BLOCKED_ENV          - environment/tooling blocked progress (not the agent's fault)
  FAILED_RETRYABLE     - a gate failed on the diff; reverted; can be retried
  FAILED_BUG_IN_AGENT  - the harness/agent did something wrong; needs a human look
"""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import tempfile
import time
from typing import Optional


class OutcomeState(str, enum.Enum):
    DONE = "DONE"
    DONE_LOW_CONFIDENCE = "DONE_LOW_CONFIDENCE"
    PARKED_DECISION = "PARKED_DECISION"
    PARKED_FOUNDATIONAL = "PARKED_FOUNDATIONAL"
    BLOCKED_ENV = "BLOCKED_ENV"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_BUG_IN_AGENT = "FAILED_BUG_IN_AGENT"


# Which states mean "this ticket is finished, skip on resume" vs "may be retried".
TERMINAL_SKIP_ON_RESUME = {
    OutcomeState.DONE,
    OutcomeState.DONE_LOW_CONFIDENCE,
    OutcomeState.PARKED_DECISION,
    OutcomeState.PARKED_FOUNDATIONAL,
}

# Contamination scope: how far a parked/failed ticket's risk can spread. The scheduler
# may only pick a next "independent" ticket whose scope does not intersect a parked one.
class ContaminationScope(str, enum.Enum):
    NONE = "none"
    FILE = "file"
    MODULE = "module"
    PACKAGE = "package"
    SERVICE = "service"
    REPO = "repo"
    EXTERNAL = "external"


@dataclasses.dataclass
class TicketOutcome:
    ticket_id: str
    state: OutcomeState
    why: str = ""                      # one-line rationale
    category: str = ""                 # blast-radius category that drove the decision
    evidence: str = ""                 # gate output / classifier signal
    attempted: str = ""                # what the worker tried
    exact_blocker: str = ""            # the precise thing that blocked, if any
    human_action_required: str = ""    # the exact next action for the morning report
    dependents_quarantined: list = dataclasses.field(default_factory=list)
    work_product_behind_flag: Optional[str] = None  # path/flag if built reversibly
    contamination_scope: ContaminationScope = ContaminationScope.NONE
    attempts: int = 1                  # monotonic across resumes
    artifact_path: Optional[str] = None  # e.g. the failing diff
    review_coverage: str = "n/a"       # which reviewers/councils actually ran
    created_at: float = dataclasses.field(default_factory=lambda: 0.0)
    updated_at: float = dataclasses.field(default_factory=lambda: 0.0)

    def to_json(self) -> dict:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        d["contamination_scope"] = self.contamination_scope.value
        return d

    @staticmethod
    def from_json(d: dict) -> "TicketOutcome":
        d = dict(d)
        d["state"] = OutcomeState(d["state"])
        d["contamination_scope"] = ContaminationScope(d.get("contamination_scope", "none"))
        # tolerate older/newer records: keep only known fields
        known = {f.name for f in dataclasses.fields(TicketOutcome)}
        return TicketOutcome(**{k: v for k, v in d.items() if k in known})


class OutcomeStore:
    """One JSON file per ticket under <state_dir>/, written atomically."""

    def __init__(self, state_dir: str, clock=time.time):
        self.state_dir = state_dir
        self._clock = clock
        os.makedirs(state_dir, exist_ok=True)

    def _path(self, ticket_id: str) -> str:
        safe = ticket_id.replace("/", "_")
        return os.path.join(self.state_dir, f"{safe}.json")

    def read(self, ticket_id: str) -> Optional[TicketOutcome]:
        path = self._path(ticket_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return TicketOutcome.from_json(json.load(fh))

    def write(self, outcome: TicketOutcome) -> None:
        now = self._clock()
        if not outcome.created_at:
            outcome.created_at = now
        outcome.updated_at = now
        path = self._path(outcome.ticket_id)
        # atomic: write temp in same dir, fsync, replace
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(outcome.to_json(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def all(self) -> list:
        """All ticket outcomes in this dir. Sibling bookkeeping files (ledger.json, pending.json,
        capability-profile.json, heartbeat.json) share the directory, so skip anything that is not
        a well-formed outcome record rather than crashing on it."""
        out = []
        for name in sorted(os.listdir(self.state_dir)):
            if name.startswith(".tmp-") or not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.state_dir, name), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict) or "state" not in data or "ticket_id" not in data:
                    continue  # not an outcome record
                out.append(TicketOutcome.from_json(data))
            except (json.JSONDecodeError, OSError, ValueError, KeyError):
                continue
        return out
