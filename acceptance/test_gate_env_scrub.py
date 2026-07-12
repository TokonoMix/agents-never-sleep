#!/usr/bin/env python3
"""Gate-subprocess env scrub test (post-audit hardening, L2).

A `vault:`/`env:` token-ref's whole point is keeping a credential OUT of the ambient environment;
`run.py` resolves it back into `os.environ` for its own IN-PROCESS consumers (the onboarding gate,
council). But the GATE COMMAND is the target repo's own, arbitrary test/build command — it has no
legitimate need to inherit ANS's own resolved gateway keys, and doing so widens what a careless or
compromised gate could leak (print all env vars for debugging, a test that dumps its environment on
failure, ...). `gates._run_command` now strips `redact.known_secret_env_var_names()` from the env it
hands to the spawned gate subprocess before running it.

Exit 0 = GREEN.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.redact import known_secret_env_var_names  # noqa: E402


def test_known_secret_env_vars_are_stripped(failures):
    work = tempfile.mkdtemp(prefix="ue-gate-env-")
    names = known_secret_env_var_names()
    if not names:
        failures.append("[scrub] known_secret_env_var_names() is empty — nothing to prove")
        return

    saved = {n: os.environ.get(n) for n in names}
    os.environ["HARMLESS_MARKER_FOR_TEST"] = "still-here"
    try:
        for n in names:
            os.environ[n] = "PLANTED-SECRET-VALUE-DO-NOT-LEAK"

        dump_script = "; ".join(f'echo {n}=${{{n}:-ABSENT}}' for n in names)
        gate = GateRunner(command=["sh", "-c", f"{dump_script}; echo MARKER=$HARMLESS_MARKER_FOR_TEST"],
                          cwd=work, timeout=10)
        out = gate.run_after_edit(baseline_green=True).output

        if "PLANTED-SECRET-VALUE-DO-NOT-LEAK" in out:
            failures.append(f"[scrub] a known-secret env var leaked into the gate subprocess: {out!r}")
        for n in names:
            if f"{n}=ABSENT" not in out:
                failures.append(f"[scrub] {n} was not scrubbed from the gate env: {out!r}")

        # A legitimate, unrelated env var must still pass through — this is a scoped scrub, not a
        # blanket wipe of the gate's environment.
        if "MARKER=still-here" not in out:
            failures.append(f"[scrub] an unrelated env var was wrongly stripped too: {out!r}")
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        os.environ.pop("HARMLESS_MARKER_FOR_TEST", None)


def main() -> int:
    failures = []
    test_known_secret_env_vars_are_stripped(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — gate subprocess env scrub not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — ANS's own resolved-credential env vars never reach the gate "
          "subprocess; unrelated env vars still pass through untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
