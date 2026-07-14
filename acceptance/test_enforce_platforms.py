#!/usr/bin/env python3
"""Cross-platform dispatcher test — proves agents_never_sleep.enforce emits each platform's CORRECT deny/block
shape from that platform's documented stdin payload (the achievable hermetic bar; live smoke-tests on
the real tools are a documented manual follow-up).

For every platform: an irreversible command DENIES in the platform's own shape; a benign command is
ALLOWED (exit 0, no output); the dispatcher is inert when not unattended. Where the platform supports
it, an ask-tool DENIES and a stop-with-sentinel BLOCKS. Exit 0 = GREEN.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)


def _run(platform, event, payload, *, unattended=True, sentinel_path=None, extra_env=None, cwd=None):
    env = dict(os.environ)
    env.pop("CLAUDE_UNATTENDED", None)
    env.pop("UE_UNATTENDED", None)
    env.pop("UE_CONSENT", None)
    if unattended:
        env["UE_UNATTENDED"] = "1"
    if sentinel_path:
        env["UE_RUN_INCOMPLETE"] = sentinel_path
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([sys.executable, "-m", "agents_never_sleep.enforce", platform, event],
                       input=json.dumps(payload), text=True, capture_output=True,
                       cwd=cwd or SKILL_ROOT, env=env)
    return p


def _denied(platform, p) -> bool:
    if platform in ("windsurf", "crush", "opencode"):
        return p.returncode == 2
    try:
        out = json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    if platform in ("claude", "codex"):
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    if platform == "gemini":
        return out.get("decision") == "deny"
    if platform == "copilot":
        return out.get("permissionDecision") == "deny"
    if platform == "cursor":
        return out.get("permission") == "deny"
    return False


def _blocked(platform, p) -> bool:
    try:
        out = json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    if platform in ("claude", "codex", "copilot"):
        return out.get("decision") == "block"
    if platform == "gemini":
        return out.get("decision") == "deny"
    if platform == "cursor":
        return bool(out.get("followup_message"))
    return False


# (platform, irreversible pre_tool payload)
IRREVERSIBLE = {
    "claude":   {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}},
    "gemini":   {"tool_name": "run_shell_command", "tool_input": {"command": "rm -rf /"}},
    "codex":    {"tool_name": "Bash", "tool_input": {"command": "vault kv delete secret/x"}},
    # deny-list floor (2026-07-14): bare `systemctl stop` moved ALLOW; `mask` is the floor concern.
    "copilot":  {"toolName": "bash", "toolArgs": {"command": "systemctl mask nginx"}},
    "cursor":   {"command": "dd if=/dev/zero of=/dev/sda"},
    "windsurf": {"tool_info": {"command_line": "mkfs.ext4 /dev/sda1"}},
    # v1.2 — crush (shell hook) + opencode (JS plugin) both send the DEFAULT dispatcher
    # payload {tool_name, tool_input:{command}} and deny via exit 2 + stderr.
    "crush":    {"tool_name": "bash", "tool_input": {"command": "git push --force origin main"}},
    "opencode": {"tool_name": "bash", "tool_input": {"command": "rm -rf /home"}},
}
BENIGN = {
    "claude":   {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
    "gemini":   {"tool_name": "run_shell_command", "tool_input": {"command": "git reset --hard HEAD"}},
    "codex":    {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
    "copilot":  {"toolName": "bash", "toolArgs": {"command": "git clean -fd"}},
    "cursor":   {"command": "rm -rf ./build"},
    "windsurf": {"tool_info": {"command_line": "npm test"}},
    "crush":    {"tool_name": "bash", "tool_input": {"command": "git reset --hard HEAD"}},
    "opencode": {"tool_name": "bash", "tool_input": {"command": "npm test"}},
}
# platforms with a native ask-tool deny
ASK = {
    "claude":  {"tool_name": "AskUserQuestion", "tool_input": {}},
    "copilot": {"toolName": "ask_user", "toolArgs": {}},
}
# platforms where stop can be blocked / nudged
STOP_SUPPORT = ("claude", "gemini", "codex", "copilot", "cursor")
PLATFORMS = list(IRREVERSIBLE)


def _check_hook_contract_coverage(failures):
    """Every supported platform must carry a recorded hook-contract version (the drift guard the
    1.0 stability statement leans on) and the tested set must equal the capability matrix."""
    sys.path.insert(0, SKILL_ROOT)
    from agents_never_sleep import capabilities
    # The DISPATCHER-tested set must equal the dispatcher-shape platforms. The in-process
    # (hermes) and wrapper (aider) adapters enforce differently and have their own tests, so
    # they are excluded from this end-to-end dispatcher coverage check.
    tested = set(PLATFORMS)
    dispatcher = set(capabilities.dispatcher_platforms())
    if tested != dispatcher:
        failures.append(f"[matrix] dispatcher-tested {sorted(tested)} != dispatcher matrix {sorted(dispatcher)}")
    for plat in capabilities.SUPPORTED:  # ALL platforms (incl. in-process/wrapper) need a contract
        if not capabilities.hook_contract(plat) or "contract" not in capabilities.hook_contract(plat):
            failures.append(f"[matrix] {plat} has no recorded hook-contract version")


def test_consent_env_gate(failures):
    """t03 end-to-end: UE_CONSENT (frozen by the launcher, read ONLY from this env var) lifts a
    single consented, single-clean-statement irreversible command to ALLOW; absent/oversized/
    malformed UE_CONSENT all DENY (fail-safe — an unreadable consent is NO consent)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "redis-cli flushall"}}
    consent_json = json.dumps({"redis_flush": {"allowed": True}})

    p = _run("claude", "pre_tool", payload, extra_env={"UE_CONSENT": consent_json})
    if _denied("claude", p):
        failures.append(f"[consent-env] consented redis_flush should ALLOW end-to-end (out={p.stdout!r})")

    p = _run("claude", "pre_tool", payload)  # UE_CONSENT unset
    if not _denied("claude", p):
        failures.append("[consent-env] without UE_CONSENT the same command must DENY")

    oversized = json.dumps({"redis_flush": {"allowed": True}, "pad": "x" * 70000})
    p = _run("claude", "pre_tool", payload, extra_env={"UE_CONSENT": oversized})
    if not _denied("claude", p):
        failures.append("[consent-env] oversized (>64KB) UE_CONSENT must DENY (treated as no consent)")

    p = _run("claude", "pre_tool", payload, extra_env={"UE_CONSENT": "{bad"})
    if not _denied("claude", p):
        failures.append("[consent-env] malformed UE_CONSENT JSON must DENY")

    p = _run("claude", "pre_tool", payload, extra_env={"UE_CONSENT": "[1, 2]"})
    if not _denied("claude", p):
        failures.append("[consent-env] non-dict UE_CONSENT (JSON array) must DENY")


