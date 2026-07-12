#!/usr/bin/env python3
"""Stale run-branch resume guard — a NEW backlog must never silently resume a STRANGER run.

Failure class (reported cross-team, generic to any shared/live checkout): at the FIRST `next` of a
run, `_enter_run_branch` resumed a run-branch persisted by a PRIOR run purely on the PRESENCE of
`run-branch.json` (a kill-9 leaves that file behind because `_exit_run_branch` never ran). If the
prior run's base was no longer an ancestor of the operator's branch, the blind `git checkout` moved
HEAD off live commits AND could delete untracked working-tree files (the ticket source itself).

The fix binds resume to a freshness assertion: persist the base SHA at run-branch creation and, on
resume, HALT (loud, non-zero, state left intact for inspection) instead of a silent checkout when
the recorded base is no longer an ancestor of the operator's branch (or the branch/base vanished).
Compare against `original_branch` — the operator's ground truth — NEVER current HEAD, which in a
shared checkout may itself be junk left by the prior process.

Exit 0 = GREEN.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.driver import RunResumeUnsafe, StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.state import OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import Ticket  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _new_repo():
    repo = tempfile.mkdtemp(prefix="ue-freshness-")
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


def _rewrite_main_unrelated(repo):
    """Force `main` onto an ORPHAN history so the prior run's base is no longer an ancestor of it
    (models the kill-9-then-new-backlog / force-push-away case)."""
    _git(repo, "checkout", "-q", "--orphan", "tmproot")
    with open(os.path.join(repo, "readme.md"), "w") as fh:
        fh.write("unrelated root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated root")
    _git(repo, "branch", "-qM", "main")


def test_stale_resume_halts_and_preserves_untracked(failures):
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    runbranch_path = os.path.join(work, "state", "run-branch.json")

    # Process 1: a run creates + persists an ans/run-* branch with its base recorded.
    r1 = _driver(repo, work).next_ticket()
    if r1.get("status") != "PROCEED":
        failures.append(f"[fresh] setup: expected PROCEED, got {r1.get('status')}: {r1.get('error','')}")
    st = json.load(open(runbranch_path, encoding="utf-8"))
    if not st.get("base"):
        failures.append("[fresh] run-branch.json must persist the base SHA at creation (missing)")

    # The operator's branch is rewound onto an unrelated history (prior run is now a STRANGER).
    _rewrite_main_unrelated(repo)

    # An untracked ticket-source file the blind checkout would have destroyed.
    victim = os.path.join(repo, "ticket-source.txt")
    with open(victim, "w") as fh:
        fh.write("do not delete me\n")

    # A fresh process must HALT rather than silently checking out the stale run branch.
    try:
        r2 = _driver(repo, work).next_ticket()
        failures.append(f"[fresh] stale resume must HALT (RunResumeUnsafe); got {r2.get('status')!r}")
    except RunResumeUnsafe:
        pass  # loud HALT — correct

    if not os.path.exists(victim):
        failures.append("[fresh] untracked ticket source was destroyed by a resume checkout (data loss)")
    if not os.path.exists(runbranch_path):
        failures.append("[fresh] HALT must leave run-branch.json intact for operator inspection")
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def test_missing_base_is_treated_as_stale(failures):
    """A run-branch.json written by a pre-guard version (no `base`) is unverifiable -> HALT, not pass."""
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    state_dir = os.path.join(work, "state")
    os.makedirs(state_dir, exist_ok=True)
    # A real run branch exists, but the persisted state predates the base field.
    _git(repo, "branch", "ans/run-legacy")
    with open(os.path.join(state_dir, "run-branch.json"), "w") as fh:
        json.dump({"run_branch": "ans/run-legacy", "original_branch": "main"}, fh)
    try:
        r = _driver(repo, work).next_ticket()
        failures.append(f"[fresh] base-less state must HALT; got {r.get('status')!r}")
    except RunResumeUnsafe:
        pass
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def test_healthy_resume_still_proceeds(failures):
    """The guard must NOT over-trigger: a normal continuing run (base still an ancestor of the
    operator's branch) resumes without a HALT."""
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")

    r1 = _driver(repo, work).next_ticket()
    if r1.get("status") != "PROCEED":
        failures.append(f"[fresh] setup: expected PROCEED, got {r1.get('status')}")
    # Agent implements the ticket; a second process completes it — the run-branch base is unchanged
    # and still an ancestor of main, so no HALT.
    with open(os.path.join(repo, "app.py"), "a") as fh:
        fh.write("# greeting\n")
    try:
        out = _driver(repo, work).complete_ticket(attempted="added a comment")
    except RunResumeUnsafe:
        failures.append("[fresh] healthy continuing run wrongly HALTed as stale (false positive)")
        out = {}
    if out and out.get("status", "").startswith("HALT"):
        failures.append(f"[fresh] healthy run should not HALT; got {out.get('status')!r}")
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def test_git_error_in_safety_check_halts_not_degrades(failures):
    """Review finding #1: a GitError raised BY the safety check itself (git binary missing/hung)
    must fail to the SAFE side (HALT), not be swallowed by the degrade-catch and silently proceed."""
    from agents_never_sleep.vcs import GitError
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    _driver(repo, work).next_ticket()  # process 1: persist a valid run-branch.json

    drv = _driver(repo, work)  # process 2

    def _boom(*_a, **_k):
        raise GitError("git binary vanished mid-check")
    drv.orch.git.branch_exists = _boom  # the safety check now hits the raise class

    try:
        r = drv.next_ticket()
        failures.append(f"[fresh] GitError in safety check must HALT, not degrade; got {r.get('status')!r}")
    except RunResumeUnsafe:
        pass  # correct: unverifiable lineage -> HALT
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def test_unborn_repo_defers_isolation_no_bogus_base(failures):
    """Review finding #2 (refined): a repo with no commit yet (unborn HEAD). `git.head()` returns a
    non-SHA ("HEAD" or "") there, so persisting it as `base` either wedges the next process OR
    silently DEFEATS the guard (is_ancestor("HEAD","HEAD") is always True). Correct behavior: defer
    isolation until a real baseline commit exists — never persist a non-commit base; never HALT."""
    import re
    repo = tempfile.mkdtemp(prefix="ue-freshness-unborn-")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "app.py"), "w") as fh:
        fh.write("print('hi')\n")  # present but UNCOMMITTED -> HEAD is unborn
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    runbranch_path = os.path.join(work, "state", "run-branch.json")
    try:
        r1 = _driver(repo, work).next_ticket()
        if r1.get("status") not in ("PROCEED", "DRAINED"):
            failures.append(f"[fresh] unborn-repo next should proceed/drain, got {r1.get('status')!r}")
        # If a run branch was persisted, its base MUST be a real commit SHA — never "HEAD"/"".
        if os.path.exists(runbranch_path):
            base = json.load(open(runbranch_path, encoding="utf-8")).get("base", "")
            if not re.fullmatch(r"[0-9a-f]{40}", base):
                failures.append(f"[fresh] unborn repo persisted a non-SHA base {base!r} "
                                "(guard defeated / wedge risk) — isolation should be deferred instead")
        with open(os.path.join(repo, "app.py"), "a") as fh:
            fh.write("# greeting\n")
        _driver(repo, work).complete_ticket(attempted="added a comment")
    except RunResumeUnsafe:
        failures.append("[fresh] unborn repo wedged on a false HALT (non-commit base persisted)")
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def test_recovery_after_stale_halt_does_not_graft_old_lineage(failures):
    """2026-07-08 E2E finding 2.2: following the stale-resume HALT's OWN recovery instruction
    (inspect, then remove run-branch.json) must yield a working fresh run. Before the fix the
    crash-recovery block reset the freshly forked run branch `--hard` to the OLD run's
    pending.snapshot with no ancestry check — silently grafting it onto the disconnected old
    lineage (exit 0, PROCEED); the corruption only surfaced on the NEXT call as a second
    HALT_RESUME_UNSAFE that looked unrelated to the instruction the operator had just followed."""
    import time
    repo = _new_repo()
    work = tempfile.mkdtemp(prefix="ue-freshness-wk-")
    os.environ["UE_RUN_INCOMPLETE"] = os.path.join(work, "state", "run-incomplete")
    runbranch_path = os.path.join(work, "state", "run-branch.json")
    pending_path = os.path.join(work, "state", "pending.json")

    # Process 1: a run forks its branch and hands out t1 (pending.json checkpoint), then dies.
    r1 = _driver(repo, work).next_ticket()
    if r1.get("status") != "PROCEED":
        failures.append(f"[graft] setup: expected PROCEED, got {r1.get('status')!r}")
        return
    if not os.path.exists(pending_path):
        failures.append("[graft] setup: PROCEED must leave a pending.json checkpoint")
        return

    _rewrite_main_unrelated(repo)  # the operator's branch moved to an unrelated history

    # First HALT: documented and expected (the stale-resume guard).
    try:
        _driver(repo, work).next_ticket()
        failures.append("[graft] stale resume must HALT before any recovery")
        return
    except RunResumeUnsafe:
        pass

    # The operator follows the HALT's instruction: remove run-branch.json, call `next` again.
    # (The sleep keeps the new ans/run-<ts>-<pid> name from colliding with process 1's branch —
    # same pid in-process, and possibly the same strftime second.)
    os.unlink(runbranch_path)
    time.sleep(1.1)
    new_main = _git(repo, "rev-parse", "main").stdout.strip()
    try:
        r3 = _driver(repo, work).next_ticket()
    except RunResumeUnsafe:
        failures.append("[graft] recovery per the HALT's own instruction HALTed again immediately")
        return
    if r3.get("status") != "PROCEED":
        failures.append(f"[graft] recovery `next` should PROCEED, got {r3.get('status')!r}")
    # The freshly forked run branch must still descend from the operator's NEW tip. A reset
    # --hard to the old pending.snapshot moves HEAD onto the old, disconnected lineage instead.
    if _git(repo, "merge-base", "--is-ancestor", new_main, "HEAD").returncode != 0:
        failures.append("[graft] run branch was grafted onto the OLD run's lineage "
                        "(pending.snapshot reset without an ancestry check)")

    # And the call AFTER recovery must not HALT either — this is where the graft used to surface.
    try:
        r4 = _driver(repo, work).next_ticket()
        if str(r4.get("status", "")).startswith("HALT"):
            failures.append(f"[graft] second call after recovery HALTed: {r4.get('status')!r}")
    except RunResumeUnsafe:
        failures.append("[graft] second call after recovery hit HALT_RESUME_UNSAFE — the exact "
                        "delayed corruption the recovery instruction was supposed to avoid")
    os.environ.pop("UE_RUN_INCOMPLETE", None)


def main() -> int:
    failures = []
    test_stale_resume_halts_and_preserves_untracked(failures)
    test_missing_base_is_treated_as_stale(failures)
    test_healthy_resume_still_proceeds(failures)
    test_git_error_in_safety_check_halts_not_degrades(failures)
    test_unborn_repo_defers_isolation_no_bogus_base(failures)
    test_recovery_after_stale_halt_does_not_graft_old_lineage(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — stale run-branch resume is not guarded")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — stale resume HALTs loudly; untracked preserved; healthy resume proceeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
