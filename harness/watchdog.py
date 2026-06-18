#!/usr/bin/env python3
"""Standalone heartbeat watchdog — supervises the PARENT unattended run.

Runs the unattended command as a child, polls its heartbeat file, and if the heartbeat goes stale
(parent hung — the gap the Stop-hook cannot see) it kills the child and restarts it RESUMABLE, up
to a cap, then alerts. This is a separate sidecar ON PURPOSE: it must NOT modify the shared
`claude-run` infra. To integrate with claude-run, call this wrapper from an opt-in
flag (see WATCHDOG.md) — do not rewrite the wrapper.

Usage:
  python3 -m harness.watchdog --heartbeat <file> --stale 900 --max-restarts 3 -- <command...>

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


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        for _ in range(20):
            if proc.poll() is not None:
                return
            time.sleep(0.25)
        proc.kill()
    except ProcessLookupError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.watchdog")
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
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        print("watchdog: no command given", file=sys.stderr)
        return 2

    restarts = 0
    while True:
        env = dict(os.environ, CLAUDE_UNATTENDED="1", UE_HEARTBEAT=args.heartbeat)
        proc = subprocess.Popen(cmd, env=env)
        started = time.time()
        while True:
            time.sleep(args.poll)
            rc = proc.poll()
            if rc is not None:
                return rc  # child finished on its own — done
            if time.time() - started < args.grace:
                continue
            age = Heartbeat.age_seconds(args.heartbeat)
            if age is None or age > args.stale:
                shown = "no heartbeat" if age is None else f"{age:.0f}s"
                print(f"watchdog: heartbeat stale ({shown} > {args.stale}s) — restarting child",
                      file=sys.stderr)
                _terminate(proc)
                break
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
