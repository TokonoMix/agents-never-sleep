#!/usr/bin/env python3
"""Stop-hook acceptance test — proves the NEVER-STOP GUARANTEE, not just the sentinel file.

`test_agent_bridge.py` proves the driver's half: it sets `.unattended/run-incomplete` while work
remains and clears it at DRAINED. But the guarantee "the agent cannot soft-halt at 2am" only holds
if the Stop-hook actually BLOCKS the stop while that file exists. That is the hook's half, and it
lives in a separate shell process the harness invokes — so it is tested here directly.

It also pins the path coupling that would otherwise break the guarantee silently: the hook checks
`${UE_RUN_INCOMPLETE:-$PWD/.unattended/run-incomplete}` and the driver writes the same env-or-repo
path. The end-to-end case below drives one real `next` and then invokes the hook with the SAME
environment, asserting the hook blocks on the very file the driver just wrote.

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
HOOK = os.path.join(SKILL_ROOT, "hooks", "stop_guard.sh")


def _run_hook(payload: dict, env: dict):
    return subprocess.run(["bash", HOOK], input=json.dumps(payload), text=True,
                          capture_output=True, env=env)


def _base_env(sentinel_path: str, unattended: bool = True) -> dict:
    env = dict(os.environ)
    env["UE_RUN_INCOMPLETE"] = sentinel_path        # pin the path so the test is CWD-independent
    if unattended:
        env["CLAUDE_UNATTENDED"] = "1"
    else:
        env.pop("CLAUDE_UNATTENDED", None)
    return env


def main() -> int:
    work = tempfile.mkdtemp(prefix="ue-hook-")
    sentinel = os.path.join(work, ".unattended", "run-incomplete")
    os.makedirs(os.path.dirname(sentinel), exist_ok=True)
    failures = []

    def blocked(proc) -> bool:
        return '"decision":"block"' in proc.stdout or '"decision": "block"' in proc.stdout

    # 1. sentinel present + unattended -> BLOCK the stop (the core never-stop guarantee)
    open(sentinel, "w").close()
    p = _run_hook({}, _base_env(sentinel))
    if not blocked(p):
        failures.append(f"sentinel present but hook did NOT block (out={p.stdout!r} err={p.stderr!r})")

    # 2. sentinel present BUT stop_hook_active -> must NOT block (anti-infinite-loop path)
    p = _run_hook({"stop_hook_active": True}, _base_env(sentinel))
    if blocked(p):
        failures.append("stop_hook_active set but hook still blocked — would infinite-loop")

    # 3. sentinel absent -> allow the stop (backlog drained)
    os.unlink(sentinel)
    p = _run_hook({}, _base_env(sentinel))
    if blocked(p) or p.returncode != 0:
        failures.append(f"sentinel absent but hook blocked/failed (rc={p.returncode}, out={p.stdout!r})")

    # 4. not unattended -> hook is inert even with a sentinel present
    open(sentinel, "w").close()
    p = _run_hook({}, _base_env(sentinel, unattended=False))
    if blocked(p) or p.returncode != 0:
        failures.append("hook acted outside an unattended run (should be inert)")

    # 5. END-TO-END: the driver writes the file the hook blocks on. Drive one real `next`, then
    #    invoke the hook with the same env and assert it blocks on the driver's own sentinel.
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

    e2e_sentinel = os.path.join(repo, ".unattended", "run-incomplete")
    env = _base_env(e2e_sentinel)
    np = subprocess.run([sys.executable, "-m", "harness.run", "next", "--repo", repo,
                         "--tickets", "tickets", "--state-dir", "state",
                         "--artifacts-dir", "artifacts", "--report", "morning-report.md"],
                        cwd=SKILL_ROOT, env=env, capture_output=True, text=True)
    res = json.loads(np.stdout) if np.stdout.strip().startswith("{") else {}
    if res.get("status") != "PROCEED":
        failures.append(f"e2e: expected first next to PROCEED, got {res.get('status')} ({np.stdout!r})")
    if not os.path.exists(e2e_sentinel):
        failures.append("e2e: driver did not create the sentinel the hook will check")
    else:
        p = _run_hook({}, env)
        if not blocked(p):
            failures.append("e2e: hook did NOT block on the sentinel the driver just wrote "
                            "(path coupling broken)")

    print("=" * 60)
    print(f"workdir: {work}")
    if failures:
        print("RESULT: ❌ RED — never-stop guarantee not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — Stop-hook blocks while sentinel exists, honours anti-loop + inert; "
          "driver/hook paths agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
