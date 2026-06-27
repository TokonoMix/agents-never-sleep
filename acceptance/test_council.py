#!/usr/bin/env python3
"""Council module test — risk routing (on the DIFF), planning/cost, disposition, and end-to-end.

The council is advisory and NEVER blocks the run; what it CAN do is withhold the automatic trust
upgrade — a high-risk diff that wasn't cleanly vetted is recorded DONE_LOW_CONFIDENCE (needs daylight
review) instead of a silent DONE. These tests prove that deterministically, with NO live LLM call
(the LLM calls are the agent's via MCP; the harness owns only this scaffolding).

Exit 0 = GREEN.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import council  # noqa: E402
from agents_never_sleep.council import CouncilTier, CouncilVerdict  # noqa: E402
from agents_never_sleep.driver import StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402

CFG = {
    "gates": [{"name": "tests", "command": [sys.executable, "-m", "unittest", "discover",
                                            "-s", ".", "-p", "test_*.py"], "blocking": True}],
    "integrations": {"tokonomix": {"enabled": True}},
    "council": {
        "enabled": True,
        "light": {"proposers": ["a", "b", "c"], "judges": ["j1", "j2"],
                  "mode": "consensus", "max_tokens": 900},
        "heavy": {"proposers": ["a", "b", "c", "d", "e"], "judges": ["j1", "j2", "j3"],
                  "mode": "consensus", "max_tokens": 1400},
        "prices_cents_per_mtok": {"a": [500, 2500], "b": [250, 1500], "c": [25, 38],
                                  "d": [15, 60], "e": [125, 1000], "j1": [500, 2500],
                                  "j2": [125, 1000], "j3": [250, 1500]},
        "est_prompt_tokens": 3000,
    },
}


def test_routing(failures):
    if council.route_from_diff(["README.md", "docs/guide.md"], "words") != CouncilTier.NONE:
        failures.append("[route] docs-only should be NONE")
    if council.route_from_diff(["src/util.py"], "def helper():\n    return 1") != CouncilTier.LIGHT:
        failures.append("[route] routine code should be LIGHT")
    if council.route_from_diff(["src/auth/login.py"], "def login(): pass") != CouncilTier.HEAVY:
        failures.append("[route] auth path should be HEAVY")
    if council.route_from_diff(["src/util.py"], "ALTER TABLE users ADD COLUMN x int") != \
            CouncilTier.HEAVY:
        failures.append("[route] migration content should be HEAVY")
    if council.route_from_diff([], "") != CouncilTier.NONE:
        failures.append("[route] empty diff should be NONE")


def test_plan_and_cost(failures):
    p = council.plan(CFG, CouncilTier.HEAVY)
    if len(p.proposers) != 5 or len(p.judges) != 3:
        failures.append(f"[plan] heavy should be 5/3, got {len(p.proposers)}/{len(p.judges)}")
    if p.est_cost_eur <= 0:
        failures.append(f"[plan] est cost should be > 0, got {p.est_cost_eur}")
    if "HEAVY" not in p.summary_line("t-1") or "€" not in p.summary_line("t-1"):
        failures.append("[plan] summary line missing tier/cost")
    if council.plan(CFG, CouncilTier.NONE).summary_line("t-1").find("SKIP") < 0:
        failures.append("[plan] NONE tier summary should say SKIP")


def test_dispose(failures):
    LC = OutcomeState.DONE_LOW_CONFIDENCE
    cases = [
        # HEAVY: only a clean PASS is auto-trusted; everything else needs daylight review.
        (CouncilTier.HEAVY, CouncilVerdict.SKIPPED, LC, True),
        (CouncilTier.HEAVY, CouncilVerdict.ERROR, LC, True),
        (CouncilTier.HEAVY, CouncilVerdict.CONCERNS, LC, True),
        (CouncilTier.HEAVY, CouncilVerdict.PASS, OutcomeState.DONE, False),
        # LIGHT: FAIL-SAFE — a review failure must not auto-trust (the bug the council caught).
        (CouncilTier.LIGHT, CouncilVerdict.CONCERNS, LC, False),
        (CouncilTier.LIGHT, CouncilVerdict.ERROR, LC, False),     # was DONE (fail-open) — now LC
        (CouncilTier.LIGHT, CouncilVerdict.SKIPPED, OutcomeState.DONE, False),  # optional skip ok
        (CouncilTier.LIGHT, CouncilVerdict.PASS, OutcomeState.DONE, False),
        # NONE: docs/trivial — never needs a council, verdict irrelevant.
        (CouncilTier.NONE, CouncilVerdict.SKIPPED, OutcomeState.DONE, False),
        (CouncilTier.NONE, CouncilVerdict.ERROR, OutcomeState.DONE, False),
    ]
    for tier, verdict, want_state, want_daylight in cases:
        d = council.dispose(tier, verdict, "", ticket_title="t")
        if d.state != want_state:
            failures.append(f"[dispose] {tier.value}/{verdict.value}: state {d.state} != {want_state}")
        if d.needs_daylight_review != want_daylight:
            failures.append(f"[dispose] {tier.value}/{verdict.value}: daylight "
                            f"{d.needs_daylight_review} != {want_daylight}")


def test_reconcile_and_coerce(failures):
    from agents_never_sleep.orchestrator import _coerce_verdict
    # integrity cross-check: a PASS that contradicts its own summary is distrusted -> CONCERNS
    if council.reconcile(CouncilVerdict.PASS, "all good, no issues found") != CouncilVerdict.PASS:
        failures.append("[reconcile] clean PASS should stay PASS")
    if council.reconcile(CouncilVerdict.PASS, "ok but a security concern about token handling") != \
            CouncilVerdict.CONCERNS:
        failures.append("[reconcile] PASS contradicted by concern-language should become CONCERNS")
    # coerce is fail-safe: missing -> SKIPPED, malformed -> ERROR (never silently benign)
    if _coerce_verdict(None, CouncilTier.HEAVY) != CouncilVerdict.SKIPPED:
        failures.append("[coerce] missing verdict should be SKIPPED")
    if _coerce_verdict("passed", CouncilTier.HEAVY) != CouncilVerdict.ERROR:
        failures.append("[coerce] malformed verdict should fail safe to ERROR")
    if _coerce_verdict("pass", CouncilTier.HEAVY) != CouncilVerdict.PASS:
        failures.append("[coerce] valid verdict should map through")


def test_budget_brake(failures):
    cfg_eur = {"budget": {"per_night_euro_cap": 2.0}}
    if council.budget_exhausted(cfg_eur, {"council_cost_eur": 0.5, "council_calls": 1})[0]:
        failures.append("[budget] under €cap should not be exhausted")
    if not council.budget_exhausted(cfg_eur, {"council_cost_eur": 2.5, "council_calls": 9})[0]:
        failures.append("[budget] over €cap should be exhausted")
    cfg_calls = {"budget": {"max_council_calls_per_night": 3}}
    if not council.budget_exhausted(cfg_calls, {"council_cost_eur": 0.0, "council_calls": 3})[0]:
        failures.append("[budget] call-count cap should be exhausted (independent of cost)")
    if council.budget_exhausted({"budget": {}}, {"council_cost_eur": 99, "council_calls": 99})[0]:
        failures.append("[budget] no caps configured -> never exhausted")


def test_enabled(failures):
    if not council.enabled({"council": {"enabled": True},
                            "integrations": {"tokonomix": {"enabled": True}}}):
        failures.append("[enabled] should be on when both flags on")
    if council.enabled({"council": {"enabled": False}}):
        failures.append("[enabled] should be off when council disabled")
    if council.enabled({"council": {"enabled": True},
                        "integrations": {"tokonomix": {"enabled": False}}}):
        failures.append("[enabled] should be off when tokonomix disabled")


def test_end_to_end_disposition(failures):
    """Drive the real StepDriver with council enabled: a HEAVY-risk diff completed WITHOUT a council
    verdict must land DONE_LOW_CONFIDENCE (needs daylight); a LIGHT diff with verdict=pass -> DONE."""
    work = tempfile.mkdtemp(prefix="ue-council-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    tickets = load_tickets(os.path.join(HERE, "tickets"))
    store = OutcomeStore(state_dir)
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=os.path.join(work, "artifacts"), unattended=True,
                        ledger=ledger)
    driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                        report_path=os.path.join(work, "report.md"), config=CFG)

    def heavy_edit():  # a new auth file -> HEAVY by path, valid python so the gate stays green
        with open(os.path.join(repo, "auth.py"), "w", encoding="utf-8") as fh:
            fh.write("def authorize(jwt_token):\n    return bool(jwt_token)\n")

    def light_edit():  # a benign comment append -> LIGHT, gate green
        with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# routine note\n")

    for _ in range(20):
        res = driver.next_ticket()
        if res["status"] != "PROCEED":
            break
        tid = res["ticket"]["id"]
        if tid == "ticket-01-trivial":
            # council hint must be present and carry a plan
            if "council" not in res:
                failures.append("[e2e] PROCEED payload missing council hint")
            heavy_edit()
            driver.complete_ticket(attempted="added auth.py")          # NO council verdict
        else:
            light_edit()
            driver.complete_ticket(attempted="note", council_verdict="pass")

    o1 = store.read("ticket-01-trivial")
    if o1 is None or o1.state != OutcomeState.DONE_LOW_CONFIDENCE:
        failures.append(f"[e2e] heavy-diff/no-council should be DONE_LOW_CONFIDENCE, got "
                        f"{getattr(o1, 'state', None)}")
    elif "NEEDS-DAYLIGHT-REVIEW" not in (o1.review_coverage or ""):
        failures.append(f"[e2e] heavy-diff missing daylight tag: {o1.review_coverage!r}")
    o3 = store.read("ticket-03-redgate")
    if o3 is None or o3.state != OutcomeState.DONE:
        failures.append(f"[e2e] light-diff/council-pass should be DONE, got {getattr(o3,'state',None)}")


def main() -> int:
    failures = []
    test_routing(failures)
    test_plan_and_cost(failures)
    test_dispose(failures)
    test_reconcile_and_coerce(failures)
    test_budget_brake(failures)
    test_enabled(failures)
    test_end_to_end_disposition(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — council module not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — diff-routing, plan/cost, disposition, and end-to-end trust-gating hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
