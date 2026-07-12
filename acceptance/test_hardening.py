#!/usr/bin/env python3
"""Regression tests for the code-review hardening fixes (BUG1 / BUG3 / BUG4).

- BUG1: the low-yield breaker must be RUN-SCOPED — a fresh resume of a backlog that already has many
  parked/failed outcomes in the store must NOT trip LOW_YIELD before doing any new work.
- BUG3: a git failure (timeout / missing binary) during snapshot or revert must become a clean
  BLOCKED_ENV outcome, never an uncaught crash that kills the run with no recorded state.
- BUG4: an unattended run where the Stop-hook can't find the sentinel (CWD != --repo and
  UE_RUN_INCOMPLETE unset) must HARD-FAIL, not silently disable never-stop.

Exit 0 = GREEN.
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

from agents_never_sleep.driver import StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.heartbeat import Heartbeat  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import LowYieldBreaker, Orchestrator, ProceedToken  # noqa: E402
from agents_never_sleep.state import ContaminationScope, OutcomeState, OutcomeStore, TicketOutcome  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402
from agents_never_sleep.vcs import GitError  # noqa: E402
from agents_never_sleep.worker import DemoWorker  # noqa: E402


def _build(work, *, breaker=None, heartbeat=None):
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    tickets = load_tickets(os.path.join(HERE, "tickets"))
    store = OutcomeStore(state_dir)
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=DemoWorker(),
                        artifacts_dir=os.path.join(work, "artifacts"), unattended=True,
                        ledger=ledger, breaker=breaker, heartbeat=heartbeat)
    driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                        report_path=os.path.join(work, "report.md"))
    return repo, store, tickets, orch, driver


def test_driver_beats_heartbeat(failures):
    """The watchdog presumes the parent hung when the heartbeat goes stale. In the agent-driven
    flow nothing beats unless the driver does, so next_ticket must refresh it (else a healthy run
    gets false-restarted)."""
    work = tempfile.mkdtemp(prefix="ue-h-heartbeat-")
    hb_path = os.path.join(work, "state", "heartbeat.json")
    _, _, _, _, driver = _build(work, heartbeat=Heartbeat(hb_path))
    driver.next_ticket()
    age = Heartbeat.age_seconds(hb_path)
    if age is None:
        failures.append("[heartbeat] driver never beat the heartbeat (watchdog would false-restart)")
    elif age > 30:
        failures.append(f"[heartbeat] heartbeat stale after next_ticket: age={age}s")


def test_bug1_run_scoped_breaker(failures):
    work = tempfile.mkdtemp(prefix="ue-h-breaker-")
    # an easily-tripped breaker, and a store already full of prior-run "bad" outcomes
    repo, store, tickets, orch, driver = _build(work, breaker=LowYieldBreaker(min_tickets=2,
                                                                              bad_ratio=0.5))
    for i in range(5):
        store.write(TicketOutcome(ticket_id=f"old-parked-{i}", state=OutcomeState.PARKED_DECISION,
                                  why="prior run", contamination_scope=ContaminationScope.NONE))
    # Fresh run: despite 5 bad outcomes in the store, the first call must NOT trip LOW_YIELD.
    res = driver.next_ticket()
    if res["status"] != "PROCEED":
        failures.append(f"[BUG1] fresh run tripped early on store history: got {res['status']}")


def test_bug3_git_failure_blocked_env(failures):
    work = tempfile.mkdtemp(prefix="ue-h-git-")
    repo, store, tickets, orch, driver = _build(work)
    ticket = next(t for t in tickets if t.id == "ticket-01-trivial")

    class _SnapshotFails:
        def ensure_safety_net(self):
            return True

        def commit_all(self, msg):
            raise GitError("simulated: git hung taking the snapshot")

    orch.git = _SnapshotFails()
    outcome = orch.begin_proceed(ticket)
    if not isinstance(outcome, TicketOutcome) or outcome.state != OutcomeState.BLOCKED_ENV:
        failures.append(f"[BUG3] snapshot git-failure not mapped to BLOCKED_ENV: {outcome}")

    # revert failure during finalize must also degrade to BLOCKED_ENV, not crash
    work2 = tempfile.mkdtemp(prefix="ue-h-git2-")
    repo2, store2, tickets2, orch2, driver2 = _build(work2)
    t3 = next(t for t in tickets2 if t.id == "ticket-03-redgate")
    token = orch2.begin_proceed(t3)              # real snapshot
    DemoWorker().apply(t3, repo2)                # break the gate so finalize will try to revert
    real_git = orch2.git

    class _RevertFails:
        def __getattr__(self, name):
            if name == "revert_to":
                def _boom(ref):
                    raise GitError("simulated: git reset --hard hung")
                return _boom
            return getattr(real_git, name)

    orch2.git = _RevertFails()
    outcome2 = orch2.finalize_after_edit(t3, token, "broke add()")
    if outcome2.state != OutcomeState.BLOCKED_ENV:
        failures.append(f"[BUG3] revert git-failure not mapped to BLOCKED_ENV: {outcome2.state}")


def test_pending_recovery_preserves_committed_done(failures):
    """Crash window: finalize committed `done:A` + recorded DONE, then crashed BEFORE clearing
    pending.json. On resume, recovery must NOT revert to the pre-edit snapshot (that would erase the
    committed, recorded work and then skip the ticket as DONE — silent work loss)."""
    work = tempfile.mkdtemp(prefix="ue-h-pending-")
    repo, store, tickets, orch, driver = _build(work)
    t1 = next(t for t in tickets if t.id == "ticket-01-trivial")

    orch.git.ensure_safety_net()
    s_pre = orch.git.commit_all(f"pre:{t1.id}")          # pre-edit snapshot
    DemoWorker().apply(t1, repo)                          # the edit
    orch.git.commit_all(f"done:{t1.id}")                 # finalize committed it
    store.write(TicketOutcome(ticket_id=t1.id, state=OutcomeState.DONE, why="gates green",
                              attempts=1))                # finalize recorded DONE
    driver._save_pending(ProceedToken(ticket_id=t1.id, snapshot=s_pre,
                                      baseline_green=True, attempt_n=1))  # ...then crashed here

    driver.next_ticket()                                 # resume

    app = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    if "agents-never-sleep demo started" not in app:
        failures.append("[pending-recovery] committed DONE edit was reverted (silent work loss)")
    if store.read(t1.id).state != OutcomeState.DONE:
        failures.append(f"[pending-recovery] ticket-01 no longer DONE: {store.read(t1.id).state}")


def test_bug4_hard_fail_on_path_mismatch(failures):
    work = tempfile.mkdtemp(prefix="ue-h-cwd-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    os.makedirs(os.path.join(repo, "tickets"), exist_ok=True)
    for name in os.listdir(os.path.join(HERE, "tickets")):
        shutil.copy(os.path.join(HERE, "tickets", name), os.path.join(repo, "tickets", name))
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w") as fh:
        json.dump({"schema_version": 1, "gates": [], "budget": {},
                   "autonomy": {"non_destructive_only": False}}, fh)

    neutral = tempfile.mkdtemp(prefix="ue-h-cwd-elsewhere-")  # CWD != repo
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env.pop("UE_RUN_INCOMPLETE", None)
    env["PYTHONPATH"] = SKILL_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "harness.run", "next", "--repo", repo,
                           "--tickets", "tickets"], cwd=neutral, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 2:
        failures.append(f"[BUG4] expected hard-fail exit 2, got {proc.returncode} ({proc.stdout!r})")
    try:
        if json.loads(proc.stdout).get("status") != "ERROR":
            failures.append(f"[BUG4] expected status ERROR, got {proc.stdout!r}")
    except json.JSONDecodeError:
        failures.append(f"[BUG4] non-JSON output: {proc.stdout!r}")
    # sanity: the SAME run from inside the repo (CWD == repo) must NOT hard-fail
    proc_ok = subprocess.run([sys.executable, "-m", "harness.run", "next", "--repo", ".",
                              "--tickets", "tickets", "--state-dir", "state",
                              "--artifacts-dir", "artifacts"], cwd=repo, env=env,
                             capture_output=True, text=True)
    if proc_ok.returncode != 0:
        failures.append(f"[BUG4] run from repo root should pass, got {proc_ok.returncode} "
                        f"({proc_ok.stdout!r} {proc_ok.stderr!r})")
    # A SYMLINKED spelling of the same repo must ALSO pass: the guard compares directory
    # IDENTITY, not spelling. This is the macOS default — TMPDIR lives under /var/folders,
    # a symlink to /private/var/folders, so getcwd() (always physical) never string-equals
    # a --repo passed through the symlink even though CWD *is* the repo (2026-07-08 E2E).
    link_parent = tempfile.mkdtemp(prefix="ue-h-cwd-link-")
    link = os.path.join(link_parent, "repo-link")
    os.symlink(repo, link)
    proc_link = subprocess.run([sys.executable, "-m", "harness.run", "next", "--repo", link,
                                "--tickets", "tickets", "--state-dir", "state",
                                "--artifacts-dir", "artifacts"], cwd=repo, env=env,
                               capture_output=True, text=True)
    if proc_link.returncode != 0:
        failures.append(f"[BUG4-symlink] symlinked --repo spelling of CWD should pass, got "
                        f"{proc_link.returncode} ({proc_link.stdout!r} {proc_link.stderr!r})")


def test_halt_readonly_repo_report_degrades(failures):
    """HALT in a read-only repo root (the flagship no-safety-net case: no VCS, git init
    impossible) must still emit the terminal HALTED JSON on exit 0 — the report write DEGRADES
    (fallback under .unattended/, or a report_error note), it never becomes an unhandled
    PermissionError traceback that breaks the agent-facing JSON contract (2026-07-08 E2E)."""
    work = tempfile.mkdtemp(prefix="ue-h-ro-halt-")
    repo = os.path.join(work, "repo")
    os.makedirs(os.path.join(repo, "tickets"))
    with open(os.path.join(repo, "tickets", "h1.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nid: h1\ntitle: Tidy a comment\n---\n\nFix the typo in notes.txt.\n")
    with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("# nots\n")
    os.makedirs(os.path.join(repo, ".claude"))
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"schema_version": 1,
                   "gates": [{"name": "noop", "command": ["true"], "blocking": True}],
                   "autonomy": {"non_destructive_only": False},
                   "report": {"local_path": "night-report.md"}}, fh)
    # .unattended stays writable (state/heartbeat live there); only the repo ROOT is read-only,
    # so git init AND the night-report write both fail — exactly the reproduced scenario.
    os.makedirs(os.path.join(repo, ".unattended"))
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env.pop("UE_RUN_INCOMPLETE", None)
    env["PYTHONPATH"] = SKILL_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    os.chmod(repo, 0o555)
    try:
        proc = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                               "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                              capture_output=True, text=True)
    finally:
        os.chmod(repo, 0o755)
    if proc.returncode != 0:
        failures.append(f"[ro-halt] expected exit 0 with HALTED JSON, got {proc.returncode} "
                        f"(stderr: {proc.stderr[-300:]!r})")
        return
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        failures.append(f"[ro-halt] non-JSON output: {proc.stdout!r}")
        return
    if out.get("status") != "HALTED":
        failures.append(f"[ro-halt] expected status HALTED, got {out!r}")
    # The degraded report must be honest: either a fallback path that really exists,
    # or report_path null + an explanatory report_error.
    rp = out.get("report_path")
    if rp:
        if not os.path.exists(rp):
            failures.append(f"[ro-halt] report_path {rp!r} does not exist")
    elif not out.get("report_error"):
        failures.append(f"[ro-halt] no report_path and no report_error note: {out!r}")


def _chmod_recursive(root: str, mode: int) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.chmod(os.path.join(dirpath, name), mode)
    os.chmod(root, mode)


def _new_repo_env(repo: str) -> dict:
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env.pop("UE_RUN_INCOMPLETE", None)
    env["PYTHONPATH"] = SKILL_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_gate_command_as_string_does_not_crash(failures):
    """`gates[].command` as a natural JSON string (e.g. "bash gate.sh"), not a list, must be
    split like a shell command line — handing the whole string to subprocess.run() verbatim
    makes it treat the string as ONE argv[0] and raise FileNotFoundError, crashing `next` with a
    raw traceback instead of running the gate (2026-07-08 E2E, second session)."""
    work = tempfile.mkdtemp(prefix="ue-h-strcmd-")
    repo = os.path.join(work, "repo")
    os.makedirs(os.path.join(repo, "tickets"))
    with open(os.path.join(repo, "tickets", "s1.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nid: s1\ntitle: Trivial\n---\n\nAdd a comment to notes.txt.\n")
    with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("# notes\n")
    with open(os.path.join(repo, "gate.sh"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/bash\nexit 0\n")
    os.chmod(os.path.join(repo, "gate.sh"), 0o755)
    os.makedirs(os.path.join(repo, ".claude"))
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"schema_version": 1,
                   "gates": [{"name": "t", "command": "bash gate.sh", "blocking": True}],
                   "autonomy": {"non_destructive_only": False}}, fh)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.local"],
                ["git", "config", "user.name", "tester"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    env = _new_repo_env(repo)
    proc = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                           "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        failures.append(f"[gate-str] expected exit 0, got {proc.returncode} "
                        f"(stdout={proc.stdout!r} stderr={proc.stderr[-300:]!r})")
        return
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        failures.append(f"[gate-str] non-JSON output (traceback?): {proc.stdout!r}")
        return
    if out.get("status") != "PROCEED":
        failures.append(f"[gate-str] expected PROCEED, got {out!r}")


def test_halt_readonly_repo_fully_locked(failures):
    """Read-only repo, UNCONFIGURED bootstrap path only: no saved config, so `next` exits early at
    the NON_DESTRUCTIVE branch — this covers the _Context.__init__ crash (preflight.write_profile /
    the state-dir bootstrap died with an unhandled PermissionError before the driver ran, 2026-07-08
    E2E, second session) and nothing past it. The realistic configured-project paths (gitignore
    append, run-incomplete sentinel, state writes with the dir already existing) are covered by
    test_halt_readonly_repo_realistic_project below (2026-07-08 E2E, third session, finding 2.1)."""
    for warm in (False, True):
        work = tempfile.mkdtemp(prefix="ue-h-ro-full-")
        repo = os.path.join(work, "repo")
        os.makedirs(os.path.join(repo, "tickets"))
        with open(os.path.join(repo, "tickets", "r1.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nid: r1\ntitle: Tidy\n---\n\nFix the typo in notes.txt.\n")
        with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("# nots\n")
        env = _new_repo_env(repo)
        if warm:
            subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                            "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                           capture_output=True, text=True)
        _chmod_recursive(repo, 0o555)
        try:
            proc = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                                   "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                                  capture_output=True, text=True)
        finally:
            _chmod_recursive(repo, 0o755)
        label = "warm" if warm else "fresh"
        if proc.returncode != 0:
            failures.append(f"[ro-full-{label}] expected exit 0, got {proc.returncode} "
                            f"(stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r})")
            continue
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            failures.append(f"[ro-full-{label}] non-JSON output (traceback?): {proc.stdout!r}")


def test_halt_readonly_repo_realistic_project(failures):
    """Read-only repo, REALISTIC project: config already committed (autonomy enabled), git history
    present, at least one ticket — so `next` gets past the early NON_DESTRUCTIVE branch and into
    the driver. Before the fix it crashed with a raw PermissionError traceback (exit 1) at TWO
    sites the state-dir redirect does not cover: Git._ensure_gitignore's bare `open(gi, "a")`
    (vcs.py) and _set_sentinel's bare os.makedirs of the repo-pinned sentinel dir (driver.py)
    (2026-07-08 E2E, third session, finding 2.1).

    fresh (tree locked before any run): the gitignore append fails -> no establishable safety net
    -> classify HALTs -> clean HALTED JSON, exit 0.
    warm (one healthy run, THEN locked): .gitignore already carries the protect entry and the
    in-repo state dir EXISTS (so makedirs(exist_ok=True) alone would not trigger the redirect —
    the writability probe must) -> clean JSON, exit 0, no traceback."""
    for warm in (False, True):
        work = tempfile.mkdtemp(prefix="ue-h-ro-real-")
        repo = os.path.join(work, "repo")
        os.makedirs(os.path.join(repo, "tickets"))
        with open(os.path.join(repo, "tickets", "r1.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nid: r1\ntitle: Tidy\n---\n\nFix the typo in notes.txt.\n")
        with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("# nots\n")
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema_version": 1,
                       "gates": [{"name": "t", "command": "true", "blocking": True}],
                       "autonomy": {"non_destructive_only": False}}, fh)
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.local"],
                    ["git", "config", "user.name", "tester"], ["git", "add", "-A"],
                    ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(cmd, cwd=repo, check=True)
        env = _new_repo_env(repo)
        if warm:
            warmup = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                                     "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                                    capture_output=True, text=True)
            if warmup.returncode != 0:
                failures.append(f"[ro-real-warm] setup run failed: {warmup.stderr[-300:]!r}")
                continue
        _chmod_recursive(repo, 0o555)
        try:
            proc = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "next",
                                   "--repo", ".", "--tickets", "tickets"], cwd=repo, env=env,
                                  capture_output=True, text=True)
        finally:
            _chmod_recursive(repo, 0o755)
        label = "warm" if warm else "fresh"
        if "PermissionError" in proc.stderr or "Traceback" in proc.stderr:
            failures.append(f"[ro-real-{label}] raw traceback leaked: {proc.stderr[-400:]!r}")
            continue
        if proc.returncode != 0:
            failures.append(f"[ro-real-{label}] expected exit 0, got {proc.returncode} "
                            f"(stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r})")
            continue
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            failures.append(f"[ro-real-{label}] non-JSON output (traceback?): {proc.stdout!r}")
            continue
        if not warm and out.get("status") != "HALTED":
            failures.append(f"[ro-real-fresh] expected clean HALTED (no establishable safety "
                            f"net), got {out!r}")
        if not warm and "safety net" not in (out.get("reason") or ""):
            failures.append(f"[ro-real-fresh] HALT reason should name the missing safety net, "
                            f"got {out.get('reason')!r}")


def test_frontmatter_horizontal_rule(failures):
    from agents_never_sleep.tickets import _parse_frontmatter
    prose = "---\nThis is a release note.\n\nSome **bold** prose, no frontmatter here.\n---\ntail\n"
    meta, body = _parse_frontmatter(prose)
    if meta != {}:
        failures.append(f"[frontmatter] prose/horizontal-rule mis-parsed as meta: {meta}")
    if "This is a release note." not in body:
        failures.append("[frontmatter] body of a non-frontmatter file was truncated")
    real = "---\nid: t-1\ntitle: Do the thing\n---\nbody here\n"
    m2, b2 = _parse_frontmatter(real)
    if m2.get("id") != "t-1" or m2.get("title") != "Do the thing" or b2.strip() != "body here":
        failures.append(f"[frontmatter] real frontmatter regressed: {m2} | {b2!r}")
    # valid frontmatter with a wrapped/continuation line must NOT be discarded wholesale
    wrapped = "---\nid: t-2\ntitle: Fix bug\ndescription: a long line\n  that wraps\n---\nbody\n"
    m3, b3 = _parse_frontmatter(wrapped)
    if m3.get("id") != "t-2" or m3.get("title") != "Fix bug" or "body" not in b3:
        failures.append(f"[frontmatter] continuation line discarded valid frontmatter: {m3} | {b3!r}")


def test_ledger_partial_json(failures):
    from agents_never_sleep.ledger import AttemptLedger
    work = tempfile.mkdtemp(prefix="ue-h-ledger-")
    path = os.path.join(work, "ledger.json")
    with open(path, "w") as fh:
        json.dump({"attempts": {"t-1": 3}}, fh)        # valid JSON, missing "signatures"
    led = AttemptLedger(path)
    try:
        led.record_failure("t-1", "sig")               # must not KeyError
    except KeyError:
        failures.append("[ledger] record_failure KeyError on partial json")
    if led.attempts("t-1") != 3:
        failures.append(f"[ledger] existing attempts lost on partial json: {led.attempts('t-1')}")


def test_bare_filename_no_crash(failures):
    from agents_never_sleep.heartbeat import Heartbeat
    from agents_never_sleep.ledger import AttemptLedger
    work = tempfile.mkdtemp(prefix="ue-h-bare-")
    cwd0 = os.getcwd()
    try:
        os.chdir(work)
        Heartbeat("hb.json").beat()                    # dirname == "" must not crash
        AttemptLedger("ledger.json").record_attempt("t-1")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[bare-filename] makedirs crashed on a bare filename: {exc!r}")
    finally:
        os.chdir(cwd0)


def main() -> int:
    failures = []
    test_frontmatter_horizontal_rule(failures)
    test_ledger_partial_json(failures)
    test_bare_filename_no_crash(failures)
    test_bug1_run_scoped_breaker(failures)
    test_bug3_git_failure_blocked_env(failures)
    test_pending_recovery_preserves_committed_done(failures)
    test_driver_beats_heartbeat(failures)
    test_bug4_hard_fail_on_path_mismatch(failures)
    test_halt_readonly_repo_report_degrades(failures)
    test_gate_command_as_string_does_not_crash(failures)
    test_halt_readonly_repo_fully_locked(failures)
    test_halt_readonly_repo_realistic_project(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — hardening regressions")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — run-scoped breaker, git-failure→BLOCKED_ENV, sentinel hard-fail all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
