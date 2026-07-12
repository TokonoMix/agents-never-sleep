#!/usr/bin/env python3
"""Plan 2 wiring: the hard-category widening end-to-end — offer record snapshots the effective set,
resolve re-checks against THAT set (not fresh config), and a hard-category resolution is forced to
DONE_LOW_CONFIDENCE + daylight review even with a green gate."""
# py3.9 compat (requires-python >= 3.9): PEP 604 annotations must not evaluate at def time.
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.report import build_report  # noqa: E402
from agents_never_sleep.run import _Context, build_parser  # noqa: E402
from agents_never_sleep.state import OutcomeState, TicketOutcome  # noqa: E402
from agents_never_sleep.tickets import Ticket  # noqa: E402


def test_offer_record_snapshots_effective_set(failures):
    with tempfile.TemporaryDirectory() as d:
        led = AttemptLedger(os.path.join(d, "ledger.json"))
        led.open_f5_offer("T1", attempt_id="a1", category="db_schema_or_migration",
                          has_safety_net=True, foundational=True,
                          consensus_assisted_categories=["db_schema_or_migration"])
        offer = led.get_f5_offer("T1")
        if offer.get("consensus_assisted_categories") != ["db_schema_or_migration"]:
            failures.append(f"offer must snapshot the effective set; got {offer!r}")


def test_resolve_park_rechecks_recorded_set_not_fresh_config(failures):
    """A RESOLVE verdict on a hard category the RECORDED offer opted in must route to proceed even
    if the live project set no longer contains it (offer record is the trusted anchor)."""
    from agents_never_sleep import f5
    from agents_never_sleep.decide import Decision, Action
    # Recorded offer opted db_schema in; a fresh eligible() check with the recorded set is True,
    # with an empty set is False — proving resolve_park must use the recorded set.
    d = Decision(Action.PARK, "why", category="db_schema_or_migration", foundational=True)
    if not f5.eligible(d, has_safety_net=True, already_attempted=False,
                       consensus_assisted_categories=["db_schema_or_migration"]):
        failures.append("recorded set should make the hard category eligible at resolve time")
    if f5.eligible(d, has_safety_net=True, already_attempted=False,
                   consensus_assisted_categories=[]):
        failures.append("empty set (stale fresh config) should NOT — proves the anchor matters")


def _build_orch(work):
    """Minimal Orchestrator fixture — mirrors test_f5_wiring.py's `_build` but returns the
    Orchestrator directly (resolve_park is an Orchestrator method, no StepDriver needed here)."""
    from agents_never_sleep.gates import GateRunner
    from agents_never_sleep.ledger import AttemptLedger
    from agents_never_sleep.orchestrator import Orchestrator
    from agents_never_sleep.state import OutcomeStore

    repo = os.path.join(work, "repo")
    sandbox = os.path.join(HERE, "sandbox")
    if os.path.isdir(sandbox):
        shutil.copytree(sandbox, repo)
    else:
        os.makedirs(repo, exist_ok=True)
    state_dir = os.path.join(work, "state")
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    store = OutcomeStore(state_dir)
    return Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=os.path.join(work, "art"), unattended=True, ledger=ledger)


def _build_driver(work, tickets, consensus_assisted_categories=None):
    """StepDriver fixture for the _f5_offer wiring itself (Task 7) — mirrors
    acceptance/test_f5_wiring.py's `_build`, plus the `consensus_assisted_categories` project-set
    knob (Task 6) that `_build` doesn't expose."""
    from agents_never_sleep.driver import StepDriver
    from agents_never_sleep.gates import GateRunner
    from agents_never_sleep.ledger import AttemptLedger
    from agents_never_sleep.orchestrator import Orchestrator
    from agents_never_sleep.state import OutcomeStore

    repo = os.path.join(work, "repo")
    sandbox = os.path.join(HERE, "sandbox")
    if os.path.isdir(sandbox):
        shutil.copytree(sandbox, repo)
    else:
        os.makedirs(repo, exist_ok=True)
    state_dir = os.path.join(work, "state")
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    store = OutcomeStore(state_dir)
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=os.path.join(work, "art"), unattended=True, ledger=ledger,
                        consensus_assisted_categories=consensus_assisted_categories)
    return StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                      report_path=os.path.join(os.path.dirname(state_dir), "report.md"))


