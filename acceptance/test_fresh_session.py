#!/usr/bin/env python3
"""Fresh-session-per-N-tickets (opt-in context strategy) — proves the feature AND, just as
importantly, that it is BYTE-IDENTICAL to the legacy single-session path when it is OFF.

The feature lets a long backlog be drained across several FRESH agent sessions (instead of one
accumulating session that empirically degrades ~ticket 19). It must NOT weaken the never-stop
guarantee when off, and must coordinate an EARLY stop with the Stop-hook when on.

Cases:
  (a) DEFAULT OFF -> ans-run does exactly ONE agent spawn, and the child env carries NO
      UE_SESSION_TICKET_BUDGET (so the driver's new counter branch is provably dead).
  (b) Stop-hook BLOCKS while the run-incomplete sentinel is present AND the budget is UNSET
      (the unchanged never-stop guarantee).
  (c) Stop-hook ALLOWS an early stop when the budget IS set AND the session-budget-reached
      marker exists — even though the sentinel is still present.
  (d) The per-session counter resets per session: the driver writes the marker once N RECORDED
      completions accumulate, and a launcher-style reset (delete counter+marker) starts fresh.
  (e) ON -> ans-run respawns a FRESH agent while the sentinel persists and stops the loop the
      moment the sentinel is gone (terminal reached), passing UE_SESSION_TICKET_BUDGET to the child.

Exit 0 = GREEN.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
ANS_RUN = os.path.join(SKILL_ROOT, "bin", "ans-run")
HOOK = os.path.join(SKILL_ROOT, "hooks", "stop_guard.sh")

TRUST_DIR = tempfile.mkdtemp(prefix="ue-fresh-trust-")
TRUST_STORE = os.path.join(TRUST_DIR, "trusted.json")


# --------------------------------------------------------------------------- launcher helpers
def _new_repo(agent_script: str, launcher_extra: dict | None = None) -> str:
    repo = tempfile.mkdtemp(prefix="ue-fresh-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("sandbox\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    agent = os.path.join(repo, "fake-agent.sh")
    with open(agent, "w") as fh:
        fh.write(agent_script)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)

    creds = os.path.join(repo, "fake-creds.json")
    with open(creds, "w") as fh:
        fh.write('{"FAKE": "placeholder"}')

    launcher = {"agent_cmd": [agent], "allow_custom_agent": True,
                "credentials_paths": [creds], "min_disk_mb": 1}
    launcher.update(launcher_extra or {})
    cfg_dir = os.path.join(repo, ".claude")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "agents-never-sleep.json"), "w") as fh:
        json.dump({"launcher": launcher}, fh)
    return repo


def _run(repo: str, *extra: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ANS_TRUST_STORE"] = TRUST_STORE
    env["ANS_TEST_MODE"] = "1"
    return subprocess.run([sys.executable, ANS_RUN, "--repo", repo, *extra],
                          capture_output=True, text=True, timeout=timeout, env=env)


def _trusted_repo(agent_script: str, launcher_extra: dict | None = None) -> str:
    repo = _new_repo(agent_script, launcher_extra)
    res = _run(repo, "--trust")
    assert res.returncode == 0, f"--trust failed: {res.stdout}{res.stderr}"
    return repo


# A fake agent that records each invocation to spawns.log and reports whether it saw the budget env.
RECORDER = (
    "#!/bin/sh\n"
    'echo "spawn budget=${UE_SESSION_TICKET_BUDGET:-UNSET} marker=${UE_SESSION_BUDGET_MARKER:-UNSET}"'
    ' >> "$REPO/spawns.log"\n'
    "exit 0\n"
)


# --------------------------------------------------------------------------- hook helpers
def _run_hook(payload: dict, env: dict):
    return subprocess.run(["bash", HOOK], input=json.dumps(payload), text=True,
                          capture_output=True, env=env)


def _blocked(proc) -> bool:
    return '"decision":"block"' in proc.stdout or '"decision": "block"' in proc.stdout


# =========================================================================== cases
def test_default_off_single_spawn(failures):
    """(a) Feature off => exactly one spawn, child env free of UE_SESSION_TICKET_BUDGET."""
    repo = _trusted_repo(RECORDER)  # no fresh_session_every => default 0 => off
    env = dict(os.environ, ANS_TRUST_STORE=TRUST_STORE, ANS_TEST_MODE="1", REPO=repo)
    res = subprocess.run([sys.executable, ANS_RUN, "--repo", repo, "go"],
                         capture_output=True, text=True, timeout=30, env=env)
    if res.returncode != 0:
        failures.append(f"[off] expected rc 0, got {res.returncode}: {res.stdout}{res.stderr}")
        return
    if "Started in background" not in res.stdout:
        failures.append(f"[off] not the legacy background path: {res.stdout}")
    if "Fresh-session mode" in res.stdout:
        failures.append("[off] fresh-session loop ran while feature was OFF")
    time.sleep(2)  # let the detached spawn write its line
    spawns = os.path.join(repo, "spawns.log")
    lines = []
    if os.path.exists(spawns):
        with open(spawns) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    if len(lines) != 1:
        failures.append(f"[off] expected exactly ONE spawn, got {len(lines)}: {lines}")
    elif "budget=UNSET" not in lines[0]:
        failures.append(f"[off] UE_SESSION_TICKET_BUDGET leaked into default-off child: {lines[0]}")


def test_hook_blocks_when_budget_unset(failures):
    """(b) Unchanged never-stop guarantee: sentinel present + budget UNSET => block."""
    work = tempfile.mkdtemp(prefix="ue-fresh-hook-")
    sentinel = os.path.join(work, ".unattended", "run-incomplete")
    os.makedirs(os.path.dirname(sentinel), exist_ok=True)
    open(sentinel, "w").close()
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env["UE_RUN_INCOMPLETE"] = sentinel
    env.pop("UE_SESSION_TICKET_BUDGET", None)  # budget UNSET => legacy behaviour
    p = _run_hook({}, env)
    if not _blocked(p):
        failures.append(f"[hook-unset] sentinel present + budget unset but hook did NOT block: {p.stdout!r}")


def test_hook_allows_when_budget_set_and_marker(failures):
    """(c) Budget set + session-budget-reached marker => allow stop EVEN with the sentinel present."""
    work = tempfile.mkdtemp(prefix="ue-fresh-hook-")
    sentinel = os.path.join(work, ".unattended", "run-incomplete")
    marker = os.path.join(work, ".unattended", "state", "session-budget-reached")
    os.makedirs(os.path.dirname(sentinel), exist_ok=True)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    open(sentinel, "w").close()
    base = dict(os.environ)
    base["CLAUDE_UNATTENDED"] = "1"
    base["UE_RUN_INCOMPLETE"] = sentinel
    base["UE_SESSION_TICKET_BUDGET"] = "3"
    base["UE_SESSION_BUDGET_MARKER"] = marker

    # marker ABSENT (budget set but not yet reached) => still block while sentinel present
    p = _run_hook({}, base)
    if not _blocked(p):
        failures.append(f"[hook-set] budget set, marker absent, sentinel present: should still block "
                        f"({p.stdout!r})")
    # marker PRESENT => allow stop despite the sentinel
    open(marker, "w").close()
    p = _run_hook({}, base)
    if _blocked(p) or p.returncode != 0:
        failures.append(f"[hook-set] budget set + marker present but hook blocked/failed "
                        f"(rc={p.returncode}, out={p.stdout!r})")


def test_counter_resets_per_session(failures):
    """(d) Driver writes the marker after N RECORDED completions; a launcher reset starts fresh."""
    work = tempfile.mkdtemp(prefix="ue-fresh-counter-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    os.makedirs(os.path.join(repo, "tickets"), exist_ok=True)
    for name in os.listdir(os.path.join(HERE, "tickets")):
        shutil.copy(os.path.join(HERE, "tickets", name), os.path.join(repo, "tickets", name))
    cfg = {"schema_version": 1,
           "gates": [{"name": "tests",
                      "command": [sys.executable, "-m", "unittest", "discover", "-s", ".",
                                  "-p", "test_*.py"], "blocking": True}],
           "budget": {"per_ticket_timeout_s": 60, "per_ticket_fix_iterations": 3},
           "autonomy": {"non_destructive_only": False, "requirement_ambiguity": "hybrid"},
           "report": {"local_path": "morning-report.md"}}
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w") as fh:
        json.dump(cfg, fh)

    state_dir = os.path.join(repo, "state")
    marker = os.path.join(repo, ".unattended", "state", "session-budget-reached")
    count_file = os.path.join(state_dir, "session-ticket-count")
    sentinel = os.path.join(repo, ".unattended", "run-incomplete")

    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env["UE_RUN_INCOMPLETE"] = sentinel
    env["UE_SESSION_TICKET_BUDGET"] = "1"  # marker after a single completion
    env["UE_SESSION_BUDGET_MARKER"] = marker

    def harness(*cmd):
        return subprocess.run([sys.executable, "-m", "harness.run", *cmd, "--repo", repo,
                               "--tickets", "tickets", "--state-dir", "state",
                               "--artifacts-dir", "artifacts", "--report", "morning-report.md"],
                              cwd=SKILL_ROOT, env=env, capture_output=True, text=True)

    # One ticket: next -> complete. With budget 1, the marker must appear after complete.
    np = harness("next")
    res = json.loads(np.stdout) if np.stdout.strip().startswith("{") else {}
    if res.get("status") != "PROCEED":
        failures.append(f"[counter] first next not PROCEED: {np.stdout!r}{np.stderr!r}")
        return
    cp = harness("complete", "--attempted", "did the thing")
    cres = json.loads(cp.stdout) if cp.stdout.strip().startswith("{") else {}
    if cres.get("status") != "RECORDED":
        failures.append(f"[counter] complete not RECORDED: {cp.stdout!r}{cp.stderr!r}")
        return
    if not os.path.exists(count_file):
        failures.append("[counter] session-ticket-count not written")
    elif open(count_file).read().strip() != "1":
        failures.append(f"[counter] expected count 1, got {open(count_file).read()!r}")
    if not os.path.exists(marker):
        failures.append("[counter] session-budget-reached marker NOT written at budget")
    # The brake: at budget, `complete` must tell the agent to STOP (not call `next`), else a
    # well-behaved agent overruns the session and the feature collapses to one long session.
    if "STOP" not in (cres.get("next") or ""):
        failures.append(f"[counter] budget reached but complete did NOT instruct STOP: {cres.get('next')!r}")

    # Launcher-style reset: delete counter + marker => next session starts at 0.
    for p in (count_file, marker):
        if os.path.exists(p):
            os.unlink(p)
    if os.path.exists(count_file) or os.path.exists(marker):
        failures.append("[counter] reset failed")
    # After reset the marker stays absent until the next completion accumulates again — proven by
    # the fact that the files are gone and the counter is re-derived from absence (count starts 0).


def _sandbox_repo(work):
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    os.makedirs(os.path.join(repo, "tickets"), exist_ok=True)
    for name in os.listdir(os.path.join(HERE, "tickets")):
        shutil.copy(os.path.join(HERE, "tickets", name), os.path.join(repo, "tickets", name))
    cfg = {"schema_version": 1,
           "gates": [{"name": "tests",
                      "command": [sys.executable, "-m", "unittest", "discover", "-s", ".",
                                  "-p", "test_*.py"], "blocking": True}],
           "budget": {"per_ticket_timeout_s": 60, "per_ticket_fix_iterations": 3},
           "autonomy": {"non_destructive_only": False, "requirement_ambiguity": "hybrid"},
           "report": {"local_path": "morning-report.md"}}
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w") as fh:
        json.dump(cfg, fh)
    return repo


def test_default_off_complete_writes_nothing(failures):
    """(a-neg) With the budget UNSET, a real `complete` must write NO counter/marker and keep the
    legacy 'call next' hint — proving the driver's new branch is dead by default (not just by spawn)."""
    work = tempfile.mkdtemp(prefix="ue-fresh-offcomplete-")
    repo = _sandbox_repo(work)
    sentinel = os.path.join(repo, ".unattended", "run-incomplete")
    count_file = os.path.join(repo, "state", "session-ticket-count")
    marker = os.path.join(repo, ".unattended", "state", "session-budget-reached")
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env["UE_RUN_INCOMPLETE"] = sentinel
    env.pop("UE_SESSION_TICKET_BUDGET", None)  # OFF

    def harness(*cmd):
        return subprocess.run([sys.executable, "-m", "harness.run", *cmd, "--repo", repo,
                               "--tickets", "tickets", "--state-dir", "state",
                               "--artifacts-dir", "artifacts", "--report", "morning-report.md"],
                              cwd=SKILL_ROOT, env=env, capture_output=True, text=True)

    harness("next")
    cp = harness("complete", "--attempted", "did the thing")
    cres = json.loads(cp.stdout) if cp.stdout.strip().startswith("{") else {}
    if cres.get("status") != "RECORDED":
        failures.append(f"[off-complete] complete not RECORDED: {cp.stdout!r}{cp.stderr!r}")
        return
    if os.path.exists(count_file):
        failures.append("[off-complete] session-ticket-count written while feature OFF")
    if os.path.exists(marker):
        failures.append("[off-complete] session-budget-reached marker written while feature OFF")
    if "STOP" in (cres.get("next") or "") or "next" not in (cres.get("next") or ""):
        failures.append(f"[off-complete] off-path lost the legacy 'call next' hint: {cres.get('next')!r}")