def test_consent_ignores_repo_file(failures):
    """Self-authorization regression: a repo-local file shaped like consent must have ZERO
    effect — only the frozen UE_CONSENT env var counts. An unattended agent CAN write repo
    files, so if a repo file granted consent the agent could authorize itself mid-run."""
    work = tempfile.mkdtemp(prefix="ue-enf-selfauth-")
    unattended_dir = os.path.join(work, ".unattended")
    os.makedirs(unattended_dir, exist_ok=True)
    with open(os.path.join(unattended_dir, "consent.json"), "w", encoding="utf-8") as fh:
        json.dump({"actions": {"redis_flush": {"allowed": True}}}, fh)

    payload = {"tool_name": "Bash", "tool_input": {"command": "redis-cli flushall"}}
    env = dict(os.environ)
    env["UE_UNATTENDED"] = "1"
    env.pop("CLAUDE_UNATTENDED", None)
    env.pop("UE_CONSENT", None)
    env["PYTHONPATH"] = SKILL_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, "-m", "agents_never_sleep.enforce", "claude", "pre_tool"],
                       input=json.dumps(payload), text=True, capture_output=True,
                       cwd=work, env=env)
    if not _denied("claude", p):
        failures.append("[consent-env] a repo-local consent-shaped file must have zero effect "
                         f"(only UE_CONSENT counts): out={p.stdout!r}")


