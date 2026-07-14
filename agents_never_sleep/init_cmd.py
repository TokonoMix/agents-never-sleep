"""ans init — first-touch project onboarding (launch-time-only; never re-invoked mid-run).

Gets a fresh repo to 'a run can start': detect the harness, scaffold a safe project config +
inert example tickets, and hand OFF (never silently perform) enforcement-hook wiring. It writes
only inside <repo>; it never touches host/global config (~/.claude/settings.json etc.).
"""
from __future__ import annotations

import argparse
import os


def run_init(argv: list[str]) -> int:
    raise NotImplementedError  # implemented in Task 2/3/4/5


def run_install_hooks(argv: list[str]) -> int:
    raise NotImplementedError  # implemented in Task 5
