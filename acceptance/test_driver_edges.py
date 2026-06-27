#!/usr/bin/env python3
"""StepDriver edge-case tests — the paths the happy-path bridge demo doesn't exercise.

Covers:
  1. NON-DESTRUCTIVE mode: a saved config with autonomy.non_destructive_only must triage (park)
     every PROCEED ticket and edit NOTHING (the safety control the advisor flagged as un-enforced).
  2. complete WITHOUT an in-flight ticket -> a clean ERROR, never a crash.
  3. CROSS-RESUME RETRY: a FAILED_RETRYABLE ticket set aside in one run is retried on the next run
     (fresh = sentinel absent), and a second identical failure trips loop-detection -> PARKED.
  4. The morning report is written at the terminal signal.

Uses the in-process StepDriver (state lives on disk, so two drive() passes simulate two resumes).
Exit 0 = GREEN.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.driver import StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402
from agents_never_sleep.worker import DemoWorker  # noqa: E402


def _build(work, *, non_destructive=False):
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")          # outside repo: irrelevant to git here
    artifacts_dir = os.path.join(work, "artifacts")
    report_path = os.path.join(work, "morning-report.md")
    tickets = load_tickets(os.path.join(HERE, "tickets"))
    store = OutcomeStore(state_dir)
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=DemoWorker(),
                        artifacts_dir=artifacts_dir, unattended=True, ledger=ledger)
    driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                        report_path=report_path, non_destructive=non_destructive)
    return repo, store, tickets, driver, report_path


def _drive(driver, tickets, repo, max_iter=40):
    """Play the scripted agent: implement each PROCEED ticket, then complete. Return statuses."""
    by_id = {t.id: t for t in tickets}
    demo = DemoWorker()
    statuses = []
    for _ in range(max_iter):
        res = driver.next_ticket()
        statuses.append(res["status"])
        if res["status"] != "PROCEED":
            return statuses
        tid = res["ticket"]["id"]
        try:
            attempted = demo.apply(by_id[tid], repo)
            driver.complete_ticket(attempted=attempted)
        except Exception as exc:  # noqa: BLE001
            driver.complete_ticket(attempted=str(exc), cannot_implement=True)
    raise AssertionError("drive did not terminate")


def main() -> int:
    failures = []

    # --- 1. non-destructive mode parks everything and edits nothing ----------------------
    work1 = tempfile.mkdtemp(prefix="ue-edge-nd-")
    repo, store, tickets, driver, report_path = _build(work1, non_destructive=True)
    app_before = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    math_before = open(os.path.join(repo, "mathutil.py"), encoding="utf-8").read()
    statuses = _drive(driver, tickets, repo)
    if statuses[-1] != "DRAINED":
        failures.append(f"[non-destructive] expected DRAINED, got {statuses}")
    if "PROCEED" in statuses:
        failures.append(f"[non-destructive] handed a PROCEED ticket (should triage all): {statuses}")
    states = {o.ticket_id: o.state for o in store.all()}
    if any(s != OutcomeState.PARKED_DECISION for s in states.values()):
        failures.append(f"[non-destructive] not all parked: {states}")
    if open(os.path.join(repo, "app.py"), encoding="utf-8").read() != app_before or \
            open(os.path.join(repo, "mathutil.py"), encoding="utf-8").read() != math_before:
        failures.append("[non-destructive] files were edited despite non_destructive_only")
    if not os.path.exists(report_path):
        failures.append("[non-destructive] no report at terminal")

    # --- 2. complete with no in-flight ticket -> clean ERROR -----------------------------
    work2 = tempfile.mkdtemp(prefix="ue-edge-nopending-")
    _, _, _, driver2, _ = _build(work2)
    res = driver2.complete_ticket(attempted="nothing in flight")
    if res.get("status") != "ERROR":
        failures.append(f"[no-pending] expected ERROR, got {res}")

    # --- 3. cross-resume retry of a set-aside FAILED_RETRYABLE ticket --------------------
    work3 = tempfile.mkdtemp(prefix="ue-edge-resume-")
    repo3, store3, tickets3, driver3, _ = _build(work3)
    first = _drive(driver3, tickets3, repo3)            # run 1: 03 -> FAILED_RETRYABLE, set aside
    s_after_1 = {o.ticket_id: o.state for o in store3.all()}
    if s_after_1.get("ticket-03-redgate") != OutcomeState.FAILED_RETRYABLE:
        failures.append(f"[resume] run1 expected 03 FAILED_RETRYABLE, got {s_after_1}")
    if first[-1] != "DRAINED":
        failures.append(f"[resume] run1 did not DRAIN: {first}")
    second = _drive(driver3, tickets3, repo3)           # run 2 (fresh): 03 retried -> loop -> PARKED
    o3 = store3.read("ticket-03-redgate")
    if o3.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[resume] run2 expected 03 PARKED_DECISION (loop), got {o3.state}")
    if o3.attempts != 2:
        failures.append(f"[resume] run2 expected attempts=2, got {o3.attempts}")
    if "PROCEED" not in second:
        failures.append(f"[resume] run2 did not retry the set-aside ticket: {second}")

    # --- 4. INT-1675 #1: complete with an UNLOADED ticket source must NOT revert work ----
    work4 = tempfile.mkdtemp(prefix="ue-edge-noticket-")
    repo4, store4, tickets4, driver4, report4 = _build(work4)
    r = driver4.next_ticket()                              # hand out + implement ONE real ticket
    if r.get("status") != "PROCEED":
        failures.append(f"[no-ticket-source] expected first next() PROCEED, got {r}")
    else:
        tid = r["ticket"]["id"]
        by_id4 = {t.id: t for t in tickets4}
        DemoWorker().apply(by_id4[tid], repo4)             # real uncommitted edits on top of pre-snapshot
        app_edit = open(os.path.join(repo4, "app.py"), encoding="utf-8").read()
        math_edit = open(os.path.join(repo4, "mathutil.py"), encoding="utf-8").read()
        # Simulate `complete` run WITHOUT --tickets (no Paperclip): a driver whose ticket source
        # loaded EMPTY, sharing the same repo + state so it finds the in-flight pending record.
        state4 = os.path.join(work4, "state")
        gate4 = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                                    "-p", "test_*.py"], cwd=repo4, timeout=60)
        orch4b = Orchestrator(repo_dir=repo4, store=store4, gate=gate4, worker=DemoWorker(),
                              artifacts_dir=os.path.join(work4, "artifacts"), unattended=True,
                              ledger=AttemptLedger(os.path.join(state4, "ledger.json")))
        driver4b = StepDriver(orch=orch4b, tickets=[], store=store4, state_dir=state4,
                              report_path=report4)
        res4 = driver4b.complete_ticket(attempted="done but ticket source not loaded")
        if res4.get("status") != "ERROR":
            failures.append(f"[no-ticket-source] expected ERROR, got {res4}")
        if open(os.path.join(repo4, "app.py"), encoding="utf-8").read() != app_edit or \
                open(os.path.join(repo4, "mathutil.py"), encoding="utf-8").read() != math_edit:
            failures.append("[no-ticket-source] DATA LOSS: edits reverted despite unloaded ticket source")
        if driver4b._load_pending() is None:
            failures.append("[no-ticket-source] pending cleared — operator cannot finalize on re-run")

    print(f"non-destructive statuses: {statuses}")
    print(f"resume run1: {first} | run2: {second}")
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — driver edge cases not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — non-destructive triage, no-pending guard, cross-resume retry all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
