#!/usr/bin/env python3
"""INT-1825 bug 1 (wiring) — the operator override must reach the live classify path.

A config block `classify.overrides: {<ticket-id>: PROCEED|PARK|HALT}` must be threaded from the
driver through `Orchestrator.classify_ticket` into `decide.classify`, so an operator can pre-clear
a ticket the heuristic would otherwise false-PARK. The override is keyed by ticket id and sourced
from config only.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness.decide import Action  # noqa: E402
from harness.orchestrator import Orchestrator  # noqa: E402
from harness.tickets import Ticket  # noqa: E402


class _StubGate:
    noop = True

    def baseline(self, repo):
        return True


def _orch(overrides):
    return Orchestrator(
        repo_dir=".", store=None, gate=_StubGate(), worker=None,
        artifacts_dir=".", unattended=True, ledger=None,
        classify_overrides=overrides,
    )


def test_override_threads_into_classify(failures):
    t = Ticket(id="INT-1781", title="Discount banner on checkout", body="add a discount",
               meta={}, path="")
    # Without an override this money ticket parks.
    if _orch({}).classify_ticket(t, True).action != Action.PARK:
        failures.append("[bug1-wire] precondition: money ticket should PARK without override")
    # With an operator override keyed by id it proceeds.
    d = _orch({"INT-1781": "PROCEED"}).classify_ticket(t, True)
    if d.action != Action.PROCEED:
        failures.append(f"[bug1-wire] config override not applied (got {d.action})")


def test_override_only_for_matching_id(failures):
    t = Ticket(id="INT-1781", title="add a discount", body="pricing change", meta={}, path="")
    d = _orch({"SOME-OTHER": "PROCEED"}).classify_ticket(t, True)
    if d.action != Action.PARK:
        failures.append("[bug1-wire] override leaked across ticket ids")


def main() -> int:
    failures = []
    test_override_threads_into_classify(failures)
    test_override_only_for_matching_id(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — operator override not wired through the live path")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — config classify.overrides reaches decide.classify by ticket id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
