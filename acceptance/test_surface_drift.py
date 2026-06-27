#!/usr/bin/env python3
"""Surface-drift guard — keeps the SemVer-Stable public surface (SEMVER.md §2) honest.

These assertions encode the FROZEN 1.0 surface and check the code conforms. They fail on
drift in EITHER direction:
  - code grows a Stable surface not recorded in SEMVER.md (an undocumented commitment), or
  - SEMVER.md names a Stable item the code dropped (a broken promise).

If a change here is intentional, it is a SemVer event: update SEMVER.md §2 + the CHANGELOG in
the same commit, then update the frozen sets below. That coupling is the point.

Covers the load-bearing, machine-checkable Stable surfaces: the loop CLI subcommands, the core
Stable flags on next/complete, and the seven outcome states a consumer interprets. Exit 0 = GREEN.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep.run import build_parser            # noqa: E402
from agents_never_sleep.state import OutcomeState          # noqa: E402

# --- the frozen 1.0 Stable surface (mirror of SEMVER.md §2) ------------------------------

FROZEN_SUBCOMMANDS = {"next", "complete", "report", "reset-attempts", "reset-spend", "parked"}

# Core Stable flags per subcommand (SEMVER §2.1). Experimental flags (council/specialist on
# `complete`) are deliberately NOT frozen here — they may change in a MINOR before stabilizing.
FROZEN_CORE_FLAGS = {
    "next": {"--repo", "--tickets"},
    "complete": {"--attempted", "--cannot-implement"},
}

FROZEN_OUTCOME_STATES = {
    "DONE", "DONE_LOW_CONFIDENCE", "PARKED_DECISION", "PARKED_FOUNDATIONAL",
    "BLOCKED_ENV", "FAILED_RETRYABLE", "FAILED_BUG_IN_AGENT",
}


def _subparsers(ap):
    for action in ap._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _flags(subparser):
    flags = set()
    for action in subparser._actions:
        flags.update(action.option_strings)
    return flags


def test_subcommands_match(failures):
    cmds = set(_subparsers(build_parser()).keys())
    if cmds != FROZEN_SUBCOMMANDS:
        extra = cmds - FROZEN_SUBCOMMANDS
        missing = FROZEN_SUBCOMMANDS - cmds
        failures.append(f"[cli] subcommand drift — undocumented:{sorted(extra)} missing:{sorted(missing)}")


def test_core_flags_present(failures):
    subs = _subparsers(build_parser())
    for cmd, expected in FROZEN_CORE_FLAGS.items():
        if cmd not in subs:
            failures.append(f"[cli] frozen subcommand '{cmd}' missing")
            continue
        have = _flags(subs[cmd])
        missing = expected - have
        if missing:
            failures.append(f"[cli] '{cmd}' lost Stable flag(s): {sorted(missing)}")


def test_outcome_states_match(failures):
    states = {s.value for s in OutcomeState}
    if states != FROZEN_OUTCOME_STATES:
        extra = states - FROZEN_OUTCOME_STATES
        missing = FROZEN_OUTCOME_STATES - states
        failures.append(
            f"[states] outcome-state drift — undocumented:{sorted(extra)} missing:{sorted(missing)} "
            "(adding a state is a MINOR — record it in SEMVER.md §2.5 + CHANGELOG)")


def main():
    failures: list[str] = []
    test_subcommands_match(failures)
    test_core_flags_present(failures)
    test_outcome_states_match(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — public-surface drift vs SEMVER.md §2")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — Stable surface (subcommands, core flags, 7 outcome states) "
          "matches the frozen SEMVER.md §2 contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
