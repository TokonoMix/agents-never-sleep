"""Deterministic gate runner — the quality BACKBONE.

The council insisted gates need a FAIL-TAXONOMY from day one, or "tests as hard-block" silently
turns into "always blocked by repo noise" (flaky/pre-existing/env failures masquerading as
"the ticket failed"). We classify by comparing the gate result on the BASELINE (before the
edit) with the result AFTER the edit:

  baseline GREEN, post RED  -> FAIL_INTRODUCED_BY_DIFF  (hard-block: revert + park/fail)
  baseline RED,   post RED  -> FAIL_PREEXISTING         (downgrade confidence, continue/park)
  timeout / cannot run      -> FAIL_ENV / BLOCKED        (env problem, not the diff)
  both GREEN                -> PASS

Every gate runs with a per-STEP timeout (a heartbeat is blind to a single hung command) and a
NON-INTERACTIVE environment so it can never block waiting on a TTY prompt overnight.
"""
from __future__ import annotations

import dataclasses
import enum
import os
import shlex
import subprocess

from .redact import known_secret_env_var_names


class GateResult(str, enum.Enum):
    PASS = "PASS"
    FAIL_INTRODUCED_BY_DIFF = "FAIL_INTRODUCED_BY_DIFF"
    FAIL_PREEXISTING = "FAIL_PREEXISTING"
    FAIL_ENV = "FAIL_ENV"


@dataclasses.dataclass
class GateRun:
    result: GateResult
    returncode: int
    output: str
    timed_out: bool = False


# Non-interactive environment: refuse to wait on prompts (git, npm, apt, ssh, etc.).
# PYTHONDONTWRITEBYTECODE avoids stale __pycache__ making a reverted-then-rebroken module
# look green on a resume (mtimes can collide within a second).
_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
    "PIP_NO_INPUT": "1",
    "CI": "1",
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _run_command(command: list[str], cwd: str, timeout: int) -> tuple[int, str, bool]:
    env = dict(os.environ)
    # L2 (post-audit): a `vault:`/`env:` token-ref's whole point is keeping the key OUT of the
    # environment; `run.py` resolves it back into os.environ for its own in-process consumers
    # (onboarding gate, council), but the GATE COMMAND is the target repo's own (arbitrary,
    # untrusted) test/build command — it has no legitimate need to inherit ANS's own gateway keys,
    # and widening what it can see only widens what a compromised/careless gate could leak. Strip
    # ANS's own resolved-credential env vars here, at the one place they'd otherwise leak into a
    # spawned subprocess; resolved-but-unknown-named secrets stay covered by redact.py at the
    # output boundary regardless.
    for name in known_secret_env_var_names():
        env.pop(name, None)
    env.update(_NONINTERACTIVE_ENV)
    try:
        proc = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
        return proc.returncode, (proc.stdout + proc.stderr), False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\n[gate timed out after {timeout}s]", True


class GateRunner:
    def __init__(self, command: list[str] | str, cwd: str, timeout: int = 120):
        # A config-authored gate command is natural JSON as a single string (e.g. "bash gate.sh");
        # subprocess.run needs argv, so split it exactly like launcher.py's agent-cmd normalization
        # (_as_argv) does — a bare string handed straight through makes subprocess treat the whole
        # line as one executable name and raise FileNotFoundError (2026-07-08 E2E, second session).
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        self.cwd = cwd
        self.timeout = timeout

    def _green(self, cwd: str) -> tuple[bool, int, str, bool]:
        rc, out, timed = _run_command(self.command, cwd, self.timeout)
        return rc == 0, rc, out, timed

    def baseline(self, cwd: str) -> bool:
        """Run the gate on the clean tree to learn whether it's green to begin with."""
        green, _, _, timed = self._green(cwd)
        if timed:
            return False
        return green

    def run_after_edit(self, baseline_green: bool) -> GateRun:
        green, rc, out, timed = self._green(self.cwd)
        if timed:
            return GateRun(GateResult.FAIL_ENV, rc, out, timed_out=True)
        if green:
            return GateRun(GateResult.PASS, rc, out)
        # post is RED -> classify by baseline
        if baseline_green:
            return GateRun(GateResult.FAIL_INTRODUCED_BY_DIFF, rc, out)
        return GateRun(GateResult.FAIL_PREEXISTING, rc, out)
