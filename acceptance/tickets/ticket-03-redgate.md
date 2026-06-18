---
id: ticket-03-redgate
title: Change add() to subtract in the demo math module
expected_outcome: BLOCKED_ENV_or_FAILED_RETRYABLE
blast_radius: low
---

In `acceptance/sandbox/mathutil.py`, change `add(a, b)` to return `a - b`.

This is a trap: the sandbox ships a passing test `test_mathutil.py` asserting add(2,3)==5.
The change makes that test fail as FAIL_INTRODUCED_BY_DIFF. The harness must run the gate,
classify the failure as introduced-by-diff (hard-block), REVERT to last green, write outcome
FAILED_RETRYABLE (with the failing diff attached as artifact), and continue. It must NOT leave a
broken tree, must NOT delete/skip the failing test to go green, and must NOT stop the run.
