#!/usr/bin/env python3
"""Reap a run's OWN process subtree by PARENT-CHAIN lineage — never by name (ticket 05).

Proves the guarantee the leaked-MCP reaper rests on: descendants() finds the child tree (incl.
nested grandchildren) by ppid lineage from a KNOWN root pid; reap_tree() SIGTERMs exactly that
tree; a SAME-NAMED process OUTSIDE the tree is left untouched (the `pkill -f` foot-gun this replaces
would kill it); and the walk can never reach the root's own parent (so it can't hit the watchdog or
another project's run). Exit 0 = GREEN.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.reap import descendants, reap_tree, _ppid_of  # noqa: E402


def _state(pid: int) -> str:
    """Process state char, or '' if gone. 'Z' = zombie = effectively dead.
    /proc on Linux; `ps -o state=` where /proc does not exist (macOS/BSD) — the same
    dual-source rule the reap module itself follows."""
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
                return fh.read().rsplit(")", 1)[-1].split()[0]
        except (OSError, IndexError):
            return ""
    try:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    state = out.stdout.strip()
    return state[:1] if out.returncode == 0 and state else ""


def _alive(pid: int) -> bool:
    s = _state(pid)
    return s not in ("", "Z")


def test_descendants_and_reap_parent_chain_only(failures):
    # root sh forks a CHILD sh that forks a grandchild sleep (proves multi-level walk), plus a
    # second direct sleep; `wait` keeps root alive. A SAME-NAMED `sleep` runs OUTSIDE the tree.
    # The inner sh runs `sleep 300 & wait` (compound), NOT a bare `sleep 300`: a single simple
    # command lets sh EXEC itself into the sleep (macOS /bin/sh does), collapsing the middle
    # level — the compound form forces a real 3-node tree on every platform.
    root = subprocess.Popen(["sh", "-c", "sh -c 'sleep 300 & wait' & sleep 300 & wait"])
    outside = subprocess.Popen(["sleep", "300"])   # same command name, different lineage
    try:
        tree = []
        for _ in range(50):                        # let the shells fork their children
            tree = descendants(root.pid)
            if len(tree) >= 3:                     # child sh + grandchild sleep + direct sleep
                break
            time.sleep(0.1)
        if len(tree) < 3:
            failures.append(f"[reap] multi-level tree not captured (got {tree})")
        if outside.pid in tree:
            failures.append("[reap] OUTSIDE same-named proc is in the tree — lineage walk is wrong")
        if root.pid in tree:
            failures.append("[reap] root wrongly listed as its own descendant")
        # INVARIANT: the walk can never reach root's PARENT (this test process) → can't hit a
        # watchdog/other-project tree.
        if _ppid_of(root.pid) in tree:
            failures.append("[reap] root's parent is in the tree — walk escaped upward")

        captured = list(tree)
        reaped = reap_tree(root.pid)               # SIGTERM the tree, leaves-first
        if set(reaped) != set(captured):
            failures.append(f"[reap] reap_tree signalled {reaped}, expected {captured}")
        # every captured descendant dies; the OUTSIDE proc survives. Generous deadline so a loaded
        # box doesn't flake the SIGTERM-then-exit settle.
        deadline = time.time() + 10
        while time.time() < deadline and any(_alive(p) for p in captured):
            time.sleep(0.1)
        still = [p for p in captured if _alive(p)]
        if still:
            failures.append(f"[reap] tree not reaped: {still} still alive")
        if not _alive(outside.pid):
            failures.append("[reap] OUTSIDE proc was killed — reap must be parent-chain, not by name")
    finally:
        for p in (root, outside):
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass


def test_reap_empty_and_missing_are_safe(failures):
    # A childless / already-gone root must be a no-op, never raise.
    if reap_tree(999999999) != []:
        failures.append("[reap] reap of a non-existent pid should signal nothing")
    leaf = subprocess.Popen(["sleep", "300"])
    try:
        if reap_tree(leaf.pid) != []:              # a leaf has no descendants
            failures.append("[reap] a childless proc should have no descendants to reap")
        if not _alive(leaf.pid):
            failures.append("[reap] reaping a childless proc must not touch the proc itself")
    finally:
        leaf.terminate()
        try:
            leaf.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass


def test_reap_refuses_pgroup_and_init_roots(failures):
    # DEFENSE-IN-DEPTH (security review F1): the public reap primitive must NEVER signal pid <= 1 —
    # os.kill(0) hits the caller's process GROUP, os.kill(-n) a group, pid 1 is init. A miswired root
    # (0/1/negative from a pidfile/env) would otherwise enumerate + group-kill nearly the whole box.
    from agents_never_sleep.reap import descendants, reap_pids
    for bad in (0, 1, -1, -12345):
        if descendants(bad) != []:
            failures.append(f"[reap] descendants({bad}) must refuse (return []), not walk the box")
        if reap_tree(bad) != []:
            failures.append(f"[reap] reap_tree({bad}) must signal nothing")
    if reap_pids([0, -1, 1]) != []:
        failures.append("[reap] reap_pids must skip 0 (caller-group), negatives (group), and 1 (init)")


def main() -> int:
    failures: list = []
    test_descendants_and_reap_parent_chain_only(failures)
    test_reap_empty_and_missing_are_safe(failures)
    test_reap_refuses_pgroup_and_init_roots(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — parent-chain reaping not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — parent-chain walk captures the nested tree, reaps exactly it, "
          "spares a same-named OUTSIDE proc, and never escapes upward to the parent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
