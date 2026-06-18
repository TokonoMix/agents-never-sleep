#!/usr/bin/env bash
# Stop-hook — prevent a PREMATURE stop while there is still unattended work to do.
#
# A Stop hook fires when the agent is about to end its turn. During an unattended run we only want
# the turn to end when the backlog is genuinely drained — otherwise the agent "soft-halts". This
# guard blocks the stop while a run-incomplete sentinel exists, and (critically) honours
# stop_hook_active so it can never create an infinite stop loop.
#
# Env-gated to CLAUDE_UNATTENDED=1; inert in normal interactive sessions.
set -euo pipefail

if [[ "${CLAUDE_UNATTENDED:-}" != "1" ]]; then
  exit 0
fi

payload="$(cat)"

# Never loop: if we already blocked once this turn, allow the stop.
already="$(printf '%s' "$payload" | python3 -c 'import json,sys;
try: print("1" if json.load(sys.stdin).get("stop_hook_active") else "0")
except Exception: print("0")')"
if [[ "$already" == "1" ]]; then
  exit 0
fi

# Opt-in fresh-session-per-N-tickets: when the launcher set a per-session ticket budget AND the
# driver has written the session-budget-reached marker, the agent has done its share for THIS
# session and may stop EARLY — the launcher will resume the backlog in a fresh session. This branch
# fires ONLY when UE_SESSION_TICKET_BUDGET is set; with it unset (the default) the never-stop
# guarantee below is fully intact. The marker path is pinned (like the sentinel) so it agrees with
# the driver even when CWD != repo.
if [[ -n "${UE_SESSION_TICKET_BUDGET:-}" ]]; then
  marker="${UE_SESSION_BUDGET_MARKER:-$PWD/.unattended/state/session-budget-reached}"
  if [[ -f "$marker" ]]; then
    exit 0
  fi
fi

# The orchestrator writes this sentinel at run start and removes it when the backlog is drained.
sentinel="${UE_RUN_INCOMPLETE:-$PWD/.unattended/run-incomplete}"
if [[ -f "$sentinel" ]]; then
  cat <<'JSON'
{"decision":"block","reason":"agents-never-sleep: backlog not drained — continue with the next ticket (PARK ambiguous/high-blast-radius items, do not stop or ask). When every ticket is in a terminal state, remove .unattended/run-incomplete and write the morning report."}
JSON
  exit 0
fi

exit 0
