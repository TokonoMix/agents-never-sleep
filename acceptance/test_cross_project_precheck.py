#!/usr/bin/env python3
"""Cross-project / irreversible-op precheck — proactive PARK before the CPS hook hard-denies.

The agentixmesh CPS PreToolUse hook hard-denies a cross-project mutation under CLAUDE_UNATTENDED=1
(Rule #11: never autonomous). This precheck lets ANS PARK a ticket that clearly NAMES such an op
BEFORE it is attempted, so the run surfaces it for a per-action human grant instead of burning an
attempt on a mid-run deny. It mirrors the COMMAND-category signals of pm_mesh.cross_project_guard
(service-control / host-control / db-client + DDL-DML) as pure text — NOT the filesystem
foreign-write check, which prose cannot reveal and CPS backstops reactively.

The load-bearing tests are the NEGATIVES (proof we did not recreate the keyword over-park bug):
benign prose that merely MENTIONS another project, DML words without a db-client, and — critically —
ATTENDED mode (a human can answer CPS's confirm, so we must NOT pre-park) all PROCEED.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.decide import Action, classify  # noqa: E402


def _c(text, unattended=True):
    return classify(text, unattended=unattended, has_safety_net=True)


def test_negatives(failures):
    """The proof-of-no-over-park cases."""
    if _c("Rename a local helper variable in utils.py").action is not Action.PROCEED:
        failures.append("[neg] plain local refactor should PROCEED")
    # merely MENTIONING another project's service (read-only/docs) — no state-verb -> PROCEED
    if _c("Document how the beheer-v3 service resolves hostnames").action is not Action.PROCEED:
        failures.append("[neg] read-only mention of another project's service should PROCEED")
    # DML words WITHOUT a db-client -> PROCEED (client-gated, like CPS 'delete the truncate helper')
    if _c("Delete the truncate() helper and drop the dead code branch").action is not Action.PROCEED:
        failures.append("[neg] DML-ish words without a db-client should PROCEED")
    # ATTENDED mode + a real systemctl op -> NOT pre-parked here (the human answers CPS's confirm live)
    d = _c("After the config change, run systemctl restart the-app.service", unattended=False)
    if d.action is Action.PARK and str(d.category).startswith("cross_project_op"):
        failures.append("[neg] attended mode must NOT cross_project_op-PARK")


def test_positives(failures):
    d = _c("After the config change, run: systemctl restart the-app.service")
    if d.action is not Action.PARK or d.category != "cross_project_op:service-control":
        failures.append(f"[pos] service-control should PARK, got {d.action}/{d.category}")
    if not d.human_action or "Rule #11" not in d.human_action:
        failures.append("[pos] service-control PARK should surface a Rule #11 grant in human_action")
    d = _c("Recover the wedged box: sudo reboot")
    if d.action is not Action.PARK or d.category != "cross_project_op:host-control":
        failures.append(f"[pos] host-control should PARK, got {d.action}/{d.category}")
    d = _c("Clean the staging table: psql -c 'TRUNCATE TABLE sessions'")
    if d.action is not Action.PARK or d.category != "cross_project_op:database-ddl":
        failures.append(f"[pos] db DML via a client should PARK, got {d.action}/{d.category}")


def test_existing_paths_intact(failures):
    # a pure schema migration (no db-client word) still routes to the EXISTING category
    d = _c("Run a database schema migration to add a column")
    if d.action is not Action.PARK or not str(d.category).startswith("db_schema"):
        failures.append(f"[compat] schema migration must still PARK as db_schema*, got {d.category}")
    # the no-safety-net HALT still precedes everything
    halt = classify("systemctl restart x", unattended=True, has_safety_net=False)
    if halt.action is not Action.HALT:
        failures.append("[compat] no-safety-net must HALT even for a cross-project op")
    # a trusted operator override still wins over the precheck
    ov = classify("sudo reboot now", unattended=True, has_safety_net=True, override="PROCEED")
    if ov.action is not Action.PROCEED:
        failures.append("[compat] operator override=PROCEED must win over the cross_project_op park")


def main():
    failures = []
    for t in (test_negatives, test_positives, test_existing_paths_intact):
        t(failures)
    if failures:
        print("RED — cross-project precheck:")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("GREEN — cross-project precheck: parks named cross-project ops unattended, "
          "proceeds on benign/attended, existing routing intact")


if __name__ == "__main__":
    main()
