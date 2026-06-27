"""Cross-platform enforcement DISPATCHER.

One CLI, wired from each platform's hook config:
    <platform-hook> -> python3 -m agents_never_sleep.enforce <platform> <event>

It reads the platform's hook payload on stdin, NORMALISES it to (tool_name, command), asks the
platform-neutral core (harness.enforcement.decide) for the verdict, and writes THAT platform's
deny/block shape (or exit code). The proven Claude bash hooks stay as-is; this serves the other
platforms (and supports "claude" too for uniform testing).

Events (generic, mapped from each platform's own event name in its config snippet):
  * pre_tool — deny an ask-tool (never-ASK) or an irreversible command (deny-irreversible).
  * stop     — block an end-of-turn while the run-incomplete sentinel exists (never-stop).

Env-gated: inert unless UE_UNATTENDED=1 or CLAUDE_UNATTENDED=1, so normal interactive sessions on any
platform are untouched. Fails OPEN (allow / exit 0) on anything unexpected — enforcement must never
wedge a tool call.
"""
from __future__ import annotations

import json
import os
import sys

from .enforcement import Action, decide

_UNATTENDED_ENV = ("UE_UNATTENDED", "CLAUDE_UNATTENDED")
_STOP_LOOP_CAP = 5  # anti-infinite-loop: stop blocking after this many auto-continues


def _unattended() -> bool:
    return any(os.environ.get(k) == "1" for k in _UNATTENDED_ENV)


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}  # non-object JSON -> fail open (allow)
    except Exception:
        return {}


def _sentinel_path() -> str:
    return os.environ.get("UE_RUN_INCOMPLETE") or os.path.join(
        os.getcwd(), ".unattended", "run-incomplete")


def _sentinel_present() -> bool:
    return os.path.exists(_sentinel_path())


# Platform-independent anti-loop: a counter beside the sentinel, since most platforms' stop payloads
# carry no loop field. Bumped each time we block a stop; defuses at the cap; cleared when the backlog
# drains (sentinel gone). Best-effort — any I/O error degrades to "no count", never wedges.
def _counter_path() -> str:
    return os.path.join(os.path.dirname(_sentinel_path()) or ".", "stop-block-count")


def _stop_block_count() -> int:
    try:
        with open(_counter_path(), encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except Exception:
        return 0


def _bump_stop_block() -> None:
    n = _stop_block_count() + 1  # read BEFORE opening for write (open("w") truncates first)
    try:
        os.makedirs(os.path.dirname(_counter_path()) or ".", exist_ok=True)
        with open(_counter_path(), "w", encoding="utf-8") as fh:
            fh.write(str(n))
    except OSError:
        pass


def _clear_stop_block() -> None:
    try:
        os.remove(_counter_path())
    except OSError:
        pass


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _blob(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(str(v) for v in obj.values())
    return ""


def _normalize(platform: str, payload: dict):
    """Return (tool_name, command) from a platform's hook payload."""
    if platform == "copilot":
        ta = payload.get("toolArgs")
        if ta is None:
            ta = payload.get("tool_input") or {}
        cmd = ta.get("command") if isinstance(ta, dict) else (ta if isinstance(ta, str) else None)
        return payload.get("toolName") or payload.get("tool_name"), cmd or _blob(ta)
    if platform == "cursor":
        return "run_shell_command", payload.get("command")
    if platform == "windsurf":
        ti = payload.get("tool_info")
        if not isinstance(ti, dict):
            ti = {}
        return "run_shell_command", ti.get("command_line") or ti.get("command")
    # claude / gemini / codex / default: tool_name + tool_input.command
    ti = payload.get("tool_input") or {}
    return payload.get("tool_name"), (ti.get("command") if isinstance(ti, dict) else None) or _blob(ti)


def _emit_deny(platform: str, reason: str) -> int:
    if platform in ("claude", "codex"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
    elif platform == "gemini":
        print(json.dumps({"decision": "deny", "reason": reason}))
    elif platform == "copilot":
        print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
    elif platform == "cursor":
        print(json.dumps({"permission": "deny", "agent_message": reason, "user_message": reason}))
    elif platform == "windsurf":
        print(reason, file=sys.stderr)
        return 2  # Windsurf pre-hooks block via exit code 2
    else:
        print(json.dumps({"decision": "deny", "reason": reason}))
    return 0


def _emit_block(platform: str, reason: str) -> int:
    if platform in ("claude", "codex", "copilot"):
        print(json.dumps({"decision": "block", "reason": reason}))
    elif platform == "gemini":
        print(json.dumps({"decision": "deny", "reason": reason}))  # AfterAgent retry
    elif platform == "cursor":
        # Cursor's stop hook cannot block completion — best-effort nudge via a follow-up message.
        print(json.dumps({"followup_message": reason}))
    # windsurf: no blocking stop event -> nothing to emit (degraded; reported by capabilities.py)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        return 0
    platform, event = argv[0].lower(), argv[1].lower()
    if not _unattended():
        return 0  # inert outside unattended runs

    payload = _read_stdin()

    if event == "stop":
        # anti-loop: a platform-supplied signal (Claude stop_hook_active / Cursor loop_count) OR the
        # platform-independent filesystem counter — so never-stop can never loop forever anywhere.
        if (payload.get("stop_hook_active") or _as_int(payload.get("loop_count")) >= _STOP_LOOP_CAP
                or _stop_block_count() >= _STOP_LOOP_CAP):
            return 0
        d = decide("stop", sentinel_present=_sentinel_present())
        if d.action == Action.BLOCK:
            _bump_stop_block()
            return _emit_block(platform, d.reason)
        _clear_stop_block()  # backlog drained — reset the counter for the next run
        return 0

    if event == "pre_tool":
        tool_name, command = _normalize(platform, payload)
        d = decide("pre_tool", tool_name=tool_name, command=command)
        return _emit_deny(platform, d.reason) if d.action == Action.DENY else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
