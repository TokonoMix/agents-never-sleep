#!/usr/bin/env bash
# PreToolUse deny-hook — make "never ASK" STRUCTURAL, exactly like never-stop.
#
# The whole point of an unattended run is that there is nobody at 2am to answer a question, so a
# single AskUserQuestion wastes the night. The autonomy contract says the agent must instead PROCEED
# (assume + log, low blast-radius) or PARK (defer THIS ticket, keep moving) — never ASK. Relying on
# the agent's discipline is precisely what fails, so this hook DENIES the ask at the code layer and
# returns a reason that re-routes it into PARK/PROCEED.
#
# It is:
#   * env-gated: inert unless CLAUDE_UNATTENDED=1 (normal interactive sessions can ask freely),
#   * narrowly scoped: only the AskUserQuestion tool is denied; everything else is allowed.
#
# Hook contract: reads the PreToolUse JSON on stdin, prints a deny decision to block, exits 0 to allow.
set -euo pipefail

# Inert outside unattended runs — interactive sessions must still be able to ask.
if [[ "${CLAUDE_UNATTENDED:-}" != "1" ]]; then
  exit 0
fi

payload="$(cat)"

# Identify the tool. Defensive even though the settings matcher is AskUserQuestion: a malformed
# payload must fail OPEN (allow) so this hook can never wedge an unrelated tool.
tool="$(printf '%s' "$payload" | python3 -c '
import json,sys
try:
    print(json.load(sys.stdin).get("tool_name","UNKNOWN"))
except Exception:
    print("UNKNOWN")
')"

if [[ "$tool" == "AskUserQuestion" ]]; then
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"agents-never-sleep: ASK is forbidden during an unattended run — there is nobody to answer at 2am. Do NOT ask. Instead PARK this decision: record (1) why you are unsure, (2) the candidate interpretations, (3) the exact human next-action, and (4) its contamination scope; then PROCEED to the next independent ticket. Only HALT on genuinely irreversible danger with no safety net."}}
JSON
  exit 0
fi

# Default: allow.
exit 0