def _migration_ticket(tid: str, meta: dict | None = None) -> Ticket:
    return Ticket(id=tid, title="Add a migration",
                 body="Run a database schema migration to add a column", meta=meta or {}, path="")


def test_driver_offers_f5_for_opted_in_hard_category(failures):
    """Task 7 Step 3: a project set of ["db_schema_or_migration"] must make a db-migration ticket
    eligible for F5, and the durable offer record must snapshot exactly that effective set."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-drv-hard-") as work:
        ticket = _migration_ticket("t-hard-offer")
        drv = _build_driver(work, [ticket], consensus_assisted_categories=["db_schema_or_migration"])
        result = drv.next_ticket()
        if result.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[drv-hard] opted-in hard category should get an F5 offer, got {result}")
            return
        offer = drv.orch.ledger.get_f5_offer("t-hard-offer")
        if offer is None or offer.get("consensus_assisted_categories") != ["db_schema_or_migration"]:
            failures.append(f"[drv-hard] ledger offer must snapshot the effective set, got {offer}")


def test_driver_parks_hard_category_when_project_set_empty(failures):
    """Task 7 Step 3: with the project set empty and no ticket override, the same hard-category
    ticket must fall through to a normal park — no F5 offer, no ledger record."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-drv-off-") as work:
        ticket = _migration_ticket("t-hard-off")
        drv = _build_driver(work, [ticket], consensus_assisted_categories=[])
        result = drv.next_ticket()
        if result.get("status") == "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[drv-off] empty project set must not offer F5, got {result}")
        if drv.orch.ledger.get_f5_offer("t-hard-off") is not None:
            failures.append("[drv-off] no F5 offer record should exist when the project set is empty")


def test_driver_ticket_opt_out_skips_offer_even_for_requirement_meaning(failures):
    """Task 7 Step 3: an explicit ticket-level `consensus_assisted: false` must skip the offer
    entirely — even for requirement_meaning, which is otherwise ALWAYS eligible."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-drv-optout-") as work:
        ticket = Ticket(id="t-optout", title="Add a widget",
                        body="Add a widget — unclear which kind of widget?",
                        meta={"consensus_assisted": False}, path="")
        drv = _build_driver(work, [ticket])
        result = drv.next_ticket()
        if result.get("status") == "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[drv-optout] ticket opt-out must skip the F5 offer, got {result}")
        if drv.orch.ledger.get_f5_offer("t-optout") is not None:
            failures.append("[drv-optout] no F5 offer record should exist after an explicit opt-out")


def test_offer_instructions_are_category_aware(failures):
    """Safety gap (Task 7 review): the PARK_CONSENSUS_ELIGIBLE offer's `instructions` must be routed
    by category. A hard-category offer must NAME `--defect-found` (so a prose-only defect can veto
    instead of being submitted as `--resolved`) and must NOT frame the requirement as 'ambiguous';
    a requirement_meaning offer must keep the disambiguation framing."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-instr-hard-") as work:
        ticket = _migration_ticket("t-hard-instr")
        drv = _build_driver(work, [ticket], consensus_assisted_categories=["db_schema_or_migration"])
        result = drv.next_ticket()
        if result.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[instr-hard] opted-in hard category should get an F5 offer, got {result}")
            return
        instr = result.get("instructions", "")
        if "--defect-found" not in instr:
            failures.append(f"[instr-hard] hard-category instructions must name --defect-found, got {instr!r}")
        if "requirement meaning is ambiguous" in instr:
            failures.append("[instr-hard] hard-category instructions must NOT call the requirement ambiguous")

    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-instr-req-") as work:
        req = Ticket(id="t-req-instr", title="Add a widget",
                     body="Add a widget — unclear which kind of widget?", meta={}, path="")
        drv = _build_driver(work, [req])
        result = drv.next_ticket()
        if result.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[instr-req] requirement_meaning should get an F5 offer, got {result}")
            return
        if "requirement meaning is ambiguous" not in result.get("instructions", ""):
            failures.append("[instr-req] requirement_meaning instructions must keep the ambiguity framing")


