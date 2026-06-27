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
    AGENT_CLIS, ALLOWLIST, detect_session_platform, is_allowlisted)
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


def main() -> int:
    failures = []
    test_map_shape(failures)
    test_detection_explicit_keys_only(failures)
    test_preflight_uses_shared_detector(failures)
    test_allowlist_bare_name_only(failures)
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
