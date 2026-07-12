"""Reap a run's OWN process subtree by PARENT-CHAIN lineage — never by name (ticket 05).

The leak (observed 2026-06-17): a killed/crashed ANS agent (`claude -p`) leaves its MCP-server
children (context7, tokonomix-mcp, npm/sh wrappers) alive — the parent vscode-server keeps them
(ppid != 1, so they don't look orphaned) and nobody reaps them → 48 claude binaries (~11 GB) +
145 context7 layers (~2.3 GB) ≈ 13 GB, swap full → OOM risk for tokonomix.service + postgres.

Why ppid-walk and NOT a name match or pgid:
  * `pkill -f claude` self-matches the reaping command AND kills OTHER users' / other-projects'
    runs — a known foot-gun (see feedback-ans-prompt-no-pkill-self-kill). NEVER do that.
  * pgid is INCOMPLETE here: a stdio MCP server that setsid()s keeps `ppid = agent` but gets its
    OWN pgid, so killpg(agent_pgid) would MISS it. A ppid-walk from the agent pid captures it.
    pgid would also risk taking out the watchdog (it is the agent's PARENT, so a ppid-walk rooted
    at the agent pid can never reach it — the invariant that keeps other trees safe).

INVARIANT (the security guard): always root a reap at the AGENT pid, never higher. descendants()
cannot reach the watchdog (parent) or another project's run (different lineage) — only the agent's
own children. A same-named process outside this tree is untouched.

Coverage honesty: an in-process reaper covers the RESTART path (watchdog _terminate) and a GRACEFUL
watchdog SIGTERM (signal handler). A SIGKILL'd watchdog cannot self-reap, so the kill-9+resume leak
is reduced, not eliminated.

Process-table source: /proc on Linux; where /proc does not exist (macOS/BSD) the same pid→ppid
table comes from ONE `ps -axo pid=,ppid=` snapshot. Both sources feed the identical lineage walk —
still parent-chain only, never a name match. (Before 2026-07-08 the module was /proc-only and
silently no-opped on Darwin.)
"""
from __future__ import annotations

import os
import signal
import subprocess
import time


def _ppid_of(pid: int):
    """Parent pid, or None if the proc is gone/unreadable. /proc on Linux, `ps` elsewhere."""
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def _all_pids() -> list:
    try:
        return [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []


def _pid_ppid_snapshot() -> dict:
    """One pid→ppid snapshot of the live process table. /proc where it exists; else a single
    `ps -axo pid=,ppid=` call (macOS/BSD). Pure lineage data — no names are read at all."""
    if os.path.isdir("/proc"):
        table: dict = {}
        for pid in _all_pids():
            pp = _ppid_of(pid)
            if pp is not None:
                table[pid] = pp
        return table
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    table = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                table[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return table


def descendants(root_pid: int) -> list:
    """All live descendants of root_pid (root itself EXCLUDED), by ppid lineage. A process-table
    snapshot at call time (/proc, or `ps` where /proc is absent). NEVER matches by name — pure
    parent-chain, so it can only reach root_pid's own tree.

    Capture this BEFORE killing root: once root dies its children reparent (ppid → init/vscode) and
    the lineage is erased, so a post-kill walk would find nothing to reap."""
    if root_pid <= 1:
        # Refuse init (1), the caller's process group (0), and negatives: walking from these would
        # enumerate nearly the whole box (other users included). A real agent root is a Popen pid ≥2.
        return []
    children: dict = {}
    for pid, pp in _pid_ppid_snapshot().items():
        children.setdefault(pp, []).append(pid)
    out, seen = [], set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def reap_pids(pids, *, sig=signal.SIGTERM) -> list:
    """Signal an explicit, pre-captured pid list (deepest-first order is the caller's job). Returns
    the pids actually signalled. Best-effort: a vanished pid (ProcessLookupError) or one we may not
    signal (PermissionError — e.g. another user's proc) is skipped, never raised.

    NEVER signals pid <= 1: os.kill(0, sig) hits the CALLER's whole process group, os.kill(-n, sig) a
    process group, and pid 1 is init. descendants() already excludes these, but reap_pids is a public
    primitive — this guard keeps a future miswire (a root from a pidfile/env resolving to 0/1) from a
    catastrophic group-kill. PID-reuse in the capture→signal window is possible; it is sub-second for
    _terminate but up to --poll for the rolling-snapshot path, and a cross-uid recycle is additionally
    EPERM-shielded (we can't signal another user's pid)."""
    signalled = []
    for pid in pids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 1:
            continue
        try:
            os.kill(pid, sig)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return signalled


def reap_tree(root_pid: int, *, include_root: bool = False, sig=signal.SIGTERM,
              grace_s: float = 0.0) -> list:
    """SIGTERM every descendant of root_pid, leaves-first (a captured child is signalled before its
    parent so a parent can't re-fork it), optionally root too. Rooted at root_pid, so ONLY that
    tree is touched. Returns the pids signalled."""
    tree = descendants(root_pid)
    order = list(reversed(tree))  # BFS emits parents before children → reversed ≈ leaves first
    if include_root:
        order.append(root_pid)
    signalled = reap_pids(order, sig=sig)
    if grace_s and signalled:
        time.sleep(grace_s)
    return signalled