def test_resolve_park_hard_category_defect_found_stays_parked(failures):
    """The end-to-end wiring of Task 6 Step 4/4b: a hard-category offer whose RECORDED
    consensus_assisted_categories opted it in, with a RESOLVE verdict that also FOUND A DEFECT,
    must route through interpret_soundness_verdict and stay parked — proving resolve_park routes
    the interpreter by the RECORDED category, not requirement_meaning-only."""
    from agents_never_sleep import f5
    from agents_never_sleep.decide import classify
    from agents_never_sleep.orchestrator import ProceedToken
    from agents_never_sleep.state import OutcomeState, TicketOutcome
    from agents_never_sleep.tickets import Ticket

    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-hard-") as work:
        orch = _build_orch(work)
        ticket = Ticket(id="t-hard-defect", title="x",
                        body="Run a database schema migration to add a column", meta={}, path="")
        decision = classify(f"{ticket.title}\n{ticket.body}", unattended=True, has_safety_net=True)
        if decision.category != "db_schema_or_migration":
            failures.append(f"fixture ticket did not classify as a hard category: {decision}")
            return
        offer = {"attempt_id": "a-hard-defect", "category": decision.category,
                "has_safety_net": True, "foundational": decision.foundational,
                "status": "offered", "consensus_assisted_categories": [decision.category]}
        # resolved=True + evidence cited + zero dissent would RESOLVE under interpret_verdict, but
        # defect_found=True must veto to KEEP_PARKED under interpret_soundness_verdict.
        verdict = f5.F5Verdict(resolved=True, chosen_reading="SQL injection at line 42",
                              evidence="line 42 concatenates unsanitized user input",
                              dissent_count=0, synthesis_text="clear vulnerability",
                              defect_found=True)
        result = orch.resolve_park(ticket, offer, verdict)
        if isinstance(result, ProceedToken):
            failures.append("a defect_found verdict on a hard category must NOT reach begin_proceed")
        if not isinstance(result, TicketOutcome) or result.state != OutcomeState.PARKED_FOUNDATIONAL:
            failures.append(f"defect_found hard category must stay parked foundational, got {result}")

        # Contrast: the SAME offer/category, resolved cleanly with NO defect, structurally eligible
        # via the recorded set, must RESOLVE into begin_proceed — proving the veto is defect-specific,
        # not a blanket block on hard categories once opted in.
        ticket2 = Ticket(id="t-hard-clean", title="x",
                         body="Run a database schema migration to add a column", meta={}, path="")
        clean_verdict = f5.F5Verdict(resolved=True, chosen_reading="additive, reversible migration",
                                    evidence="migration adds a nullable column only",
                                    dissent_count=0, synthesis_text="straightforwardly additive",
                                    defect_found=False)
        clean_result = orch.resolve_park(ticket2, offer, clean_verdict)
        if not isinstance(clean_result, ProceedToken):
            failures.append(f"a clean soundness verdict on the RECORDED opted-in set must RESOLVE, "
                            f"got {clean_result}")


def test_resolve_park_requirement_meaning_still_uses_interpret_verdict(failures):
    """The `requirement_meaning` branch must still route through interpret_verdict (the
    disambiguation gate), not interpret_soundness_verdict — defect_found=True is meaningless there
    (the agent never sets it on that path) and must not accidentally veto a real disambiguation."""
    from agents_never_sleep import f5
    from agents_never_sleep.decide import classify
    from agents_never_sleep.orchestrator import ProceedToken

    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-reqmeaning-") as work:
        orch = _build_orch(work)
        from agents_never_sleep.tickets import Ticket
        ticket = Ticket(id="t-reqmeaning", title="Add a widget",
                        body="Add a widget — unclear which kind of widget?", meta={}, path="")
        decision = classify(f"{ticket.title}\n{ticket.body}", unattended=True, has_safety_net=True)
        if decision.category != "requirement_meaning":
            failures.append(f"fixture ticket did not classify as requirement_meaning: {decision}")
            return
        offer = {"attempt_id": "a-reqmeaning", "category": decision.category,
                "has_safety_net": True, "foundational": decision.foundational, "status": "offered"}
        verdict = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                              evidence="components/Badge.tsx already renders status",
                              dissent_count=0, synthesis_text="clearly reading A")
        result = orch.resolve_park(ticket, offer, verdict)
        if not isinstance(result, ProceedToken):
            failures.append(f"requirement_meaning RESOLVE must still route via interpret_verdict "
                            f"and reach begin_proceed, got {result}")


