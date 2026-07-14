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

from agents_never_sleep import enforcement as E  # noqa: E402
from agents_never_sleep.enforcement import Action  # noqa: E402


def test_ask(failures):
    # `clarify` is Hermes's ask tool (v1.1) — denying it preempts the fail-open clarify-timeout.
    for name in ("AskUserQuestion", "ask_user", "ASK_USER", "clarify", "Clarify"):
        if not E.is_ask_tool(name):
            failures.append(f"[ask] {name!r} should be recognised as an ask-tool")
    for name in ("Bash", "run_shell_command", "", None):
        if E.is_ask_tool(name):
            failures.append(f"[ask] {name!r} should NOT be an ask-tool")
    for tool in ("AskUserQuestion", "clarify"):
        d = E.decide("pre_tool", tool_name=tool)
        if d.action != Action.DENY or "PARK" not in d.reason or "PROCEED" not in d.reason:
            failures.append(f"[ask] {tool} should DENY with PARK/PROCEED steer: {d}")


def test_irreversible(failures):
    deny = [
        ("git push --force origin main", "force_push"),
        ("git push -f", "force_push"),
        ("git push origin --delete feature", "remote_delete"),
        ("git push --mirror backup", "mirror_push"),
        ("rm -rf /", "recursive_root_delete"),
        ("rm -rf ~/important", "recursive_root_delete"),
        ("sudo rm -fr /etc", "recursive_root_delete"),
        ("psql -c 'DROP TABLE users'", "destructive_sql"),
        ("mysql -e 'truncate table logs'", "destructive_sql"),
        ("mkfs.ext4 /dev/sda1", "disk_destructive"),
        ("dd if=/dev/zero of=/dev/sda", "disk_destructive"),
        ("vault kv delete secret/x", "vault_secret"),
        ("vault kv put secret/x k=v", "vault_secret"),
        ("sendmail user@x.com < body", "send_email"),
        # standard-form bypass shapes closed post-audit (H1): whitespace/reordering/bundling/long-form
        # flags and force-via-refspec are all just as irreversible as the canonical spelling.
        ("git push origin +main", "force_push"),
        ("git push -qf origin main", "force_push"),
        ("rm -r -f /", "recursive_root_delete"),
        ("rm --recursive --force /", "recursive_root_delete"),
        ("rm -r /important", "recursive_root_delete"),
        # second security-review pass (2026-07-12): quoted/braced forms that beat the H1 patterns.
        ("git push origin '+master'", "force_push"),          # quoted +refspec force
        ('git push origin "+topic"', "force_push"),
        ("rm -rf ${HOME}", "recursive_root_delete"),   # brace form of the covered $HOME
        ('rm -rf "${HOME}"', "recursive_root_delete"),
        ("vault kv metadata delete secret/x", "vault_secret"),  # permanent (vs recoverable kv delete)
        ('dd if=/dev/zero of="/dev/sda"', "disk_destructive"),   # quoted device path
        # deny-list floor (2026-07-14): the 5 concessions moved ALLOW->DENY, plus the reviewer v4
        # regex fixes (mount-agnostic vault, command-anchored mail) and the retained new adds.
        ("redis-cli flushall", "redis_flush"),
        ("docker volume rm pgdata", "docker_volume_rm"),
        ("vault write db/rotate-root/x", "vault_secret"),
        ("vault kv put secret/app/db k=v", "vault_secret"),
        ("vault write kv/app k=v", "vault_secret"),
        ('mail -s "done" a@b', "send_email"),
        ("mail a@b < body", "send_email"),
        ("echo hi | mail a@b", "send_email"),
        ("sendmail -q", "send_email"),
        ("postqueue -f", "send_email"),
        ("systemctl mask nginx", "service_teardown"),
        ("docker system prune -af", "service_teardown"),
        ("docker compose down -v", "service_teardown"),
        ("git push origin v1.2.3", "release_tag_push"),
        ("git push --follow-tags origin main", "release_tag_push"),
        ("find / -name '*.log' -delete", "recursive_root_delete"),
        ("kubectl delete namespace staging", "k8s_delete"),
        ("kubectl delete deployment web --all", "k8s_delete"),
    ]
    for cmd, slug in deny:
        slugs = E.is_irreversible(cmd)
        if not slugs:
            failures.append(f"[irrev] should DENY: {cmd!r}")
        elif slug not in slugs:
            failures.append(f"[irrev] {cmd!r} slugs {slugs!r} do not include {slug!r}")
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
        "git push origin 'main'",          # quoted NORMAL branch (no +) — must ALLOW (no over-block)
        "echo ${HOME}/logs",               # a braced-var mention without rm — must ALLOW
        "vault kv get secret/x",           # a read, not a delete — must ALLOW
        # deny-list floor (2026-07-14): must-ALLOW regression set.
        "docker rm ci-container",          # container removal is routine, not the volume
        "systemctl restart nginx",         # restart/stop-alone stays ALLOW; only mask is a floor concern
        "kubectl delete pod x",            # narrowed to stateful/bulk kinds — a bare pod is routine
        "vault write sys/policies/acl/x",  # non-secret system mount — not a secret overwrite
        "git commit -m 'fix mailx bug'",   # mail over-block regression — toolname in prose, not a command
        "git push origin v2-rewrite",      # version-shaped BRANCH name, not a dotted release tag
        "find /var/www/proj -name '*.pyc' -delete",  # project subpath, not genuine root/home
        "redis-cli get k",                 # a read, not a flush — must ALLOW
        "docker compose down",             # plain compose down (no -v) stays ALLOW
        "terraform plan",                  # plan is routine, not destroy
        "aws s3 cp ./x s3://bucket/x",     # routine s3 op
    ]
    for cmd in allow:
        slugs = E.is_irreversible(cmd)
        if slugs and cmd != "echo dropping the table is fine in prose":
            failures.append(f"[irrev] should ALLOW: {cmd!r} (matched {slugs!r})")
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
