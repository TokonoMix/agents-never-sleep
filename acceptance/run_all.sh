#!/usr/bin/env bash
# Blocking gate for the ANS self-build run: every acceptance suite must be green.
# Exits non-zero on the FIRST red suite (so a broken edit reverts), printing which failed.
set -uo pipefail
cd "$(dirname "$0")/.."
# When this gate runs INSIDE a live unattended run, the run's own env (CLAUDE_UNATTENDED,
# UE_RUN_INCOMPLETE, UE_HEARTBEAT, session-budget vars) leaks into the test subprocesses and
# flips env-gated behaviour the suites assert on (hooks, sentinel paths, fresh-session loop).
# The suites must observe a NEUTRAL environment regardless of who invokes them.
unset CLAUDE_UNATTENDED UE_RUN_INCOMPLETE UE_HEARTBEAT UE_SESSION_TICKET_BUDGET UE_SESSION_BUDGET_MARKER
fail=0
for t in acceptance/test_*.py acceptance/run_acceptance.py; do
  # test_real_claude.py is a pytest-marked INTEGRATION test (spawns a real `claude -p`,
  # needs pytest + the CLI + credentials) — by its own header it is NOT in the standard
  # hermetic suite. Run it explicitly: pytest -m integration acceptance/test_real_claude.py
  [ "$t" = "acceptance/test_real_claude.py" ] && continue
  if ! python3 "$t" >/dev/null 2>&1; then
    echo "RED: $t"
    fail=1
  fi
done
[ "$fail" = 0 ] && echo "ALL ACCEPTANCE SUITES GREEN"
exit "$fail"