def test_hard_category_resolve_forces_daylight_review_e2e(failures):
    """Spec §5 (Task 8) — the load-bearing safety test. A hard-category (db_schema_or_migration)
    RESOLVE must be forced to DONE_LOW_CONFIDENCE + a daylight-review reason even with a green
    gate, and this must survive the REAL `complete` finalize path (token round-trip through
    _save_pending/_load_pending), not a direct _finalize_impl call. Control: the SAME flow with a
    requirement_meaning ticket stays plain DONE."""
    from agents_never_sleep import f5
    from agents_never_sleep.state import OutcomeState

    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-daylight-hard-") as work:
        ticket = _migration_ticket("t-daylight-hard")
        drv = _build_driver(work, [ticket], consensus_assisted_categories=["db_schema_or_migration"])
        offer = drv.next_ticket()
        if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[daylight-hard] expected an F5 offer, got {offer}")
            return
        verdict = f5.F5Verdict(resolved=True, chosen_reading="additive, reversible migration",
                              evidence="migration adds a nullable column only", dissent_count=0,
                              synthesis_text="straightforwardly additive", defect_found=False)
        resumed = drv.resolve_park("t-daylight-hard", offer["attempt_id"], verdict)
        if resumed.get("status") != "PROCEED":
            failures.append(f"[daylight-hard] RESOLVE must hand back a PROCEED payload, got {resumed}")
            return
        with open(os.path.join(drv.orch.repo_dir, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# F5-resolved migration note\n")
        done = drv.complete_ticket(attempted="implemented the migration")
        if done.get("state") != OutcomeState.DONE_LOW_CONFIDENCE.value:
            failures.append(f"[daylight-hard] hard-category resolution with a green gate must "
                            f"floor to DONE_LOW_CONFIDENCE, got {done}")
        if "F5 resolved a hard-PARK category" not in done.get("why", ""):
            failures.append(f"[daylight-hard] why must carry the hard-category daylight-review "
                            f"reason, got {done.get('why')!r}")

    # Control: requirement_meaning RESOLVE with a green gate stays plain DONE (unchanged behaviour).
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-daylight-control-") as work:
        ticket = Ticket(id="t-daylight-control", title="Add a widget",
                        body="Add a widget — unclear which kind of widget?", meta={}, path="")
        drv = _build_driver(work, [ticket])
        offer = drv.next_ticket()
        if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[daylight-control] expected an F5 offer, got {offer}")
            return
        verdict = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                              evidence="components/Badge.tsx already renders status",
                              dissent_count=0, synthesis_text="clearly reading A")
        resumed = drv.resolve_park("t-daylight-control", offer["attempt_id"], verdict)
        if resumed.get("status") != "PROCEED":
            failures.append(f"[daylight-control] RESOLVE must hand back a PROCEED payload, got {resumed}")
            return
        with open(os.path.join(drv.orch.repo_dir, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# F5-resolved widget note\n")
        done = drv.complete_ticket(attempted="implemented reading A")
        if done.get("state") != OutcomeState.DONE.value:
            failures.append(f"[daylight-control] requirement_meaning resolution must stay plain "
                            f"DONE, got {done}")
        if "F5 resolved a hard-PARK category" in done.get("why", ""):
            failures.append(f"[daylight-control] requirement_meaning must NOT carry the "
                            f"hard-category daylight reason, got {done.get('why')!r}")


def test_force_daylight_review_composes_with_existing_low_confidence(failures):
    """Compose-when-already-flagged (brief Step 1, second assertion): when the diff independently
    already produces DONE_LOW_CONFIDENCE (here via credits_degrade, standing in for a
    council/specialist daylight flag), a force_daylight_review token must STILL fold in the
    hard-category reason. Proves the `state in (DONE, DONE_LOW_CONFIDENCE)` guard in
    _finalize_impl, not a `== DONE` gate — a DONE-only gate would silently drop this audit fact on
    the highest-risk path. Exercised via the real finalize_after_edit path (not a raw dataclass
    poke), with the token built directly rather than through resolve_park (the fixture cannot
    easily drive a council/specialist flag alongside an F5 offer in one pass)."""
    from agents_never_sleep.orchestrator import ProceedToken
    from agents_never_sleep.state import OutcomeState

    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-compose-") as work:
        orch = _build_orch(work)
        ticket = _migration_ticket("t-compose")
        token = orch.begin_proceed(ticket)
        if not isinstance(token, ProceedToken):
            failures.append(f"[compose] begin_proceed must return a ProceedToken, got {token}")
            return
        token.force_daylight_review = "F5 resolved a hard-PARK category: db_schema_or_migration"
        with open(os.path.join(orch.repo_dir, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# compose-case trivial edit\n")
        outcome = orch.finalize_after_edit(ticket, token, "trivial edit", credits_degrade=True)
        if outcome.state != OutcomeState.DONE_LOW_CONFIDENCE:
            failures.append(f"[compose] credits_degrade must floor to DONE_LOW_CONFIDENCE, "
                            f"got {outcome.state}")
        if "F5 resolved a hard-PARK category" not in outcome.why:
            failures.append(f"[compose] the hard-category reason must survive composing on top "
                            f"of an ALREADY DONE_LOW_CONFIDENCE state (== DONE gate would drop "
                            f"it), got why={outcome.why!r}")


def _context_for(repo, consensus_assisted_categories):
    """Build a real `_Context` (the object run.py's cmd_next/etc construct) over a minimal saved
    config — mirrors test_f5_wiring.py's `_write_config`, plus the `classify.
    consensus_assisted_categories` knob Task 9 wires through. `existing = load_config(...)` finds
    this file, so no preflight probe runs."""
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    cfg = {
        "schema_version": 1,
        "classify": {"overrides": {}, "consensus_assisted_categories": consensus_assisted_categories},
    }
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    args = build_parser().parse_args(["next", "--repo", repo, "--tickets", "tickets"])
    return _Context(args)


def test_run_context_passes_project_set_to_orchestrator(failures):
    """Task 9: `classify.consensus_assisted_categories` from the saved config must reach the
    Orchestrator the run entry builds — completing the offer-time path end-to-end."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-runctx-") as work:
        repo = os.path.join(work, "repo")
        os.makedirs(repo, exist_ok=True)
        ctx = _context_for(repo, ["db_schema_or_migration"])
        if ctx.orch.consensus_assisted_categories != ["db_schema_or_migration"]:
            failures.append(f"[run-ctx] Orchestrator.consensus_assisted_categories must mirror the "
                            f"config, got {ctx.orch.consensus_assisted_categories!r}")


def test_run_context_validates_consensus_config_fail_fast(failures):
    """Task 9: a typo'd/unknown category must abort at `_Context` construction (config-load time),
    before any ticket work — not surface later as a silent no-op deep in the offer path."""
    with tempfile.TemporaryDirectory(prefix="ue-f5plan2-runctx-typo-") as work:
        repo = os.path.join(work, "repo")
        os.makedirs(repo, exist_ok=True)
        try:
            _context_for(repo, ["db_schema_or_migratoin"])  # deliberate typo
        except ValueError:
            pass
        else:
            failures.append("[run-ctx] a typo'd category must raise ValueError at startup "
                            "(validate_consensus_config not wired at config-load)")


def _resolve_park_argv(*extra):
    return ["resolve-park", "--ticket-id", "T1", "--attempt-id", "a1",
            "--resolved", "--chosen-reading", "x", *extra]


def test_resolve_park_defect_found_flag_defaults_false(failures):
    parser = build_parser()
    args = parser.parse_args(_resolve_park_argv())
    if getattr(args, "defect_found", "MISSING") is not False:
        failures.append(f"--defect-found must default to False, got {getattr(args, 'defect_found', 'MISSING')!r}")

    from agents_never_sleep.f5 import F5Verdict
    verdict = F5Verdict(resolved=args.resolved, chosen_reading=args.chosen_reading or "",
                        evidence=args.evidence or "", dissent_count=args.dissent_count,
                        synthesis_text=args.synthesis_text or "",
                        defect_found=getattr(args, "defect_found", False))
    if verdict.defect_found is not False:
        failures.append(f"F5Verdict.defect_found must be False when --defect-found omitted, got {verdict.defect_found!r}")


def test_resolve_park_defect_found_flag_sets_true(failures):
    parser = build_parser()
    args = parser.parse_args(_resolve_park_argv("--defect-found"))
    if args.defect_found is not True:
        failures.append(f"--defect-found must set True, got {args.defect_found!r}")

    from agents_never_sleep.f5 import F5Verdict
    verdict = F5Verdict(resolved=args.resolved, chosen_reading=args.chosen_reading or "",
                        evidence=args.evidence or "", dissent_count=args.dissent_count,
                        synthesis_text=args.synthesis_text or "",
                        defect_found=getattr(args, "defect_found", False))
    if verdict.defect_found is not True:
        failures.append(f"F5Verdict.defect_found must be True when --defect-found passed, got {verdict.defect_found!r}")


def test_report_shows_hard_category_f5_declined_line(failures):
    """Task 11 (spec §6): Plan 1's declined-consensus report line is keyed off the
    category-agnostic `review_coverage == "f5-attempted-declined"` tag that resolve_park sets for
    EVERY declined park (see resolve_park's fall-through at the bottom of orchestrator.py, which
    runs regardless of `offer.get("category")`) — so the block fires for a hard-category park too.
    But the line's WORDING hardcoded "the requirement-meaning ambiguity", which is wrong/misleading
    for a hard-category (e.g. db_schema_or_migration) park. Assert the block fires AND names the
    recorded category, not a false requirement-meaning claim."""
    o = TicketOutcome(ticket_id="t-hard-declined", state=OutcomeState.PARKED_FOUNDATIONAL,
                      why="hard-PARK category: db_schema_or_migration — F5 consensus tried "
                          "and declined: no cited evidence",
                      category="db_schema_or_migration", review_coverage="f5-attempted-declined")
    text = build_report([o], run_label="t")
    if "F5 consensus attempt" not in text:
        failures.append("[report-hard] F5-declined block missing for a hard-category park")
    if "t-hard-declined" not in text:
        failures.append("[report-hard] declined hard-category ticket id missing from the report")
    if "db_schema_or_migration" not in text:
        failures.append(f"[report-hard] declined-consensus line must name the recorded hard "
                        f"category, got: {text!r}")
    if "requirement-meaning ambiguity" in text:
        failures.append("[report-hard] declined-consensus line must not claim a "
                        "requirement-meaning ambiguity for a hard-category park")


def main():
    failures = []
    test_offer_record_snapshots_effective_set(failures)
    test_resolve_park_rechecks_recorded_set_not_fresh_config(failures)
    test_driver_offers_f5_for_opted_in_hard_category(failures)
    test_driver_parks_hard_category_when_project_set_empty(failures)
    test_driver_ticket_opt_out_skips_offer_even_for_requirement_meaning(failures)
    test_offer_instructions_are_category_aware(failures)
    test_resolve_park_hard_category_defect_found_stays_parked(failures)
    test_resolve_park_requirement_meaning_still_uses_interpret_verdict(failures)
    test_resolve_park_defect_found_flag_defaults_false(failures)
    test_resolve_park_defect_found_flag_sets_true(failures)
    test_hard_category_resolve_forces_daylight_review_e2e(failures)
    test_force_daylight_review_composes_with_existing_low_confidence(failures)
    test_run_context_passes_project_set_to_orchestrator(failures)
    test_run_context_validates_consensus_config_fail_fast(failures)
    test_report_shows_hard_category_f5_declined_line(failures)
    if failures:
        print("RESULT: ❌")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
