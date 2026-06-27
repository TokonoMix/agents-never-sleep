#!/usr/bin/env python3
"""Prove the cross-resume attempt-ledger + state-loop detection.

ticket-03 always breaks the gate. On the first pass it should be FAILED_RETRYABLE (retryable).
On a SECOND pass (a 'resume') the same failure signature recurs, loop detection trips, and the
ticket must become PARKED_DECISION ('unproductive looping') rather than retrying forever — the
exact gap a heartbeat watchdog is blind to.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402
from agents_never_sleep.worker import DemoWorker  # noqa: E402


def build(repo, state_dir, artifacts_dir):
    gate = GateRunner(
        command=[sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        cwd=repo, timeout=60,
    )
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    return Orchestrator(repo_dir=repo, store=OutcomeStore(state_dir), gate=gate,
                        worker=DemoWorker(), artifacts_dir=artifacts_dir, unattended=True,
                        ledger=ledger, loop_threshold=2)


def main() -> int:
    work = tempfile.mkdtemp(prefix="ue-resume-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir, artifacts_dir = os.path.join(work, "state"), os.path.join(work, "art")
    tickets = [t for t in load_tickets(os.path.join(HERE, "tickets")) if t.id == "ticket-03-redgate"]

    r1 = build(repo, state_dir, artifacts_dir).run(tickets)
    s1 = r1.outcomes[0].state
    r2 = build(repo, state_dir, artifacts_dir).run(tickets)   # resume: same state dir + ledger
    s2 = r2.outcomes[0].state
    attempts = r2.outcomes[0].attempts

    print(f"pass 1: {s1.value}  ->  pass 2 (resume): {s2.value}  (attempts={attempts})")
    ok = (s1 == OutcomeState.FAILED_RETRYABLE and s2 == OutcomeState.PARKED_DECISION)
    print("RESULT:", "✅ GREEN — loop detected on resume, parked not retried forever"
          if ok else "❌ RED — resume/loop-detection did not behave as specified")
    print(f"workdir: {work}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