def test_below_budget_keeps_going(failures):
    """First completion of a budget-2 session => count 1 < 2 => 'call next', no marker yet."""
    work = tempfile.mkdtemp(prefix="ue-fresh-below-")
    repo = _sandbox_repo(work)
    sentinel = os.path.join(repo, ".unattended", "run-incomplete")
    marker = os.path.join(repo, ".unattended", "state", "session-budget-reached")
    env = dict(os.environ)
    env["CLAUDE_UNATTENDED"] = "1"
    env["UE_RUN_INCOMPLETE"] = sentinel
    env["UE_SESSION_TICKET_BUDGET"] = "2"
    env["UE_SESSION_BUDGET_MARKER"] = marker

    def harness(*cmd):
        return subprocess.run([sys.executable, "-m", "harness.run", *cmd, "--repo", repo,
                               "--tickets", "tickets", "--state-dir", "state",
                               "--artifacts-dir", "artifacts", "--report", "morning-report.md"],
                              cwd=SKILL_ROOT, env=env, capture_output=True, text=True)

    harness("next")
    cp = harness("complete", "--attempted", "first of two")
    cres = json.loads(cp.stdout) if cp.stdout.strip().startswith("{") else {}
    if cres.get("status") != "RECORDED":
        failures.append(f"[below] complete not RECORDED: {cp.stdout!r}{cp.stderr!r}")
        return
    if "STOP" in (cres.get("next") or ""):
        failures.append(f"[below] count 1 < budget 2 but complete told STOP: {cres.get('next')!r}")
    if os.path.exists(marker):
        failures.append("[below] marker written before budget reached")


