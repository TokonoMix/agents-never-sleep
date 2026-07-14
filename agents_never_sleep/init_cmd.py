"""ans init — first-touch project onboarding (launch-time-only; never re-invoked mid-run).

Gets a fresh repo to 'a run can start': detect the harness, scaffold a safe project config +
inert example tickets, and hand OFF (never silently perform) enforcement-hook wiring. It writes
only inside <repo>; it never touches host/global config (~/.claude/settings.json etc.).
"""
from __future__ import annotations

import argparse
import os

from . import agent_clis, capabilities


# Maturity preference for the --yes multi-CLI pick: live-verified first, then ALLOWLIST order.
# NB: the agent-CLI name IS the capabilities key ("claude"/"codex"/"gemini"/"copilot") — never
# map to "claude-code" here (see Global Constraints > Capabilities platform key).
def _maturity_rank(name: str) -> int:
    verified = name in capabilities.LIVE_VERIFIED
    return (0 if verified else 1, agent_clis.ALLOWLIST.index(name))


def select_harness(installed, *, assume_yes, ask=input, out=print):
    if not installed:
        return None
    if len(installed) == 1:
        return installed[0]
    ranked = sorted(installed, key=_maturity_rank)
    if assume_yes:
        out(f"Multiple agent CLIs found ({', '.join(installed)}); "
            f"--yes selects the highest-enforcement-maturity one: {ranked[0]}.")
        return ranked[0]
    # Show each candidate WITH its enforcement maturity so the choice is informed (spec decision #2).
    out("Multiple agent CLIs found — choose which runs this backlog:")
    for i, name in enumerate(ranked, 1):
        out(f"  {i}) {name}  —  {capabilities.status_line(name)}")
    try:
        raw = ask(f"Which? [1-{len(ranked)}, default 1] ").strip()
    except EOFError:
        # Non-TTY / closed stdin: never hang — fall back to the highest-maturity default.
        out("(no input available — defaulting to the highest-maturity CLI)")
        return ranked[0]
    if not raw:
        return ranked[0]
    try:
        idx = int(raw)
    except ValueError:
        return ranked[0]
    return ranked[idx - 1] if 1 <= idx <= len(ranked) else ranked[0]


def maturity_lines(platform):
    lines = [capabilities.status_line(platform)]
    for note in capabilities.report_notes(platform):
        lines.append(f"  ⚠️  {note}")
    return lines


def run_init(argv: list[str]) -> int:
    raise NotImplementedError  # implemented in Task 2/3/4/5


def run_install_hooks(argv: list[str]) -> int:
    raise NotImplementedError  # implemented in Task 5
