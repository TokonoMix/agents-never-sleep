#!/usr/bin/env python3
"""ANS-OSS — harness/agent_clis.py: the single-source CLI map + session detection.

Guards the review findings of 2026-06-10:
  * detection uses EXPLICIT env-marker keys only — no substring scans over the whole
    env, and an exported API key (GEMINI_API_KEY) must NOT count as "inside that CLI";
  * UE_PLATFORM (the harness's explicit override) always wins;
  * every map entry keeps the safe/unattended split: cmd_unattended differs from
    cmd_safe, carries the autonomy flag, and documents what it grants;
  * the allowlist matches on basename so /usr/local/bin/claude passes and bash doesn't.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep.agent_clis import (  # noqa: E402
    AGENT_CLIS, ALLOWLIST, detect_session_platform, is_allowlisted, scaffold_preset)
from agents_never_sleep import preflight  # noqa: E402


def test_map_shape(failures):
    for name, spec in AGENT_CLIS.items():
        for key in ("cmd_safe", "cmd_unattended", "grants", "version_args"):
            if not spec.get(key):
                failures.append(f"[map] {name} misses {key}")
        if spec.get("cmd_safe") == spec.get("cmd_unattended"):
            failures.append(f"[map] {name}: unattended variant must differ from safe "
                            "(it carries the autonomy flag)")
        if spec.get("cmd_safe", [""])[0] != name:
            failures.append(f"[map] {name}: argv[0] should be the CLI name itself")
        if name not in ALLOWLIST:
            failures.append(f"[map] {name} missing from ALLOWLIST")


def test_detection_explicit_keys_only(failures):
    if detect_session_platform({}) != "":
        failures.append("[detect] empty env should detect nothing")
    if detect_session_platform({"GEMINI_API_KEY": "x"}) != "":
        failures.append("[detect] a bare API key must NOT count as a CLI session")
    if detect_session_platform({"SOME_VAR": "CLAUDE_CODE"}) != "":
        failures.append("[detect] substring in an unrelated value must not match")
    if detect_session_platform({"CLAUDECODE": "1"}) != "claude":
        failures.append("[detect] CLAUDECODE marker should detect claude")
    if detect_session_platform({"GEMINI_CLI": "1"}) != "gemini":
        failures.append("[detect] GEMINI_CLI marker should detect gemini")
    if detect_session_platform({"CODEX_SANDBOX": "1", "UE_PLATFORM": "copilot"}) != "copilot":
        failures.append("[detect] UE_PLATFORM override must win over markers")
    if detect_session_platform({"UE_PLATFORM": "nonsense"}) != "":
        failures.append("[detect] unknown UE_PLATFORM should fall through to markers/empty")


def test_preflight_uses_shared_detector(failures):
    saved = dict(os.environ)
    try:
        for key in [k for ks in
                    ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX",
                     "OPENAI_CODEX", "GEMINI_CLI", "GEMINI_CLI_SESSION", "GEMINI_API_KEY",
                     "COPILOT_AGENT", "GITHUB_COPILOT_CLI", "UE_PLATFORM")
                    for k in [ks]]:
            os.environ.pop(key, None)
        os.environ["GEMINI_API_KEY"] = "x"  # the old buggy heuristic keyed on this
        if preflight._detect_platform() != "unknown":
            failures.append("[preflight] bare GEMINI_API_KEY must not detect gemini")
        os.environ["CLAUDECODE"] = "1"
        if preflight._detect_platform() != "claude-code":
            failures.append("[preflight] CLAUDECODE should map to claude-code")
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_allowlist_bare_name_only(failures):
    # Only BARE known-CLI names pass. A path-bearing argv0 (./claude, /abs/claude) must
    # NOT — a hostile repo could ship its own executable named `claude` and a basename
    # match would wave it through (security 2026-06-10). Path-bearing commands fall through
    # to the explicit allow_custom_agent opt-out instead.
    for good in ("claude", "codex", "gemini", "copilot"):
        if not is_allowlisted(good):
            failures.append(f"[allowlist] bare {good} should pass")
    for bad in ("bash", "sh", "curl", "python3", "/bin/bash",
                "/usr/local/bin/claude", "./claude", "x/claude"):
        if is_allowlisted(bad):
            failures.append(f"[allowlist] {bad} must not be allowlisted")


def test_scaffold_preset_confirmed_vs_safe(failures):
    # The single source of a launcher-preset row (config.run_wizard AND init_cmd.run_init both
    # call this) — confirmed=True must select cmd_unattended, confirmed=False must select
    # cmd_safe, and the row shape must be exactly {cmd, autonomy_confirmed, env}.
    for name, spec in AGENT_CLIS.items():
        row_true = scaffold_preset(name, confirmed=True)
        row_false = scaffold_preset(name, confirmed=False)
        if row_true != {"cmd": spec["cmd_unattended"], "autonomy_confirmed": True, "env": {}}:
            failures.append(f"[scaffold_preset] {name} confirmed=True row wrong: {row_true!r}")
        if row_false != {"cmd": spec["cmd_safe"], "autonomy_confirmed": False, "env": {}}:
            failures.append(f"[scaffold_preset] {name} confirmed=False row wrong: {row_false!r}")


def test_claude_unattended_is_full_bypass(failures):
    # 2026-09-30 operator decision: an unattended run must not be left with an edits-only mode.
    # In headless `-p`, a gated Bash call is auto-DENIED (not prompted), so acceptEdits yields a
    # run that silently does nothing. The shipped unattended variant is therefore full bypass,
    # and the lockstep marker table knows `auto` (won't hang; the classifier can still deny).
    from agents_never_sleep.agent_clis import (NONINTERACTIVE_MARKERS,
                                               is_noninteractive_permission)
    want = ["claude", "-p", "--permission-mode", "bypassPermissions"]
    if AGENT_CLIS["claude"]["cmd_unattended"] != want:
        failures.append(f"[claude] cmd_unattended should be {want}, got "
                        f"{AGENT_CLIS['claude']['cmd_unattended']}")
    if "auto" not in NONINTERACTIVE_MARKERS["claude"]["pairs"]["--permission-mode"]:
        failures.append("[claude] NONINTERACTIVE_MARKERS must accept --permission-mode auto")
    if is_noninteractive_permission(["claude", "-p", "--permission-mode", "auto"]) is not True:
        failures.append("[claude] --permission-mode auto must read as non-interactive (won't hang)")
    if "acceptEdits" in " ".join(AGENT_CLIS["claude"]["grants"]):
        failures.append("[claude] grants text must describe the bypass flag, not acceptEdits")


def test_claude_permission_level(failures):
    # One classifier for the launcher's GO/NOTE/NO-GO decision (SSOT, next to the marker
    # table): full (end-to-end autonomous) > auto (won't hang, may still deny a step) >
    # edits (file edits only; shell auto-denied headless) > gated (prompts → hangs detached).
    from agents_never_sleep.agent_clis import claude_permission_level
    cases = (
        (["claude", "-p", "--permission-mode", "bypassPermissions"], "full"),
        (["claude", "-p", "--permission-mode=bypassPermissions"], "full"),
        (["claude", "-p", "--dangerously-skip-permissions"], "full"),
        (["claude", "-p", "--permission-mode", "auto"], "auto"),
        (["claude", "-p", "--permission-mode", "acceptEdits"], "edits"),
        (["claude", "-p", "--permission-mode=acceptEdits"], "edits"),
        (["claude", "-p"], "gated"),
        (["claude", "-p", "--permission-mode", "plan"], "gated"),
        (["claude", "-p", "--permission-mode", "default"], "gated"),
    )
    for argv, want in cases:
        got = claude_permission_level(argv)
        if got != want:
            failures.append(f"[level] {argv} → {got!r}, want {want!r}")


def test_with_full_autonomy_rewrites_claude_argv(failures):
    # The consent-driven repair: append the bypass pair when no --permission-mode is present,
    # replace the value (both forms) when one is, drop a redundant edits-only pair, and leave an
    # already-full argv untouched. Other CLIs are returned unchanged (no table knowledge).
    from agents_never_sleep.agent_clis import with_full_autonomy
    full = ["claude", "-p", "--permission-mode", "bypassPermissions"]
    cases = (
        (["claude", "-p"], full),
        (["claude", "-p", "--permission-mode", "acceptEdits"], full),
        (["claude", "-p", "--permission-mode=plan"], ["claude", "-p",
                                                     "--permission-mode=bypassPermissions"]),
        (["claude", "-p", "--permission-mode", "auto", "--model", "x"],
         ["claude", "-p", "--permission-mode", "bypassPermissions", "--model", "x"]),
        (full, full),
        (["claude", "-p", "--dangerously-skip-permissions"],
         ["claude", "-p", "--dangerously-skip-permissions"]),
        (["codex", "exec"], ["codex", "exec"]),
    )
    for argv, want in cases:
        before = list(argv)
        got = with_full_autonomy(argv)
        if got != want:
            failures.append(f"[repair] {argv} → {got}, want {want}")
        if argv != before:
            failures.append(f"[repair] input argv was mutated: {before} → {argv}")


def main() -> int:
    failures = []
    test_map_shape(failures)
    test_detection_explicit_keys_only(failures)
    test_preflight_uses_shared_detector(failures)
    test_allowlist_bare_name_only(failures)
    test_scaffold_preset_confirmed_vs_safe(failures)
    test_claude_unattended_is_full_bypass(failures)
    test_claude_permission_level(failures)
    test_with_full_autonomy_rewrites_claude_argv(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — agent-CLI map/detection not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — single-source map shape, explicit-marker detection, "
          "UE_PLATFORM override and basename allowlist all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
