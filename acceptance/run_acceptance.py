#!/usr/bin/env python3
"""Acceptance demo — the ONLY verification that maps to the '2am stop' pain.

Sets up a throwaway sandbox repo, runs the harness UNATTENDED over the 3 acceptance tickets,
and asserts the harness:
  * drove all three end-to-end without ever asking a live question / halting,
  * produced the correct durable outcome state for each,
  * reverted the bad edit (tree clean) and kept the good edit,
  * wrote a morning report.

Exit code 0 = GREEN (MVP slice-1 done). Non-zero = the spine is not proven yet.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness.gates import GateRunner  # noqa: E402
from harness.ledger import AttemptLedger  # noqa: E402
from harness.orchestrator import Orchestrator  # noqa: E402
from harness.report import build_report  # noqa: E402
from harness.state import OutcomeState, OutcomeStore  # noqa: E402
from harness.tickets import load_tickets  # noqa: E402
from harness.worker import DemoWorker  # noqa: E402

EXPECTED = {
    "ticket-01-trivial": OutcomeState.DONE,
    "ticket-02-ambiguous": OutcomeState.PARKED_DECISION,
    "ticket-03-redgate": OutcomeState.FAILED_RETRYABLE,
}


def main() -> int:
    work = tempfile.mkdtemp(prefix="ue-acceptance-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    artifacts_dir = os.path.join(work, "artifacts")
    report_path = os.path.join(work, "morning-report.md")

    tickets = load_tickets(os.path.join(HERE, "tickets"))
    store = OutcomeStore(state_dir)
    gate = GateRunner(
        command=[sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        cwd=repo, timeout=60,
    )
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(
        repo_dir=repo, store=store, gate=gate, worker=DemoWorker(),
        artifacts_dir=artifacts_dir, unattended=True, ledger=ledger,
    )

    result = orch.run(tickets)

    report = build_report(
        result.outcomes, run_label="acceptance demo",
        halted=result.halted, halt_reason=result.halt_reason,
        stopped_low_yield=result.stopped_low_yield,
    )
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    # ---- assertions ----
    failures = []
    if result.halted:
        failures.append(f"run HALTED unexpectedly: {result.halt_reason}")

    got = {o.ticket_id: o.state for o in result.outcomes}
    for tid, want in EXPECTED.items():
        if got.get(tid) != want:
            failures.append(f"{tid}: expected {want.value}, got {got.get(tid)}")

    # reversibility: the bad edit must be gone, the good edit must remain
    mathutil = open(os.path.join(repo, "mathutil.py"), encoding="utf-8").read()
    if "return a + b" not in mathutil:
        failures.append("ticket-03 was NOT reverted — mathutil.py still broken")
    app = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    if "agents-never-sleep demo started" not in app:
        failures.append("ticket-01's good edit was lost")

    # the failing diff must have been captured as an artifact
    art = os.path.join(artifacts_dir, "ticket-03-redgate.gate.txt")
    if not os.path.exists(art):
        failures.append("ticket-03 failing-gate artifact missing")

    print(report)
    print("=" * 60)
    print(f"workdir: {work}")
    if failures:
        print("RESULT: ❌ RED — slice-1 not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — 3-ticket demo passed, never stopped, states correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
