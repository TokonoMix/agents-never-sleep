#!/usr/bin/env python3
"""never-ASK hook acceptance test — proves ASK is STRUCTURALLY forbidden during an unattended run.

The autonomy contract has three responses to uncertainty (PROCEED / PARK / HALT) and a fourth that
is forbidden unattended: ASK. `stop_guard.sh` already makes never-stop structural; this test pins
the matching guarantee for never-ASK: `deny_ask.sh` DENIES the AskUserQuestion tool whenever
CLAUDE_UNATTENDED=1, and is completely inert otherwise so interactive sessions can still ask.

The hook lives in a separate shell process the harness invokes via a PreToolUse matcher, so it is
tested here directly against the real PreToolUse JSON shape.

Exit 0 = GREEN.
"""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
HOOK = os.path.join(SKILL_ROOT, "hooks", "deny_ask.sh")


def _run_hook(payload: dict, unattended: bool = True):
    env = dict(os.environ)
    if unattended:
        env["CLAUDE_UNATTENDED"] = "1"
    else:
        env.pop("CLAUDE_UNATTENDED", None)
    return subprocess.run(["bash", HOOK], input=json.dumps(payload), text=True,
                          capture_output=True, env=env)


def _denied(proc) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    hso = out.get("hookSpecificOutput", {})
    # Pin the FULL PreToolUse deny contract: a wrong/missing hookEventName is a silent no-op at
    # runtime, so the test must prove the shape, not just the decision.
    return (hso.get("permissionDecision") == "deny"
            and hso.get("hookEventName") == "PreToolUse")


def main() -> int:
    failures = []

    # 1. AskUserQuestion + unattended -> DENY (the core never-ASK guarantee)
    p = _run_hook({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}})
    if not _denied(p):
        failures.append(f"AskUserQuestion unattended was NOT denied (out={p.stdout!r} err={p.stderr!r})")
    else:
        reason = json.loads(p.stdout)["hookSpecificOutput"].get("permissionDecisionReason", "")
        # The deny reason must re-route to PARK/PROCEED, not leave the agent stuck.
        if not ("PARK" in reason and "PROCEED" in reason):
            failures.append(f"deny reason does not steer to PARK/PROCEED: {reason!r}")

    # 2. AskUserQuestion but NOT unattended -> inert (interactive sessions can ask freely)
    p = _run_hook({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}, unattended=False)
    if _denied(p) or p.returncode != 0:
        failures.append(f"hook acted outside an unattended run (should be inert) (out={p.stdout!r})")

    # 3. A different tool + unattended -> allowed (never wedge unrelated tools)
    p = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    if _denied(p) or p.returncode != 0:
        failures.append(f"non-Ask tool was blocked by the never-ASK hook (out={p.stdout!r})")

    # 4. Malformed payload + unattended -> fail OPEN (allow), never crash/wedge
    bad = subprocess.run(["bash", HOOK], input="not json at all", text=True,
                         capture_output=True, env={**os.environ, "CLAUDE_UNATTENDED": "1"})
    if _denied(bad) or bad.returncode != 0:
        failures.append(f"malformed payload did not fail open (rc={bad.returncode}, out={bad.stdout!r})")

    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — never-ASK guarantee not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — deny_ask.sh denies AskUserQuestion unattended (steers to PARK/PROCEED), "
          "inert otherwise, never wedges other tools, fails open on bad input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