def main() -> int:
    failures = []
    _check_hook_contract_coverage(failures)
    def fresh_sentinel():
        # each run gets its OWN .unattended dir, so the platform-independent stop-block counter
        # (kept beside the sentinel) is isolated per run — exactly as in production.
        p = os.path.join(tempfile.mkdtemp(prefix="ue-enf-"), "run-incomplete")
        open(p, "w").close()
        return p

    for plat in PLATFORMS:
        sentinel = fresh_sentinel()
        # irreversible -> DENY in the platform's shape
        p = _run(plat, "pre_tool", IRREVERSIBLE[plat])
        if not _denied(plat, p):
            failures.append(f"[{plat}] irreversible not denied (rc={p.returncode} out={p.stdout!r})")

        # benign -> ALLOW (exit 0, no deny)
        p = _run(plat, "pre_tool", BENIGN[plat])
        if p.returncode != 0 or _denied(plat, p):
            failures.append(f"[{plat}] benign should ALLOW (rc={p.returncode} out={p.stdout!r})")

        # inert when not unattended
        p = _run(plat, "pre_tool", IRREVERSIBLE[plat], unattended=False)
        if p.returncode != 0 or _denied(plat, p):
            failures.append(f"[{plat}] should be inert when not unattended (out={p.stdout!r})")

        # stop with sentinel -> BLOCK where supported; windsurf degrades (no crash, exit 0)
        p = _run(plat, "stop", {}, sentinel_path=sentinel)
        if plat in STOP_SUPPORT:
            if not _blocked(plat, p):
                failures.append(f"[{plat}] stop+sentinel should block/nudge (out={p.stdout!r})")
        else:  # windsurf
            if p.returncode != 0:
                failures.append(f"[{plat}] degraded stop must not crash (rc={p.returncode})")

        # stop WITHOUT sentinel -> allow (no block)
        p = _run(plat, "stop", {})
        if p.returncode != 0 or _blocked(plat, p):
            failures.append(f"[{plat}] stop without sentinel should ALLOW (out={p.stdout!r})")

    # ask-tool deny on the two platforms that support it
    for plat, payload in ASK.items():
        p = _run(plat, "pre_tool", payload)
        if not _denied(plat, p):
            failures.append(f"[{plat}] ask-tool should DENY (out={p.stdout!r})")

    # anti-loop (payload signal): stop_hook_active / loop_count must NOT block, even with a sentinel.
    p = _run("claude", "stop", {"stop_hook_active": True}, sentinel_path=fresh_sentinel())
    if _blocked("claude", p):
        failures.append("[claude] stop_hook_active must defuse the block (anti-loop)")
    p = _run("cursor", "stop", {"loop_count": 9}, sentinel_path=fresh_sentinel())
    if _blocked("cursor", p):
        failures.append("[cursor] loop_count over cap must defuse the nudge (anti-loop)")

    # anti-loop (filesystem counter): on a platform whose stop payload has NO loop field (gemini),
    # repeated blocks must eventually DEFUSE so never-stop can't loop forever.
    s = fresh_sentinel()
    blocked_then_defused = False
    for i in range(8):
        p = _run("gemini", "stop", {}, sentinel_path=s)
        if i < 5 and not _blocked("gemini", p):
            failures.append(f"[gemini] stop block #{i} should still block (counter not yet at cap)")
        if not _blocked("gemini", p):
            blocked_then_defused = True
            break
    if not blocked_then_defused:
        failures.append("[gemini] filesystem stop-counter never defused — never-stop could loop forever")

    # fail-open on non-dict stdin: a JSON array must NOT crash (would be exit 1 = let-through on
    # windsurf). It should be a clean allow (rc 0, no deny), never a wedge.
    p = subprocess.run([sys.executable, "-m", "agents_never_sleep.enforce", "windsurf", "pre_tool"],
                       input="[1,2,3]", text=True, capture_output=True, cwd=SKILL_ROOT,
                       env={**os.environ, "UE_UNATTENDED": "1"})
    if p.returncode not in (0,):
        failures.append(f"[windsurf] non-dict stdin must fail OPEN (rc={p.returncode} err={p.stderr!r})")

    # launcher (enforce.sh) rc-mapping: a real Windsurf deny must surface as exit 2; benign as exit 0.
    launcher = os.path.join(SKILL_ROOT, "hooks", "enforce.sh")
    env = {**os.environ, "UE_UNATTENDED": "1"}
    p = subprocess.run(["bash", launcher, "windsurf", "pre_tool"],
                       input=json.dumps(IRREVERSIBLE["windsurf"]), text=True, capture_output=True, env=env)
    if p.returncode != 2:
        failures.append(f"[launcher] windsurf irreversible should exit 2 via enforce.sh (rc={p.returncode})")
    p = subprocess.run(["bash", launcher, "windsurf", "pre_tool"],
                       input=json.dumps(BENIGN["windsurf"]), text=True, capture_output=True, env=env)
    if p.returncode != 0:
        failures.append(f"[launcher] windsurf benign should exit 0 via enforce.sh (rc={p.returncode})")

    test_consent_env_gate(failures)
    test_consent_ignores_repo_file(failures)

    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — cross-platform dispatcher not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — all 6 platforms deny irreversible in their own shape, allow benign, "
          "stay inert off-unattended, block/degrade stop correctly, with anti-loop defusing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
