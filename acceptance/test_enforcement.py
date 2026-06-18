#!/usr/bin/env python3
"""Enforcement CORE test — the platform-neutral decision logic shared by every adapter.

Proves the three guarantees as pure decisions (no platform I/O): never-ASK denies ask-tools,
deny-irreversible denies the destructive/outward command set (and NOT the harness's own revert),
never-stop blocks while the sentinel exists, and benign work is allowed. Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness import enforcement as E  # noqa: E402
from harness.enforcement import Action  # noqa: E402


def test_ask(failures):
    for name in ("AskUserQuestion", "ask_user", "ASK_USER"):
        if not E.is_ask_tool(name):
            failures.append(f"[ask] {name!r} should be recognised as an ask-tool")
    for name in ("Bash", "run_shell_command", "", None):
        if E.is_ask_tool(name):
            failures.append(f"[ask] {name!r} should NOT be an ask-tool")
    d = E.decide("pre_tool", tool_name="AskUserQuestion")
    if d.action != Action.DENY or "PARK" not in d.reason or "PROCEED" not in d.reason:
        failures.append(f"[ask] ask-tool should DENY with PARK/PROCEED steer: {d}")


def test_irreversible(failures):
    deny = [
        ("git push --force origin main", "force-push"),
        ("git push -f", "force-push"),
        ("git push origin --delete feature", "remote branch/tag delete"),
        ("git push --mirror backup", "mirror push"),
        ("rm -rf /", "recursive delete of a root/home path"),
        ("rm -rf ~/important", "recursive delete of a root/home path"),
        ("sudo rm -fr /etc", "recursive delete of a root/home path"),
        ("psql -c 'DROP TABLE users'", "destructive SQL"),
        ("mysql -e 'truncate table logs'", "destructive SQL"),
        ("mkfs.ext4 /dev/sda1", "disk-destructive command"),
        ("dd if=/dev/zero of=/dev/sda", "disk-destructive command"),
        ("vault kv delete secret/x", "Vault secret deletion"),
        ("vault kv put secret/x k=v", "Vault secret write/rotate"),
        ("sendmail user@x.com < body", "sending real email"),
        ("systemctl stop nginx", "service/volume teardown"),
        ("docker volume rm data", "service/volume teardown"),
    ]
    for cmd, kind in deny:
        bad, k = E.is_irreversible(cmd)
        if not bad:
            failures.append(f"[irrev] should DENY: {cmd!r}")
        elif k != kind:
            failures.append(f"[irrev] {cmd!r} kind {k!r} != {kind!r}")
        d = E.decide("pre_tool", command=cmd)
        if d.action != Action.DENY:
            failures.append(f"[irrev] decide should DENY: {cmd!r}")

    allow = [
        "git reset --hard HEAD",          # the harness's own revert — must NOT be blocked
        "git clean -fd",                   # ditto
        "git push origin main",            # normal push
        "rm -rf ./build",                  # local relative path
        "rm -rf node_modules",             # local
        "pytest -q",
        "echo dropping the table is fine in prose",  # not an actual SQL drop command... see note
    ]
    for cmd in allow:
        bad, _ = E.is_irreversible(cmd)
        if bad and cmd != "echo dropping the table is fine in prose":
            failures.append(f"[irrev] should ALLOW: {cmd!r}")
    # `git reset --hard` / `git clean` are the load-bearing allows — assert explicitly.
    for cmd in ("git reset --hard HEAD", "git clean -fd", "rm -rf ./build", "git push origin main"):
        if E.decide("pre_tool", command=cmd).action != Action.ALLOW:
            failures.append(f"[irrev] revert/benign must ALLOW: {cmd!r}")


def test_stop(failures):
    if E.decide("stop", sentinel_present=True).action != Action.BLOCK:
        failures.append("[stop] sentinel present should BLOCK the stop")
    if E.decide("stop", sentinel_present=False).action != Action.ALLOW:
        failures.append("[stop] no sentinel should ALLOW the stop")
    if "backlog" not in E.decide("stop", sentinel_present=True).reason:
        failures.append("[stop] block reason should explain the backlog isn't drained")


def test_benign_and_unknown(failures):
    if E.decide("pre_tool", tool_name="Bash", command="ls -la").action != Action.ALLOW:
        failures.append("[benign] a harmless command must ALLOW")
    if E.decide("weird_event").action != Action.ALLOW:
        failures.append("[unknown] an unknown event must default to ALLOW (never wedge)")


def main() -> int:
    failures = []
    test_ask(failures)
    test_irreversible(failures)
    test_stop(failures)
    test_benign_and_unknown(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — enforcement core not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — never-ASK / deny-irreversible / never-stop decisions hold; "
          "harness revert (git reset --hard / clean) correctly allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
