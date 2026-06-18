#!/usr/bin/env bash
# Blocking gate for the ANS self-build run: every acceptance suite must be green.
# Exits non-zero on the FIRST red suite (so a broken edit reverts), printing which failed.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
for t in acceptance/test_*.py acceptance/run_acceptance.py; do
  if ! python3 "$t" >/dev/null 2>&1; then
    echo "RED: $t"
    fail=1
  fi
done
[ "$fail" = 0 ] && echo "ALL ACCEPTANCE SUITES GREEN"
exit "$fail"
