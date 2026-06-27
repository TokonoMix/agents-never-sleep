#!/usr/bin/env python3
"""Agent-as-worker BRIDGE acceptance test — proves the real overnight path, not the in-process one.

`run_acceptance.py` drives the in-process `Orchestrator` with a DemoWorker. That proves the spine
but NOT the bridge a real run uses: the agent calling `python3 -m agents_never_sleep.run next`, implementing
the one ticket it is handed, then calling `... complete`, in a loop, across separate processes.

This test is a scripted agent doing exactly that over the same 3 tickets via the real CLI, and it
asserts:
  * identical end states to the in-process demo (DONE / PARKED_DECISION / FAILED_RETRYABLE),
  * the loop terminates with DRAINED (never soft-halts, never asks),
  * the run-incomplete SENTINEL is present while work remains and cleared ONLY at DRAINED
    (this is what structurally prevents a 2am stop now that the agent drives the loop),
  * reversibility: the bad edit is reverted, the good edit kept, the failing diff is captured.

Exit 0 = GREEN (the bridge is proven). Non-zero = the bridge is asserted-but-unproven.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.state import OutcomeState, OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402
from agents_never_sleep.worker import DemoWorker  # noqa: E402

EXPECTED = {
    "ticket-01-trivial": OutcomeState.DONE,
    "ticket-02-ambiguous": OutcomeState.PARKED_DECISION,
    "ticket-03-redgate": OutcomeState.FAILED_RETRYABLE,
}

# Correct unattended usage: run FROM the repo root so the driver's sentinel and the Stop-hook's
# $PWD-based sentinel agree (--repo .). harness is importable via PYTHONPATH=SKILL_ROOT.
COMMON = ["--repo", ".", "--tickets", "tickets",
          "--state-dir", "state", "--artifacts-dir", "artifacts",
          "--report", "morning-report.md"]


def _run_cli(repo, *cli_args):
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"          # real unattended path
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = SKILL_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    args = [sys.executable, "-m", "harness.run", *cli_args]
    proc = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"CLI {cli_args} exited {proc.returncode}\n"
                             f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"CLI {cli_args} did not emit JSON: {exc}\nSTDOUT:\n{proc.stdout}")


def _write_config(repo):
    cfg = {
        "schema_version": 1,
        "gates": [{"name": "tests",
                   "command": [sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"],
                   "blocking": True}],
        "budget": {"per_ticket_timeout_s": 60, "per_ticket_fix_iterations": 3},
        "autonomy": {"non_destructive_only": False, "requirement_ambiguity": "hybrid"},
        "report": {"local_path": "morning-report.md"},
    }
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def main() -> int:
    work = tempfile.mkdtemp(prefix="ue-bridge-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    # tickets live relative to the repo (as in a real project)
    os.makedirs(os.path.join(repo, "tickets"), exist_ok=True)
    for name in os.listdir(os.path.join(HERE, "tickets")):
        shutil.copy(os.path.join(HERE, "tickets", name), os.path.join(repo, "tickets", name))
    _write_config(repo)

    tickets_by_id = {t.id: t for t in load_tickets(os.path.join(repo, "tickets"))}
    demo = DemoWorker()
    sentinel = os.path.join(repo, ".unattended", "run-incomplete")

    failures = []
    statuses = []
    sentinel_seen_set_during_run = False

    # ---- the scripted agent loop: next -> implement -> complete -> next ... --------------
    for _ in range(20):  # bounded: a real bug must not hang the test
        res = _run_cli(repo, "next", *COMMON)
        statuses.append(res.get("status"))

        if res["status"] == "PROCEED":
            if not os.path.exists(sentinel):
                failures.append("sentinel missing while a PROCEED ticket is in flight")
            else:
                sentinel_seen_set_during_run = True
            tid = res["ticket"]["id"]
            ticket = tickets_by_id[tid]
            try:
                attempted = demo.apply(ticket, repo)        # the agent makes the deterministic edit
                done = _run_cli(repo, "complete", *COMMON, "--attempted", attempted)
            except Exception as exc:  # noqa: BLE001 - the demo only knows 01 and 03
                done = _run_cli(repo, "complete", *COMMON,
                                "--attempted", f"cannot implement: {exc}", "--cannot-implement")
            if done["status"] != "RECORDED":
                failures.append(f"complete for {tid} returned {done}")
            continue

        # terminal signal
        break
    else:
        failures.append("agent loop did not terminate within 20 iterations (possible soft-hang)")

    final = statuses[-1] if statuses else None
    if final != "DRAINED":
        failures.append(f"run did not end DRAINED; ended {final} (statuses: {statuses})")
    if not sentinel_seen_set_during_run:
        failures.append("sentinel was never observed set during the run")
    if os.path.exists(sentinel):
        failures.append("sentinel still present after the run terminated — a Stop would be blocked")

    # ---- end states identical to the in-process demo ------------------------------------
    store = OutcomeStore(os.path.join(repo, "state"))
    got = {o.ticket_id: o.state for o in store.all()}
    for tid, want in EXPECTED.items():
        if got.get(tid) != want:
            failures.append(f"{tid}: expected {want.value}, got {got.get(tid)}")

    # ---- reversibility + artifact -------------------------------------------------------
    # INT-1825 bug 2: the run is isolated to a dedicated ans/run-* branch and the operator's branch
    # is restored at the terminal. So the contract is now: (a) the operator's working tree is
    # PRISTINE after the run (the good edit is NOT swept onto it), and (b) the good edit is KEPT on
    # the run branch (reverting the bad edit, keeping the good one) for the operator to review/merge.
    def _git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout

    cur_branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    run_branches = [b.strip().lstrip("* ") for b in _git("branch").splitlines() if "ans/run-" in b]
    if not run_branches:
        failures.append("no ans/run-* branch survived the run — the night's work is gone (bug 2)")
    if cur_branch.startswith("ans/run-"):
        failures.append("terminal left the repo on the run branch — operator branch not restored")

    # (a) operator branch pristine: bad edit absent AND the good edit not swept onto it.
    mathutil = open(os.path.join(repo, "mathutil.py"), encoding="utf-8").read()
    if "return a + b" not in mathutil:
        failures.append("ticket-03 was NOT reverted — mathutil.py still broken on operator branch")
    app_on_operator = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    if "agents-never-sleep demo started" in app_on_operator:
        failures.append("good edit leaked onto the operator branch — run not isolated (bug 2)")

    # (b) good edit kept + bad edit reverted ON the run branch.
    if run_branches:
        rb = run_branches[0]
        app_on_run = _git("show", f"{rb}:app.py")
        if "agents-never-sleep demo started" not in app_on_run:
            failures.append("ticket-01's good edit was lost from the run branch")
        math_on_run = _git("show", f"{rb}:mathutil.py")
        if "return a + b" not in math_on_run:
            failures.append("ticket-03's bad edit was NOT reverted on the run branch")
    if not os.path.exists(os.path.join(repo, "artifacts", "ticket-03-redgate.gate.txt")):
        failures.append("ticket-03 failing-gate artifact missing")

    report = os.path.join(repo, "morning-report.md")
    if not os.path.exists(report):
        failures.append("morning report not written at terminal")

    print(f"status sequence: {statuses}")
    print(f"end states: {{ {', '.join(f'{k}={v.value}' for k, v in sorted(got.items()))} }}")
    print("=" * 60)
    print(f"workdir: {work}")
    if failures:
        print("RESULT: ❌ RED — bridge not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — agent-as-worker bridge proven via the real CLI, sentinel-correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
