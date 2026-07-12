#!/usr/bin/env python3
"""deny-irreversible hook acceptance test — proves the guarantee END TO END through the real bash
process Claude invokes, not just the Python decision core (that's `test_enforcement.py`'s job).

`hooks/deny_irreversible.sh` used to keep its own bash case/glob copy of the irreversible pattern
list; it now delegates to `agents_never_sleep.enforcement` via the shared cross-platform dispatcher
(hooks/enforce.sh's `python3 -m agents_never_sleep.enforce claude pre_tool`). This test pins that the
delegation is wired correctly (right JSON shape, right env-gating, fails open on a broken payload) and
that the regex core denies STANDARD-FORM bypass shapes a naive bash `case`/glob never could: bundled
short flags (`-qf`), long-form flags (`--recursive --force`), separated flags (`-r -f`), and
force-via-`+`refspec.

Exit 0 = GREEN.
"""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
HOOK = os.path.join(SKILL_ROOT, "hooks", "deny_irreversible.sh")


def _run_hook(command: str, unattended: bool = True):
    env = dict(os.environ)
    if unattended:
        env["CLAUDE_UNATTENDED"] = "1"
    else:
        env.pop("CLAUDE_UNATTENDED", None)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(["bash", HOOK], input=payload, text=True,
                          capture_output=True, env=env)


def _denied(proc) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    hso = out.get("hookSpecificOutput", {})
    return (hso.get("permissionDecision") == "deny"
            and hso.get("hookEventName") == "PreToolUse")


def main() -> int:
    failures = []

    deny_cases = [
        "git push --force origin main",
        "rm -rf /",
        "vault kv delete secret/x",
        # standard-form bypass shapes a bash case/glob copy could not catch (H1):
        "git push -qf origin main",       # bundled short flags (quiet + force)
        "git push origin +main",          # force via + refspec, no --force/-f token at all
        "rm -r -f /",                     # separated short flags
        "rm --recursive --force /",       # long-form flags
        "rm -fr /etc",                    # reordered bundled flags
    ]
    for cmd in deny_cases:
        p = _run_hook(cmd)
        if not _denied(p):
            failures.append(f"{cmd!r} was NOT denied (out={p.stdout!r} err={p.stderr!r})")

    allow_cases = [
        "rm -rf ./build",       # local relative path
        "rm -rf node_modules",  # local
        "git reset --hard HEAD",  # the harness's own revert
        "git clean -fd",
        "git push origin main",   # normal push
        "ls -la",
    ]
    for cmd in allow_cases:
        p = _run_hook(cmd)
        if _denied(p) or p.returncode != 0:
            failures.append(f"{cmd!r} should ALLOW but got out={p.stdout!r} rc={p.returncode}")

    # Inert outside an unattended run.
    p = _run_hook("git push --force origin main", unattended=False)
    if _denied(p) or p.returncode != 0:
        failures.append(f"hook acted outside an unattended run (should be inert) (out={p.stdout!r})")

    # Malformed payload must fail OPEN, never crash/wedge.
    bad = subprocess.run(["bash", HOOK], input="not json at all", text=True,
                         capture_output=True, env={**os.environ, "CLAUDE_UNATTENDED": "1"})
    if _denied(bad) or bad.returncode != 0:
        failures.append(f"malformed payload did not fail open (rc={bad.returncode}, out={bad.stdout!r})")

    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — deny-irreversible hook not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — deny_irreversible.sh denies the irreversible set end to end "
          "(incl. bundled/long-form/separated-flag and +refspec bypass shapes), allows local/benign "
          "commands and the harness's own revert, inert outside unattended runs, fails open on bad input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
