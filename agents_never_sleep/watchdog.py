#!/usr/bin/env python3
"""Standalone heartbeat watchdog — supervises the PARENT unattended run.

Runs the unattended command as a child, polls its heartbeat file, and if the heartbeat goes stale
(parent hung — the gap the Stop-hook cannot see) it kills the child and restarts it RESUMABLE, up
to a cap, then alerts. This is a separate sidecar ON PURPOSE: it must NOT modify the shared
`claude-run` infra. To integrate with claude-run, call this wrapper from an opt-in
flag (see WATCHDOG.md) — do not rewrite the wrapper.

Usage:
  python3 -m agents_never_sleep.watchdog --heartbeat <file> --stale 900 --max-restarts 3 -- <command...>

On exhausted restarts it runs the optional --alert command (e.g. a Paperclip issue creator).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from .heartbeat import Heartbeat
from .reap import descendants, reap_pids


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the child (escalate to SIGKILL after ~5s) AND reap its own descendant tree — the
    leaked MCP servers (context7, tokonomix-mcp, npm/sh). The tree is captured BEFORE the kill:
    once the child dies its children reparent (ppid → vscode/init) and the lineage is erased, so a
    post-kill walk would find nothing. Parent-chain only (reap.descendants rooted at the child pid),
    NEVER a name match — a same-named process outside this tree is untouched (ticket 05)."""
    if proc.poll() is not None:
        return
    tree = descendants(proc.pid)  # capture while the lineage is still intact
    try:
        proc.send_signal(signal.SIGTERM)
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    if tree:  # SIGTERM the captured descendants that outlived the agent (leaves-first)
        reap_pids(list(reversed(tree)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agents_never_sleep.watchdog")
    ap.add_argument("--heartbeat", required=True)
    # CRITICAL: in the agent-driven flow the heartbeat is only beaten at next/complete boundaries;
    # between them the agent implements one ticket, which can take the full per_ticket_timeout_s
    # (default 1800s). So --stale MUST exceed that worst-case single-ticket work time + gate time,
    # or the watchdog false-restarts a healthy run mid-ticket. Default is sized for the 1800s budget
    # (1800 + gate + margin). Raise it if your per_ticket_timeout_s is higher.
    ap.add_argument("--stale", type=int, default=2400, help="seconds of no heartbeat = hung "
                    "(must exceed per_ticket_timeout_s + gate time)")
    ap.add_argument("--poll", type=int, default=30)
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--alert", default="", help="shell command to run when restarts are exhausted")
    ap.add_argument("--alert-timeout", type=int, default=30,
                    help="hard timeout for the --alert command so giving up can't itself hang")
    ap.add_argument("--grace", type=int, default=300, help="startup grace before staleness counts")
    ap.add_argument("--early-stale", type=int, default=1200,
                    help="seconds after grace within which the child MUST produce its first "
                         "heartbeat (time-to-first-beat); 0 disables the check. Catches a "
                         "startup stall (e.g. a process-check loop) before the full --stale "
                         "window (typically 3h) would fire. Safe: a healthy child beats within "
                         "seconds of boot; only a child that never called next() triggers this.")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        print("watchdog: no command given", file=sys.stderr)
        return 2

    # Graceful-stop reaping: if an OPERATOR stops the run with SIGTERM/SIGINT, reap the current
    # child's leaked MCP tree before exiting (a SIGKILL'd watchdog cannot self-reap — that residual
    # leak is not closable in-process, see reap.py). `_current` holds the live child for the handler.
    _current = {"proc": None}

    def _on_signal(signum, _frame):
        p = _current["proc"]
        if p is not None:
            _terminate(p)  # captures + reaps the child tree
        raise SystemExit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported — degrade to no graceful reap

    restarts = 0
    # Poll the CHILD frequently (not on the slow --poll cadence) so an instant crash surfaces
    # fast: ans-run's post-spawn early-exit probe (a ~2s poll of THIS process) can then report
    # a dead agent instead of a false "Started in background". Heartbeat STALENESS is still
    # evaluated only on the --poll cadence (a cheap file read, no need to do it every second).
    child_poll = min(1, args.poll) if args.poll and args.poll > 0 else 1
    while True:
        env = dict(os.environ, CLAUDE_UNATTENDED="1", UE_HEARTBEAT=args.heartbeat)
        proc = subprocess.Popen(cmd, env=env)
        _current["proc"] = proc
        started = time.time()
        last_stale_check = started
        # Rolling snapshot of the child's descendant tree, refreshed on the --poll cadence. On a
        # SPONTANEOUS crash the child's children reparent the instant it exits (lineage erased), so
        # a post-exit walk finds nothing — the last snapshot (≤ --poll old, pids still valid) is what
        # lets us reap a crashed agent's orphaned MCP servers.
        last_tree: list = []
        last_snap = 0.0
        early_stale_checked = False  # fires at most once per child lifetime
        while True:
            rc = proc.poll()
            if rc is not None:
                # Child finished on its own — return its rc (ANY exit code). The watchdog restarts
                # a HANG (stale heartbeat, below), NOT a clean exit; a genuine crash (rc != 0)
                # surfaces here. Reap the pre-exit snapshot: the agent's MCP children have just
                # reparented and would otherwise leak.
                if last_tree:
                    reap_pids(list(reversed(last_tree)))
                return rc
            now = time.time()
            if now - last_snap >= args.poll:
                last_snap = now
                last_tree = descendants(proc.pid)
            # Direction 3 (INT-2674): time-to-first-beat early-stale check.
            # A healthy child calls next() within seconds of boot and writes its first heartbeat
            # immediately. A startup-stalled child (e.g. stuck in a process-check loop or waiting
            # on user input) never beats at all, so the heartbeat file either doesn't exist or still
            # holds the PREVIOUS run's timestamp (predates `started`). We detect this by checking
            # whether the last-beat age exceeds the child's own lifetime: age > (now - started) ↔
            # beat_ts < started ↔ this child has not yet written a single beat.
            # The check fires once, after grace + early_stale seconds, so a slow boot (e.g. a long
            # preflight probe) still has the full grace window before we conclude it is stuck.
            if (args.early_stale > 0 and not early_stale_checked
                    and now - started >= args.grace + args.early_stale):
                early_stale_checked = True
                age = Heartbeat.age_seconds(args.heartbeat)
                child_age = now - started
                if age is None or age > child_age:
                    shown = "no heartbeat" if age is None else f"{age:.0f}s"
                    print(f"watchdog: startup stall — no heartbeat from child within "
                          f"{args.early_stale}s after grace ({shown} > {child_age:.0f}s) "
                          f"— restarting early", file=sys.stderr)
                    _terminate(proc)
                    break
            if now - started >= args.grace and now - last_stale_check >= args.poll:
                last_stale_check = now
                age = Heartbeat.age_seconds(args.heartbeat)
                if age is None or age > args.stale:
                    shown = "no heartbeat" if age is None else f"{age:.0f}s"
                    print(f"watchdog: heartbeat stale ({shown} > {args.stale}s) — restarting child",
                          file=sys.stderr)
                    _terminate(proc)
                    break
            time.sleep(child_poll)
        restarts += 1
        if restarts > args.max_restarts:
            print(f"watchdog: exhausted {args.max_restarts} restarts — alerting", file=sys.stderr)
            if args.alert:
                # --alert is an OPERATOR-supplied command (CLI/config), never derived from ticket
                # content, so shell=True is intentional here to allow pipes/redirects in the alert.
                try:
                    subprocess.run(args.alert, shell=True, timeout=args.alert_timeout)  # noqa: S602
                except subprocess.TimeoutExpired:
                    print(f"watchdog: --alert timed out after {args.alert_timeout}s",
                          file=sys.stderr)
            return 75  # match claude-run's "overload/giving-up" exit convention


if __name__ == "__main__":
    raise SystemExit(main())
