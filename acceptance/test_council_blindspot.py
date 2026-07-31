#!/usr/bin/env python3
"""INT-2658 — council blind-spot surfacing in the run report.

The incident (2026-07-29, einstein-saas run "betaal-fundament"): a TRANSIENT gateway 502 on
ticket 01 made the worker conclude the gateway was down for the whole run. From ticket 04 on it
stopped trying entirely. The run report said the run finished; only the per-ticket
`review_coverage` fields revealed that the cross-vendor review — the compensating control that
justifies the PROCEED overrides — had been off for two thirds of the backlog.

Two deterministic signals the HARNESS can see without calling any LLM:
  1. a RUN of consecutive council errors (the generalisation footprint), and
  2. council_calls > 0 while council_cost_eur == 0.0 (calls "happened" but nothing was charged —
     mutually contradictory, so at least one of the two is not what it claims).

Both belong at the TOP of the report as BLIND SPOTs, not buried per ticket.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.report import build_report, council_blind_spots  # noqa: E402
from agents_never_sleep.state import OutcomeState, TicketOutcome  # noqa: E402

FAILED = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILED.append(label)


def mk(tid, coverage, *, state=OutcomeState.DONE, updated=0.0):
    return TicketOutcome(ticket_id=tid, state=state, why="w", review_coverage=coverage,
                   updated_at=updated)


CLEAN = "deterministic-gates · council:pass"
ERR = "deterministic-gates · council:error"


def test_consecutive_errors_are_flagged():
    print("consecutive council errors -> blind spot")
    outcomes = [mk("01", ERR, updated=1.0), mk("02", ERR, updated=2.0), mk("03", CLEAN, updated=3.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 3, "council_cost_eur": 0.3})
    check(len(notes) == 1, "exactly one note for one error run")
    check("2" in notes[0], "note states the length of the run")
    check("INT-2658" in notes[0], "note cites the incident ticket for context")


def test_single_isolated_error_is_not_a_run():
    print("one isolated council error -> no consecutive-run note")
    outcomes = [mk("01", CLEAN, updated=1.0), mk("02", ERR, updated=2.0), mk("03", CLEAN, updated=3.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 3, "council_cost_eur": 0.3})
    check(not any("consecutive" in n.lower() for n in notes),
          "a single transient error is not reported as a generalisation")


def test_longest_run_is_reported():
    print("longest run of errors is the one reported")
    outcomes = [mk("01", ERR, updated=1.0), mk("02", CLEAN, updated=2.0),
                mk("03", ERR, updated=3.0), mk("04", ERR, updated=4.0), mk("05", ERR, updated=5.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 5, "council_cost_eur": 0.5})
    run_note = [n for n in notes if "consecutive" in n.lower()]
    check(len(run_note) == 1, "one consecutive-run note")
    check("3" in run_note[0], "reports the LONGEST run (3), not the first (1)")
    check("03" in run_note[0] and "05" in run_note[0], "names the first and last ticket of the run")


def test_ordering_follows_recording_time_not_filename():
    """store.all() sorts by FILENAME; the generalisation footprint is about the order the tickets
    were actually RECORDED. A backlog whose ids don't sort in processing order must still show the
    true run."""
    print("run detection uses updated_at, not id order")
    outcomes = [mk("aa", CLEAN, updated=9.0), mk("bb", ERR, updated=1.0), mk("cc", ERR, updated=2.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 3, "council_cost_eur": 0.3})
    check(any("consecutive" in n.lower() for n in notes),
          "bb+cc are consecutive in recording order even though 'aa' sorts first")


def test_contradictory_cost_signal():
    print("council_calls > 0 with zero cost -> contradiction note")
    outcomes = [mk("01", CLEAN, updated=1.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 5, "council_cost_eur": 0.0})
    check(any("0.00" in n or "no cost" in n.lower() or "geen" in n.lower() for n in notes),
          "the zero-cost contradiction is surfaced")
    check(any("5" in n for n in notes), "the note states how many calls were claimed")
    # and the inverse: calls with real cost is NOT a contradiction
    quiet = council_blind_spots(outcomes, {"council_calls": 5, "council_cost_eur": 0.42})
    check(not any("contradict" in n.lower() for n in quiet),
          "calls WITH cost are not flagged")


def test_no_councils_at_all_is_silent():
    """A run with the council disabled must not grow a spurious blind spot — the onboarding /
    degraded-review notes already cover that case, and a false alarm here would train operators to
    ignore the banner."""
    print("no council calls at all -> silent")
    outcomes = [mk("01", "deterministic-gates", updated=1.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 0, "council_cost_eur": 0.0})
    check(notes == [], "nothing reported when no council ever ran")


def test_missing_progress_is_fail_safe():
    print("missing/garbage progress dict never raises")
    outcomes = [mk("01", ERR, updated=1.0), mk("02", ERR, updated=2.0)]
    for bad in ({}, None, {"council_calls": "x", "council_cost_eur": None}):
        try:
            notes = council_blind_spots(outcomes, bad)
        except Exception as exc:                                   # noqa: BLE001
            check(False, f"raised on progress={bad!r}: {exc}")
            continue
        check(any("consecutive" in n.lower() for n in notes),
              f"error-run still detected with progress={bad!r}")


def test_notes_reach_the_rendered_report():
    print("blind spots render at the top of the report")
    outcomes = [mk("01", ERR, updated=1.0), mk("02", ERR, updated=2.0)]
    notes = council_blind_spots(outcomes, {"council_calls": 2, "council_cost_eur": 0.0})
    text = build_report(outcomes, notes=notes)
    check("BLIND SPOT" in text, "rendered as a BLIND SPOT banner")
    # Anchor on the run summary line — everything below it is per-state detail. (Anchoring on the
    # first "**" would land INSIDE the banner itself, which is bold.)
    body_start = text.index("DONE clean.")
    for n in notes:
        head = n.split("—")[0].strip()[:30]
        check(head and text.index(head) < body_start,
              f"blind spot above the summary line: {head!r}")
    check(len(notes) == 2, "both signals fire together (error run + zero-cost contradiction)")


if __name__ == "__main__":
    for fn in (test_consecutive_errors_are_flagged, test_single_isolated_error_is_not_a_run,
               test_longest_run_is_reported, test_ordering_follows_recording_time_not_filename,
               test_contradictory_cost_signal, test_no_councils_at_all_is_silent,
               test_missing_progress_is_fail_safe, test_notes_reach_the_rendered_report):
        fn()
    if FAILED:
        print(f"\nRED — {len(FAILED)} check(s) failed:")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print("\nGREEN — council blind-spot surfacing (INT-2658)")
