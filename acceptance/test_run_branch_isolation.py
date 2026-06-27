#!/usr/bin/env python3
"""INT-1825 bug 2 — harness snapshots must NOT pollute the operator's working branch.

`Orchestrator.begin_proceed`/finalize do `commit_all("pre:..."/"done:...")` on whatever branch is
checked out. In a real run that is the operator's branch (main), so every accounting/result commit
lands on main — during the S2 run this polluted main directly. The fix: a fresh run creates and
checks out a dedicated `ans/run-<ts>` branch, persists its name in the state dir (each next/complete
is a FRESH PROCESS, so it must survive across them), every later command defensively checks it out
BEFORE the crash-recovery revert, and the terminal signal checks the operator's branch back out
(leaving the run branch for review/merge). The operator's branch HEAD must be byte-for-byte
unchanged by a whole run.

Exit 0 = GREEN.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.driver import StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.state import OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import Ticket  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _new_repo():
    repo = tempfile.mkdtemp(prefix="ue-runbranch-")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "app.py"), "w") as fh:
        fh.write("print('hi')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "init")
    return repo


def _driver(repo, work):
    """A fresh StepDriver bound to shared on-disk state (simulates a new process each call)."""
    state_dir = os.path.join(work, "state")
    art = os.path.join(work, "art")
    orch = Orchestrator(
        repo_dir=repo, store=OutcomeStore(state_dir),
        gate=GateRunner(command=["true"], cwd=repo, timeout=30),
        worker=None, artifacts_dir=art, unattended=True,
        ledger=AttemptLedger(os.path.join(state_dir, "ledger.json")),
        protect_paths=[".unattended"],
    )
    tickets = [Ticket(id="t1", title="Add a greeting comment",
                      body="add a line comment to app.py", meta={}, path="")]
    return StepDriver(orch=orch, tickets=tickets, store=OutcomeStore(state_dir),
                      state_dir=state_dir, report_path=os.path.join(repo, "night-report.md"),
                      config={})


def _branch(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def test_run_keeps_operator_branch_pristine(failures):
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-runbranch-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    main_head_before = _git(repo, "rev-parse", "main").stdout.strip()

    # Process 1: fresh run -> should create + switch to an ans/run-* branch and hand out PROCEED.
    r1 = _driver(repo, work).next_ticket()
    if r1.get("status") != "PROCEED":
        failures.append(f"[bug2] expected PROCEED, got {r1.get('status')}: {r1.get('error', '')}")
    cur = _branch(repo)
    if not cur.startswith("ans/run-"):
        failures.append(f"[bug2] fresh run did not switch to an ans/run-* branch (on {cur!r})")
    if _git(repo, "rev-parse", "main").stdout.strip() != main_head_before:
        failures.append("[bug2] the pre:<ticket> snapshot landed on main (main HEAD moved)")

    # The agent 'implements' the ticket.
    with open(os.path.join(repo, "app.py"), "a") as fh:
        fh.write("# greeting\n")

    # Process 2 (fresh driver, shared state): complete the ticket.
    _driver(repo, work).complete_ticket(attempted="added a comment")
    if _git(repo, "rev-parse", "main").stdout.strip() != main_head_before:
        failures.append("[bug2] the done:<ticket> commit landed on main (main HEAD moved)")

    # Process 3: drain -> terminal -> must restore the operator's branch.
    r3 = _driver(repo, work).next_ticket()
    if r3.get("status") != "DRAINED":
        failures.append(f"[bug2] expected DRAINED terminal, got {r3.get('status')}")
    if _branch(repo) != "main":
        failures.append(f"[bug2] terminal did not check the operator branch back out (on {_branch(repo)!r})")
    if _git(repo, "rev-parse", "main").stdout.strip() != main_head_before:
        failures.append("[bug2] main HEAD changed across the whole run — branch not isolated")

    # The work must still exist on the run branch for the operator to merge.
    runbranches = [b.strip().lstrip("* ") for b in _git(repo, "branch").stdout.splitlines()
                   if "ans/run-" in b]
    if not runbranches:
        failures.append("[bug2] the run branch was deleted — the night's work is gone")

    # The terminal must POINT the operator at the run branch (else an overnight run hides its output).
    if r3.get("run_branch") not in runbranches:
        failures.append(f"[bug2] terminal did not surface the run branch (got {r3.get('run_branch')!r})")
    report_txt = open(os.path.join(repo, "night-report.md"), encoding="utf-8").read()
    if runbranches and runbranches[0] not in report_txt:
        failures.append("[bug2] morning report does not name the run branch to merge")
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def main() -> int:
    failures = []
    test_run_keeps_operator_branch_pristine(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — harness still pollutes the operator's branch")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — run isolated to ans/run-* branch; operator branch pristine + restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
