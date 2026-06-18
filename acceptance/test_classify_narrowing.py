#!/usr/bin/env python3
"""INT-1825 bug 1 — the keyword classifier false-PARKS on incidental jargon.

During the S2 run the matcher parked `s2-01` on the word "isolation" (container/network isolation,
not tenant isolation) and INT-1781 on "schema" (JSON-Schema, not a DB schema migration). The
word-boundary regex is meaning-blind. Fix = (a) narrow the two colliding tokens to require real
DB/tenant context, and (b) give the OPERATOR a config-only classification override as the durable
escape hatch — never an agent-runtime loosening, and never able to bypass the no-safety-net HALT.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness.decide import Action, classify  # noqa: E402


def _act(text, **kw):
    kw.setdefault("unattended", True)
    kw.setdefault("has_safety_net", True)
    return classify(text, **kw).action


def test_json_schema_jargon_does_not_park(failures):
    # The INT-1781 false-park: "schema" here is JSON-Schema, not a DB migration.
    text = "Canonicalise un-typed additionalProperties extras by inferred type (JSON-Schema)"
    if _act(text) != Action.PROCEED:
        failures.append("[bug1] JSON-Schema ticket false-PARKED on the word 'schema'")


def test_container_isolation_jargon_does_not_park(failures):
    # The s2-01 false-park: container/network isolation, not tenant isolation.
    text = "Run the worker container with --network none for process isolation"
    if _act(text) != Action.PROCEED:
        failures.append("[bug1] container-isolation ticket false-PARKED on the word 'isolation'")


def test_real_db_migration_still_parks(failures):
    if _act("Add column status to the orders table (schema migration)") != Action.PARK:
        failures.append("[bug1] a real DB schema migration was NOT parked — narrowing too aggressive")
    if _act("alter table users drop column legacy_flag") != Action.PARK:
        failures.append("[bug1] 'alter table ... drop column' was NOT parked")


def test_real_tenant_isolation_still_parks(failures):
    if _act("Enforce tenant isolation in the query layer via RLS") != Action.PARK:
        failures.append("[bug1] real tenant-isolation ticket was NOT parked — narrowing too aggressive")


def test_operator_override_forces_proceed(failures):
    # A money/billing ticket normally hard-PARKs; the operator can pre-clear it.
    text = "Add a discount banner to the checkout page"
    if _act(text) != Action.PARK:
        failures.append("[bug1] precondition: money ticket should park without override")
    if _act(text, override="PROCEED") != Action.PROCEED:
        failures.append("[bug1] operator override=PROCEED did not force PROCEED")


def test_operator_override_can_force_park(failures):
    text = "Tidy a code comment"  # routine -> normally PROCEED
    if _act(text) != Action.PROCEED:
        failures.append("[bug1] precondition: routine ticket should proceed without override")
    if _act(text, override="PARK") != Action.PARK:
        failures.append("[bug1] operator override=PARK did not force PARK")


def test_override_cannot_bypass_no_safety_net_halt(failures):
    # No reversibility safety net => HALT is non-negotiable even with an operator override.
    a = classify("Tidy a comment", unattended=True, has_safety_net=False, override="PROCEED").action
    if a != Action.HALT:
        failures.append("[bug1] override bypassed the no-safety-net HALT — unsafe")


def main() -> int:
    failures = []
    for t in (test_json_schema_jargon_does_not_park,
              test_container_isolation_jargon_does_not_park,
              test_real_db_migration_still_parks,
              test_real_tenant_isolation_still_parks,
              test_operator_override_forces_proceed,
              test_operator_override_can_force_park,
              test_override_cannot_bypass_no_safety_net_halt):
        t(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — classifier still mis-parks / override missing")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — jargon proceeds, real DB/tenant work parks, operator override holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