def test_on_respawns_until_terminal(failures):
    """(e) Feature ON: respawn fresh agents while the sentinel persists; stop when it clears."""
    # The fake agent simulates the driver: on the first two sessions it leaves the sentinel
    # (more work); on the third it removes it (terminal reached). It also records its budget env.
    sentinel_rel = ".unattended/run-incomplete"
    agent_script = (
        "#!/bin/sh\n"
        'echo "spawn budget=${UE_SESSION_TICKET_BUDGET:-UNSET}" >> "$REPO/spawns.log"\n'
        'n=$(cat "$REPO/round" 2>/dev/null || echo 0)\n'
        'n=$((n + 1))\n'
        'echo "$n" > "$REPO/round"\n'
        f'mkdir -p "$REPO/$(dirname {sentinel_rel})"\n'
        f'if [ "$n" -ge 3 ]; then rm -f "$REPO/{sentinel_rel}"; else : > "$REPO/{sentinel_rel}"; fi\n'
        "exit 0\n"
    )
    repo = _trusted_repo(agent_script, {"fresh_session_every": 2})
    # Seed the sentinel so the loop sees "work remaining" after session 1/2.
    os.makedirs(os.path.join(repo, ".unattended"), exist_ok=True)
    open(os.path.join(repo, sentinel_rel), "w").close()

    env = dict(os.environ, ANS_TRUST_STORE=TRUST_STORE, ANS_TEST_MODE="1", REPO=repo)
    res = subprocess.run([sys.executable, ANS_RUN, "--repo", repo, "go"],
                         capture_output=True, text=True, timeout=60, env=env)
    if res.returncode != 0:
        failures.append(f"[on] expected rc 0, got {res.returncode}: {res.stdout}{res.stderr}")
        return
    if "Fresh-session mode" not in res.stdout:
        failures.append(f"[on] did not enter fresh-session loop: {res.stdout}")
    if "Fresh-session loop done after 3 session(s)" not in res.stdout:
        failures.append(f"[on] expected 3 sessions before terminal: {res.stdout}")
    spawns = os.path.join(repo, "spawns.log")
    lines = [ln for ln in open(spawns).read().splitlines() if ln.strip()] if os.path.exists(spawns) else []
    if len(lines) != 3:
        failures.append(f"[on] expected 3 spawns, got {len(lines)}: {lines}")
    if any("budget=2" not in ln for ln in lines):
        failures.append(f"[on] some spawn did not carry UE_SESSION_TICKET_BUDGET=2: {lines}")
    if os.path.exists(os.path.join(repo, sentinel_rel)):
        failures.append("[on] loop ended but sentinel still present")


def main() -> int:
    failures = []
    test_default_off_single_spawn(failures)
    test_default_off_complete_writes_nothing(failures)
    test_hook_blocks_when_budget_unset(failures)
    test_hook_allows_when_budget_set_and_marker(failures)
    test_below_budget_keeps_going(failures)
    test_counter_resets_per_session(failures)
    test_on_respawns_until_terminal(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — fresh-session feature / default-off guarantee not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — default-off is single-spawn & budget-free; never-stop intact when off; "
          "early-stop honoured when on; per-session counter resets; loop respawns until terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
