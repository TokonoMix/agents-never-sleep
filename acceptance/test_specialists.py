#!/usr/bin/env python3
"""Specialist-reviewer test — deterministic lens selection + the daylight-review fold, end-to-end.

Specialist lenses (architect+security default; tenant/mobile/ux/i18n/perf/seo conditional on the
diff) are the AGENT's paid tokonomix reviews; the harness owns only the DETERMINISTIC half:
  * SELECT which lenses a change needs, from the ACTUAL diff;
  * RECORD that coverage on the outcome;
  * FOLD a high-blast-radius concern (architect/security/tenant-safety) reported by the agent into
    the SAME advisory trust-gating the council uses — DONE_LOW_CONFIDENCE + needs-daylight-review,
    never a revert/block;
  * share the council's per-night SPEND brake (specialist € counts, without burning a council call).

All proven with NO live LLM call. Exit 0 = GREEN.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import specialists  # noqa: E402
from agents_never_sleep.specialists import SpecialistRole  # noqa: E402
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
    # council OFF on purpose: proves specialists are independent and exercises the files-None branch.
    "council": {"enabled": False, "markup_factor": 2.5},
    "specialists": {
        "enabled": True,
        "default_model": "gpt-5.4-mini",
        "model_by_role": {"security": "claude-opus-4-8", "architect": "gpt-5.4"},
        "est_prompt_tokens": 2500, "max_tokens": 700,
    },
}


def test_select(failures):
    if specialists.select_from_diff(["README.md", "docs/g.md"], "words"):
        failures.append("[select] docs-only should select no lenses")
    base = specialists.select_from_diff(["src/util.py"], "def f(): return 1")
    if SpecialistRole.ARCHITECT not in base or SpecialistRole.SECURITY not in base:
        failures.append("[select] non-trivial change must always get architect+security")
    if len(base) != 2:
        failures.append(f"[select] routine code should be JUST architect+security, got {base}")
    tenant = specialists.select_from_diff(["src/db.py"], "WHERE company_id = ?")
    if SpecialistRole.TENANT not in tenant:
        failures.append("[select] tenant signal in diff should add tenant-safety lens")
    css = specialists.select_from_diff(["ui/app.css"], "@media (max-width: 600px) {}")
    if SpecialistRole.MOBILE not in css:
        failures.append("[select] css/@media should add mobile-responsive lens")


def test_parse_and_daylight(failures):
    parsed = specialists.parse_roles(["security", "ARCHITECT", "tenant-safety", "bogus", ""])
    if set(parsed) != {SpecialistRole.SECURITY, SpecialistRole.ARCHITECT, SpecialistRole.TENANT}:
        failures.append(f"[parse] value/name parsing or unknown-skip wrong: {parsed}")
    if specialists.parse_roles(["security", "security"]) != [SpecialistRole.SECURITY]:
        failures.append("[parse] should de-dupe repeated roles")
    # only architect/security/tenant force daylight; perf/seo/etc do not
    dl = specialists.daylight_concerns(["performance", "security", "seo", "tenant-safety"])
    if set(dl) != {SpecialistRole.SECURITY, SpecialistRole.TENANT}:
        failures.append(f"[daylight] should keep only high-blast-radius lenses, got {dl}")
    if specialists.daylight_concerns(["performance", "i18n", "seo"]):
        failures.append("[daylight] low-blast-radius concerns must NOT force daylight")
    if specialists.daylight_concerns([]) or specialists.daylight_concerns(None):
        failures.append("[daylight] empty/None concerns must yield no daylight roles")


def test_hint_plan_enabled(failures):
    hint = specialists.pre_work_hint("add a payment endpoint touching tenant_id")
    if SpecialistRole.ARCHITECT not in hint or SpecialistRole.SECURITY not in hint:
        failures.append("[hint] architect+security must always be hinted")
    if SpecialistRole.TENANT not in hint:
        failures.append("[hint] tenant keyword should add tenant-safety to the hint")
    p = specialists.plan(CFG, [SpecialistRole.ARCHITECT, SpecialistRole.SECURITY])
    if p.est_cost_eur <= 0:
        failures.append(f"[plan] est cost should be > 0, got {p.est_cost_eur}")
    if p.model_by_role.get("security") != "claude-opus-4-8":
        failures.append("[plan] per-role model override not applied")
    if "specialists" not in p.summary_line() or "€" not in p.summary_line():
        failures.append("[plan] summary line missing label/cost")
    if specialists.plan(CFG, []).summary_line().find("none") < 0:
        failures.append("[plan] empty roles summary should say none")
    if not specialists.enabled(CFG):
        failures.append("[enabled] should be on with specialists+tokonomix enabled")
    if specialists.enabled({"specialists": {"enabled": False}}):
        failures.append("[enabled] should be off when specialists disabled")
    if specialists.enabled({"specialists": {"enabled": True},
                            "integrations": {"tokonomix": {"enabled": False}}}):
        failures.append("[enabled] should be off when tokonomix disabled")


def test_end_to_end(failures):
    """Drive the real StepDriver with specialists enabled: a diff whose agent reports a SECURITY
    concern lands DONE_LOW_CONFIDENCE + needs-daylight; a benign change stays DONE but still records
    specialist coverage; the reported € feeds the spend brake without burning a council call."""
    work = tempfile.mkdtemp(prefix="ue-spec-")
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

    def edit_file(name, body):
        with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    saw_hint = False
    spend = None  # captured in-loop: progress is run-scoped and wiped at the terminal `next`
    for _ in range(20):
        res = driver.next_ticket()
        if res["status"] != "PROCEED":
            break
        tid = res["ticket"]["id"]
        if tid == "ticket-01-trivial":
            if "specialists" in res and res["specialists"].get("roles"):
                saw_hint = True
            edit_file("auth.py", "def authorize(jwt_token):\n    return bool(jwt_token)\n")
            driver.complete_ticket(attempted="auth", specialist_concerns=["security"],
                                   specialist_cost_eur=0.42)
            spend = driver._load_progress()  # snapshot before DRAINED resets it
        else:
            edit_file("note.py", "# benign helper\nVALUE = 1\n")
            driver.complete_ticket(attempted="note")  # no concern

    if not saw_hint:
        failures.append("[e2e] PROCEED payload missing specialists hint with roles")

    o1 = store.read("ticket-01-trivial")
    if o1 is None or o1.state != OutcomeState.DONE_LOW_CONFIDENCE:
        failures.append(f"[e2e] security concern should be DONE_LOW_CONFIDENCE, got "
                        f"{getattr(o1, 'state', None)}")
    else:
        cov = o1.review_coverage or ""
        if "NEEDS-DAYLIGHT-REVIEW" not in cov or "security" not in cov or "specialists:" not in cov:
            failures.append(f"[e2e] daylight/coverage tag missing on concern outcome: {cov!r}")

    o3 = store.read("ticket-03-redgate")
    if o3 is None or o3.state != OutcomeState.DONE:
        failures.append(f"[e2e] benign change should stay DONE, got {getattr(o3, 'state', None)}")
    elif "specialists:" not in (o3.review_coverage or ""):
        failures.append(f"[e2e] specialist coverage not recorded on benign change: "
                        f"{getattr(o3, 'review_coverage', None)!r}")

    # spend brake: the reported € accrued, but no council call was consumed.
    if spend is None:
        failures.append("[e2e] never captured the post-concern progress snapshot")
    else:
        if spend.get("council_cost_eur", 0.0) < 0.42:
            failures.append(f"[e2e] specialist cost not folded into spend brake: {spend}")
        if spend.get("council_calls", 0) != 0:
            failures.append(f"[e2e] specialist review must NOT burn a council call: {spend}")


def test_end_to_end_with_council(failures):
    """The path the slice actually wires: BOTH council and specialists enabled on the SAME diff. A
    HEAVY-risk diff with no council verdict ALREADY needs daylight (council); when a security
    specialist ALSO flags it, the disposition must stay DONE_LOW_CONFIDENCE and the prose must carry
    BOTH diagnostics (the council reason must not be clobbered), and the diff is computed once."""
    cfg = dict(CFG)
    cfg["council"] = {
        "enabled": True, "markup_factor": 2.5, "est_prompt_tokens": 3000,
        "light": {"proposers": ["a", "b"], "judges": ["j1"], "mode": "consensus", "max_tokens": 800},
        "heavy": {"proposers": ["a", "b", "c"], "judges": ["j1", "j2"], "mode": "consensus",
                  "max_tokens": 1200},
        "prices_cents_per_mtok": {"a": [50, 250], "b": [25, 150], "c": [25, 38],
                                  "j1": [50, 250], "j2": [25, 150]},
    }
    work = tempfile.mkdtemp(prefix="ue-spec-co-")
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
                        report_path=os.path.join(work, "report.md"), config=cfg)

    for _ in range(20):
        res = driver.next_ticket()
        if res["status"] != "PROCEED":
            break
        tid = res["ticket"]["id"]
        if tid == "ticket-01-trivial":
            # HEAVY diff (auth path) — council not run (no verdict) -> council daylight; plus a
            # security specialist concern. Both daylight reasons should survive.
            with open(os.path.join(repo, "auth.py"), "w", encoding="utf-8") as fh:
                fh.write("def authorize(jwt_token):\n    return bool(jwt_token)\n")
            driver.complete_ticket(attempted="auth", specialist_concerns=["security"])
        else:
            with open(os.path.join(repo, "note.py"), "a", encoding="utf-8") as fh:
                fh.write("\n# note\n")
            driver.complete_ticket(attempted="note", council_verdict="pass")

    o1 = store.read("ticket-01-trivial")
    if o1 is None or o1.state != OutcomeState.DONE_LOW_CONFIDENCE:
        failures.append(f"[e2e+council] expected DONE_LOW_CONFIDENCE, got {getattr(o1,'state',None)}")
        return
    cov, why = (o1.review_coverage or ""), (o1.why or "")
    if "specialists:" not in cov or "NEEDS-DAYLIGHT-REVIEW" not in cov:
        failures.append(f"[e2e+council] coverage missing specialist/daylight tag: {cov!r}")
    if "council" not in why or "specialist" not in why:
        failures.append(f"[e2e+council] why clobbered — must carry BOTH diagnostics: {why!r}")
    if "council" not in (o1.human_action_required or "") or \
            "specialist" not in (o1.human_action_required or ""):
        failures.append(f"[e2e+council] human_action lost a diagnostic: {o1.human_action_required!r}")


def main() -> int:
    failures = []
    test_select(failures)
    test_parse_and_daylight(failures)
    test_hint_plan_enabled(failures)
    test_end_to_end(failures)
    test_end_to_end_with_council(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — specialist reviewers not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — lens selection, parse/daylight filter, hint/plan, and end-to-end "
          "coverage + daylight-fold + spend-brake all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
