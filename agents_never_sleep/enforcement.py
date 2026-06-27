"""Provider-neutral enforcement DECISIONS — the single source of truth every platform adapter shares.

Pure functions, no platform I/O. The cross-platform dispatcher (harness/enforce.py) normalises each
platform's hook payload to (event, tool_name, command) and calls `decide()`; this module owns WHAT the
answer is, never HOW a given platform expresses it.

The three guarantees:
  * never-ASK        — deny a tool that asks the human a question (PARK/PROCEED instead).
  * deny-irreversible — deny a genuinely irreversible / outward command.
  * never-stop       — block an end-of-turn while the run-incomplete sentinel exists.

NB: the Claude adapter predates this module as three proven bash hooks (hooks/*.sh) and keeps its own
copy of the irreversible patterns. The canonical copy now lives HERE (INT-1935 single-source audit);
converging deny_irreversible.sh onto this module is a documented follow-up (avoid refactoring proven
security code without need). ALL new "never do" patterns go into _IRREVERSIBLE below — not in SKILL.md
or any hook file.
"""
from __future__ import annotations

import dataclasses
import enum
import re

# Tool names meaning "ask the human a question", across platforms (lower-cased match).
_ASK_TOOLS = {"askuserquestion", "ask_user"}

# Irreversible / outward command patterns (case-insensitive), mirroring hooks/deny_irreversible.sh.
# Deliberately NOT matching local `git reset --hard` / `git clean` — that's the harness's own revert.
_IRREVERSIBLE = [
    (re.compile(r"git\s+push\b.*(--force\b|\s-f\b|--force-with-lease\b)", re.I), "force-push"),
    (re.compile(r"git\s+push\b.*(:\S|\s--delete\b)", re.I), "remote branch/tag delete"),
    (re.compile(r"git\s+push\b.*--mirror\b", re.I), "mirror push"),
    (re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+(/|~|\$HOME)", re.I),
     "recursive delete of a root/home path"),
    (re.compile(r"\b(drop\s+database|drop\s+table|truncate\s+table)\b", re.I), "destructive SQL"),
    (re.compile(r"\bmkfs\b|\bdd\b[^\n]*\bof=/dev/|\bshred\b", re.I), "disk-destructive command"),
    (re.compile(r"\bvault\s+(kv\s+)?(delete|destroy)\b", re.I), "Vault secret deletion"),
    (re.compile(r"\bvault\s+kv\s+put\b|\bvault\s+write\b[^\n]*rotate", re.I), "Vault secret write/rotate"),
    (re.compile(r"\bsendmail\b|\bmailx\b|\bmail\s+-s\b", re.I), "sending real email"),
    (re.compile(r"\bsystemctl\s+(stop|disable)\b|\bdocker\s+(rm|volume\s+rm)\b", re.I),
     "service/volume teardown"),
]

ASK_DENY_REASON = (
    "agents-never-sleep: ASK is forbidden during an unattended run — there is nobody to answer. "
    "Do NOT ask. PARK this decision (record why, the candidate interpretations, the exact human "
    "next-action, and its contamination scope) and PROCEED to the next ticket; HALT only on "
    "genuinely irreversible danger.")

STOP_BLOCK_REASON = (
    "agents-never-sleep: backlog not drained — continue with the next ticket (PARK ambiguous / "
    "high-blast-radius items, do not stop or ask). When every ticket is in a terminal state, remove "
    ".unattended/run-incomplete and write the morning report.")


def irreversible_reason(kind: str) -> str:
    return (f"agents-never-sleep: blocked an irreversible/outward action ({kind}). "
            "Park it for human review instead.")


class Action(str, enum.Enum):
    ALLOW = "allow"   # let the tool/stop proceed
    DENY = "deny"     # block a tool/command
    BLOCK = "block"   # block a stop (keep working)


@dataclasses.dataclass
class Decision:
    action: Action
    reason: str = ""
    kind: str = ""   # the irreversible category, or "ask", when denied


def is_ask_tool(tool_name) -> bool:
    return (tool_name or "").strip().lower() in _ASK_TOOLS


def is_irreversible(command):
    """(True, kind) if the command is irreversible/outward, else (False, "")."""
    if not command:
        return False, ""
    for pat, kind in _IRREVERSIBLE:
        if pat.search(command):
            return True, kind
    return False, ""


def decide(event: str, *, tool_name=None, command=None, sentinel_present: bool = False) -> Decision:
    """The whole contract in one place.
      event="pre_tool" -> DENY an ask-tool or an irreversible command, else ALLOW.
      event="stop"     -> BLOCK while the run-incomplete sentinel exists, else ALLOW.
    """
    if event == "stop":
        if sentinel_present:
            return Decision(Action.BLOCK, STOP_BLOCK_REASON)
        return Decision(Action.ALLOW)
    if event == "pre_tool":
        if is_ask_tool(tool_name):
            return Decision(Action.DENY, ASK_DENY_REASON, "ask")
        bad, kind = is_irreversible(command)
        if bad:
            return Decision(Action.DENY, irreversible_reason(kind), kind)
        return Decision(Action.ALLOW)
    return Decision(Action.ALLOW)
