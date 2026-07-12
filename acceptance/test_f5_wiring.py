#!/usr/bin/env python3
"""F5 WIRING — the runtime interrupt/resume path that activates agents_never_sleep/f5.py.

test_f5.py already proves the PURE core (narrow eligibility + downgrade-only interpretation).
This suite proves the WIRING around it: the driver's new PARK_CONSENSUS_ELIGIBLE interrupt, the
`resolve-park` CLI round-trip (both RESOLVE->PROCEED and KEEP_PARKED->declined-park), the durable
already-attempted flag surviving a simulated crash, the per-run F5 call ceiling, and the morning
report's declined-consensus visibility line.

Exit 0 = GREEN.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

import shutil

from agents_never_sleep import f5                                        # noqa: E402
from agents_never_sleep import driver as driver_mod                      # noqa: E402
from agents_never_sleep.decide import classify                           # noqa: E402
from agents_never_sleep.driver import StepDriver                         # noqa: E402
from agents_never_sleep.gates import GateRunner                          # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator, ProceedToken   # noqa: E402
from agents_never_sleep.report import build_report                       # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore, TicketOutcome  # noqa: E402
from agents_never_sleep.tickets import Ticket                            # noqa: E402


def test_ledger_f5_attempted(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-ledger-")
    led = AttemptLedger(os.path.join(work, "ledger.json"))
    if led.f5_attempted("t-1"):
        failures.append("[ledger] a fresh ticket must not be f5_attempted")
    if led.get_f5_offer("t-1") is not None:
        failures.append("[ledger] a fresh ticket must have no offer record")
    led.open_f5_offer("t-1", attempt_id="a-1", category="requirement_meaning",
                      has_safety_net=True, foundational=False)
    if not led.f5_attempted("t-1"):
        failures.append("[ledger] open_f5_offer did not persist f5_attempted in-process")
    rec = led.get_f5_offer("t-1")
    if rec is None or rec.get("attempt_id") != "a-1" or rec.get("category") != "requirement_meaning":
        failures.append(f"[ledger] get_f5_offer did not return the recorded fields: {rec}")
    if rec.get("status") != "offered":
        failures.append(f"[ledger] a new offer must start life as status='offered', got {rec}")
    # a FRESH AttemptLedger over the SAME path (simulates a new process) must see it too.
    led2 = AttemptLedger(os.path.join(work, "ledger.json"))
    if not led2.f5_attempted("t-1"):
        failures.append("[ledger] f5_attempted did not survive a reload from disk")
    rec2 = led2.get_f5_offer("t-1")
    if rec2 is None or rec2.get("attempt_id") != "a-1":
        failures.append(f"[ledger] the offer record did not survive a reload from disk: {rec2}")
    if led2.f5_attempted("t-2"):
        failures.append("[ledger] an unrelated ticket must not read as attempted")
    led2.consume_f5_offer("t-1")
    if led2.get_f5_offer("t-1").get("status") != "consumed":
        failures.append("[ledger] consume_f5_offer must flip the record's status to 'consumed'")


def _ambiguous_ticket(tid: str) -> Ticket:
    # Matches decide.AMBIGUITY_SIGNALS ("which kind"/"?") and no HARD_PARK_CATEGORIES keyword, so
    # classify() routes it to the requirement_meaning PARK branch (consensus_resolvable=True) — the
    # ONLY category F5 is ever eligible on (see f5.py / acceptance/test_f5.py).
    return Ticket(id=tid, title="Add a widget", body="Add a widget — unclear which kind of widget?",
                 meta={}, path="")


def _build(repo, state_dir, artifacts_dir, tickets, config=None):
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    store = OutcomeStore(state_dir)
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=artifacts_dir, unattended=True, ledger=ledger)
    return StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                      report_path=os.path.join(os.path.dirname(state_dir), "report.md"),
                      config=config)


# Minimal council-enabled config (mirrors acceptance/test_budget.py's CFG_STOP) — used to prove
# INT-2412 parity: resolve-park's PROCEED path must attach the SAME hints as next_ticket's.
_CFG_COUNCIL_ON = {
    "budget": {
        "per_night_euro_cap": 2.0,
        "max_council_calls_per_night": 5,
        "balance_threshold_euro": 1.0,
        "on_credits_exhausted": "stop",
    },
    "integrations": {"tokonomix": {"enabled": True}},
    "council": {
        "enabled": True,
        "light": {"proposers": ["a"], "judges": ["j"], "mode": "consensus", "max_tokens": 900},
        "heavy": {"proposers": ["a", "b"], "judges": ["j"], "mode": "consensus", "max_tokens": 1400},
        "prices_cents_per_mtok": {"a": [500, 2500], "b": [250, 1500], "j": [300, 1500]},
        "est_prompt_tokens": 3000,
    },
}


def test_resolve_park_proceed_has_council_parity(failures):
    """INT-2412: an F5 RESOLVE->PROCEED payload must carry the SAME pre-work hints as a normal
    next_ticket PROCEED — today resolve_park builds its PROCEED payload inline and skips
    _attach_council_hint (+specialist/onboarding/scratchpad), so an F5-resolved ticket hands the
    agent a lighter payload than an identical non-F5 ticket. This is the RED test for that gap."""
    work = tempfile.mkdtemp(prefix="ue-f5-parity-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)

    # Driver A: a NON-ambiguous ticket goes straight through next_ticket's PROCEED path (the
    # reference behaviour this fix must match).
    normal_ticket = Ticket(id="t-normal", title="Fix a typo in the README",
                           body="Correct a spelling mistake in the README.", meta={}, path="")
    drv_a = _build(repo, os.path.join(work, "state-a"), os.path.join(work, "art-a"),
                  [normal_ticket], config=_CFG_COUNCIL_ON)
    normal_proceed = drv_a.next_ticket()
    if normal_proceed.get("status") != "PROCEED":
        failures.append(f"[parity] reference ticket should PROCEED directly, got {normal_proceed}")
        return
    if "council" not in normal_proceed:
        failures.append(f"[parity] reference next_ticket PROCEED missing 'council' hint: "
                        f"{normal_proceed}")

    # Driver B: an ambiguous ticket is F5-offered, then resolved -> the PROCEED path under test.
    ambiguous = _ambiguous_ticket("t-ambig-parity")
    drv_b = _build(repo, os.path.join(work, "state-b"), os.path.join(work, "art-b"),
                  [ambiguous], config=_CFG_COUNCIL_ON)
    offer = drv_b.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[parity] expected an F5 offer, got {offer}")
        return
    verdict = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                          evidence="components/Badge.tsx already renders status", dissent_count=0,
                          synthesis_text="clearly reading A")
    resolved_proceed = drv_b.resolve_park("t-ambig-parity", offer["attempt_id"], verdict)
    if resolved_proceed.get("status") != "PROCEED":
        failures.append(f"[parity] RESOLVE must hand back PROCEED, got {resolved_proceed}")
        return
    if "council" not in resolved_proceed:
        failures.append(f"[parity] resolve-park PROCEED missing 'council' hint — the gap INT-2412 "
                        f"fixes, got keys={sorted(resolved_proceed.keys())}")

    # Same set of hint keys on both PROCEED paths (beyond the fixed status/ticket/attempt/snapshot/
    # instructions envelope + resolve-park's F5-specific 'instructions' text, which legitimately
    # differs in content but not in the hint KEYS attached by the shared helper).
    envelope = {"status", "ticket", "attempt", "snapshot", "instructions"}
    normal_hint_keys = set(normal_proceed.keys()) - envelope
    resolved_hint_keys = set(resolved_proceed.keys()) - envelope
    if normal_hint_keys != resolved_hint_keys:
        failures.append(f"[parity] hint-key sets diverge between next_ticket PROCEED "
                        f"({sorted(normal_hint_keys)}) and resolve-park PROCEED "
                        f"({sorted(resolved_hint_keys)})")


def test_resolve_park_proceed_stops_on_credits_exhausted(failures):
    """INT-2412: resolve-park's PROCEED path must honour the SAME credits STOP gate as
    next_ticket's — a zero per-night €cap must terminate STOPPED_CREDITS on the F5-resolved ticket
    instead of handing it out, mirroring next_ticket's _credits_stop handling."""
    work = tempfile.mkdtemp(prefix="ue-f5-parity-stop-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    cfg = {**_CFG_COUNCIL_ON, "budget": {**_CFG_COUNCIL_ON["budget"], "per_night_euro_cap": 0.0}}
    ambiguous = _ambiguous_ticket("t-ambig-stop")
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [ambiguous],
                config=cfg)
    offer = drv.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[parity-stop] expected an F5 offer, got {offer}")
        return
    verdict = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                          evidence="components/Badge.tsx already renders status", dissent_count=0,
                          synthesis_text="clearly reading A")
    result = drv.resolve_park("t-ambig-stop", offer["attempt_id"], verdict)
    if result.get("status") != "STOPPED_CREDITS":
        failures.append(f"[parity-stop] expected STOPPED_CREDITS (per_night_euro_cap=0.0 exhausted "
                        f"immediately), got {result}")


def test_orchestrator_resolve_park_resolve_and_decline(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-orch-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [])
    ticket = _ambiguous_ticket("t-resolve")
    decision = classify(f"{ticket.title}\n{ticket.body}", unattended=True, has_safety_net=True)
    if decision.category != "requirement_meaning":
        failures.append(f"[orch] fixture ticket did not classify as requirement_meaning: {decision}")
    # `offer` is the durable ledger RECORD (Task 1's open_f5_offer shape) — resolve_park never
    # re-classifies the ticket, so the test builds the record directly rather than re-deriving it.
    offer = {"attempt_id": "a-resolve", "category": decision.category,
            "has_safety_net": True, "foundational": decision.foundational, "status": "offered"}

    good = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                        evidence="components/Badge.tsx already renders status", dissent_count=0,
                        synthesis_text="clearly reading A")
    resolved = drv.orch.resolve_park(ticket, offer, good)
    if not isinstance(resolved, ProceedToken):
        failures.append(f"[orch] RESOLVE must route into begin_proceed (ProceedToken), got {resolved}")

    ticket2 = _ambiguous_ticket("t-decline")
    bad = f5.F5Verdict(resolved=False)
    declined = drv.orch.resolve_park(ticket2, offer, bad)
    if not isinstance(declined, TicketOutcome) or declined.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[orch] KEEP_PARKED must write a PARKED_DECISION outcome, got {declined}")
    if "f5-attempted-declined" not in (declined.review_coverage or ""):
        failures.append(f"[orch] declined park missing the audit tag: {declined.review_coverage!r}")
    if "F5 consensus tried and declined" not in declined.why:
        failures.append(f"[orch] declined park 'why' missing the audit text: {declined.why!r}")
    if declined.category != "requirement_meaning":
        failures.append(f"[orch] declined park lost its category: {declined.category!r}")
    stored = drv.store.read("t-decline")
    if stored is None or stored.state != OutcomeState.PARKED_DECISION:
        failures.append("[orch] declined outcome was not durably written to the store")


def test_orchestrator_resolve_park_rejects_forged_hard_category(failures):
    """A resolve-park call carrying an offer RECORD whose category is a hard-PARK category (forged
    or a stale replay) must NEVER honour a claimed RESOLVE — defense in depth, since
    interpret_verdict alone cannot see the category and resolve_park never re-classifies the
    (possibly-mutated) ticket text, only the persisted record."""
    work = tempfile.mkdtemp(prefix="ue-f5-forge-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [])
    hard_ticket = Ticket(id="t-hard", title="x",
                        body="Run a database schema migration to add a column", meta={}, path="")
    decision = classify(f"{hard_ticket.title}\n{hard_ticket.body}", unattended=True,
                       has_safety_net=True)
    if decision.category != "db_schema_or_migration":
        failures.append(f"[orch] fixture did not classify as a hard category: {decision}")
    offer = {"attempt_id": "a-forge", "category": decision.category,
            "has_safety_net": True, "foundational": decision.foundational, "status": "offered"}
    forged = f5.F5Verdict(resolved=True, chosen_reading="whatever", evidence="whatever",
                          dissent_count=0)
    result = drv.orch.resolve_park(hard_ticket, offer, forged)
    if isinstance(result, ProceedToken):
        failures.append("[orch] a forged RESOLVE on a hard-PARK category must NOT reach begin_proceed")
    if not isinstance(result, TicketOutcome) or result.state != OutcomeState.PARKED_FOUNDATIONAL:
        failures.append(f"[orch] hard category must still park foundational, got {result}")


def test_report_shows_f5_declined_block(failures):
    o = TicketOutcome(ticket_id="t-declined", state=OutcomeState.PARKED_DECISION,
                      why="requirement-meaning ambiguous; defer the decision — F5 consensus tried "
                          "and declined: no cited evidence",
                      category="requirement_meaning", review_coverage="f5-attempted-declined")
    text = build_report([o], run_label="t")
    if "F5 consensus attempt" not in text:
        failures.append("[report] F5-declined block missing from the report")
    if "t-declined" not in text:
        failures.append("[report] declined ticket id missing from the report")


def test_next_ticket_offers_f5_only_when_eligible(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-offer-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    ambiguous = _ambiguous_ticket("t-ambig")
    hard = Ticket(id="t-hard", title="x", body="Add a discount to checkout — which percentage?",
                 meta={}, path="")
    drv = _build(repo, state_dir, os.path.join(work, "art"), [ambiguous, hard])

    r1 = drv.next_ticket()
    if r1.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[offer] requirement_meaning PARK should offer F5, got {r1}")
    if r1.get("ticket", {}).get("id") != "t-ambig":
        failures.append(f"[offer] wrong ticket offered: {r1}")
    if "disambiguat" not in (r1.get("prompt") or "").lower():
        failures.append(f"[offer] payload missing the grounding prompt: {r1}")
    if not (r1.get("attempt_id") or "").strip():
        failures.append(f"[offer] payload missing a non-empty attempt_id: {r1}")
    led = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    if not led.f5_attempted("t-ambig"):
        failures.append("[offer] eligibility check must mark already_attempted BEFORE returning "
                        "(optimistic marking — gap #3)")
    offer_rec = led.get_f5_offer("t-ambig")
    if offer_rec is None or offer_rec.get("status") != "offered":
        failures.append(f"[offer] durable offer record must be status='offered', got {offer_rec}")

    # No resolve-park was called — the very next `next` falls through to a NORMAL park (this is
    # both "the agent ignored the offer" and "the process crashed" cases; gap #3 proper is Task 7).
    r2 = drv.next_ticket()
    if r2.get("status") != "DRAINED":
        failures.append(f"[offer] second next() (no resolve-park) should fall through + drain, got {r2}")
    o1 = drv.store.read("t-ambig")
    if o1 is None or o1.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[offer] t-ambig should be parked normally on the fallthrough, got {o1}")
    if "f5-attempted-declined" in (o1.review_coverage or ""):
        failures.append("[offer] a fallthrough park (no verdict was ever rendered) must NOT carry "
                        "the declined-consensus audit tag")
    o2 = drv.store.read("t-hard")
    if o2 is None or o2.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[offer] hard-category ticket should park normally, got {o2}")


def test_f5_offer_survives_a_simulated_crash(failures):
    """Simulate: process 1 calls `next`, gets PARK_CONSENSUS_ELIGIBLE, then 'crashes' (never calls
    resolve-park). A FRESH process (new Orchestrator/StepDriver, same repo/state/ledger paths) must
    NOT re-offer F5 on this ticket — it falls through to a normal, un-tagged park."""
    work = tempfile.mkdtemp(prefix="ue-f5-crash-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    ticket = _ambiguous_ticket("t-crash")

    proc1 = _build(repo, state_dir, os.path.join(work, "art"), [ticket])
    offer = proc1.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[crash] process 1 should get an F5 offer, got {offer}")
        return
    # 'crash' — proc1 is dropped, resolve-park is NEVER called.

    proc2 = _build(repo, state_dir, os.path.join(work, "art"), [ticket])  # fresh process, same disk
    resumed = proc2.next_ticket()
    if resumed.get("status") != "DRAINED":
        failures.append(f"[crash] fresh process should fall through to a normal park + drain, "
                        f"got {resumed}")
    stored = proc2.store.read("t-crash")
    if stored is None or stored.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[crash] ticket should be parked normally after the fallthrough, got {stored}")
    if "f5-attempted-declined" in (stored.review_coverage or ""):
        failures.append("[crash] a fallthrough (never-resolved) park must NOT carry the "
                        "declined-consensus audit tag — no verdict was ever actually rendered")


def test_f5_budget_counter_increments(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-budget-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [])
    before = drv._load_progress().get("f5_calls", -1)
    if before != 0:
        failures.append(f"[budget] f5_calls should start at 0, got {before}")
    drv._bump_f5_calls()
    after = drv._load_progress().get("f5_calls")
    if after != 1:
        failures.append(f"[budget] _bump_f5_calls should increment f5_calls, got {after}")


def test_driver_resolve_park_resolve_branch_completes_done(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-resolve-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    ticket = _ambiguous_ticket("t-e2e-resolve")
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [ticket])

    offer = drv.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[e2e-resolve] expected an F5 offer, got {offer}")
        return
    verdict = f5.F5Verdict(resolved=True, chosen_reading="a status badge",
                          evidence="components/Badge.tsx already renders status", dissent_count=0,
                          synthesis_text="clearly reading A")
    resumed = drv.resolve_park("t-e2e-resolve", offer["attempt_id"], verdict)
    if resumed.get("status") != "PROCEED":
        failures.append(f"[e2e-resolve] RESOLVE must hand back a PROCEED payload, got {resumed}")
        return
    # Re-entry guard on the RESOLVE path itself: the ticket has no terminal outcome yet (it's mid
    # PROCEED, pending `complete`), so this exercises the offer-status=="consumed" guard specifically
    # (not the is_terminal guard the decline/idempotent tests already cover) — the exact hole a
    # second begin_proceed / clobbered pending token would open.
    replay = drv.resolve_park("t-e2e-resolve", offer["attempt_id"], verdict)
    if replay.get("status") != "ALREADY_RESOLVED":
        failures.append(f"[e2e-resolve] a RESOLVE replay before complete must be a no-op, got {replay}")
    with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("\n# F5-resolved widget note\n")
    done = drv.complete_ticket(attempted="implemented reading A")
    if done.get("state") != OutcomeState.DONE.value:
        failures.append(f"[e2e-resolve] the F5-resolved ticket should complete DONE, got {done}")


def test_driver_resolve_park_decline_branch_keeps_parked(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-decline-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    ticket = _ambiguous_ticket("t-e2e-decline")
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [ticket])

    offer = drv.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[e2e-decline] expected an F5 offer, got {offer}")
        return
    result = drv.resolve_park("t-e2e-decline", offer["attempt_id"], f5.F5Verdict(resolved=False))
    if result.get("status") != "KEPT_PARKED":
        failures.append(f"[e2e-decline] KEEP_PARKED must return KEPT_PARKED, got {result}")
    stored = drv.store.read("t-e2e-decline")
    if stored is None or stored.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[e2e-decline] ticket should be parked, got {stored}")
    if "f5-attempted-declined" not in (stored.review_coverage or ""):
        failures.append(f"[e2e-decline] missing audit tag: {stored.review_coverage!r}")
    nxt = drv.next_ticket()
    if nxt.get("status") != "DRAINED":
        failures.append(f"[e2e-decline] backlog should drain after the declined park, got {nxt}")


def test_resolve_park_halts_if_safety_net_lost(failures):
    """Defensive guard beyond the enumerated gaps: if the safety net vanished between the offer and
    resolve-park, the explicit ensure_safety_net() check in StepDriver.resolve_park HALTs —
    resolve-park must surface that, not silently proceed or park against a stale record."""
    work = tempfile.mkdtemp(prefix="ue-f5-halt-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    ticket = _ambiguous_ticket("t-halt")
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [ticket])
    offer = drv.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[halt] expected an F5 offer, got {offer}")
        return
    drv.orch.git.ensure_safety_net = lambda: False   # simulate the safety net vanishing
    result = drv.resolve_park("t-halt", offer["attempt_id"],
                             f5.F5Verdict(resolved=True, chosen_reading="x", evidence="y"))
    if result.get("status") != "HALTED":
        failures.append(f"[halt] resolve-park must HALT if the safety net vanished, got {result}")


def test_resolve_park_twice_is_idempotent(failures):
    """Re-entry guard (both reviews): a duplicate/stale resolve-park call after the offer was
    already consumed must be a safe no-op — never a second begin_proceed/park, never a clobbered
    pending token."""
    work = tempfile.mkdtemp(prefix="ue-f5-idem-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    ticket = _ambiguous_ticket("t-idem")
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), [ticket])
    offer = drv.next_ticket()
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[idem] expected an F5 offer, got {offer}")
        return
    first = drv.resolve_park("t-idem", offer["attempt_id"], f5.F5Verdict(resolved=False))
    if first.get("status") != "KEPT_PARKED":
        failures.append(f"[idem] first resolve-park should KEEP_PARKED, got {first}")
    second = drv.resolve_park("t-idem", offer["attempt_id"], f5.F5Verdict(resolved=False))
    if second.get("status") != "ALREADY_RESOLVED":
        failures.append(f"[idem] duplicate resolve-park must be a no-op, got {second}")
    stored = drv.store.read("t-idem")
    if stored is None or stored.state != OutcomeState.PARKED_DECISION:
        failures.append(f"[idem] outcome must still be the single original park, got {stored}")


def test_f5_budget_ceiling_stops_offering(failures):
    """The per-run F5 call ceiling throttles RUN-WIDE, not per-ticket: with the ceiling
    monkeypatched down to 1 and 3 eligible (requirement_meaning) tickets in the backlog, only
    the FIRST ticket gets a PARK_CONSENSUS_ELIGIBLE offer; the remaining two must fall through
    to a normal park in the SAME run — never a second offer."""
    work = tempfile.mkdtemp(prefix="ue-f5-cap-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    tickets = [_ambiguous_ticket(f"t-cap-{i}") for i in range(3)]
    drv = _build(repo, os.path.join(work, "state"), os.path.join(work, "art"), tickets)

    original_cap = driver_mod._F5_MAX_CALLS_PER_RUN
    driver_mod._F5_MAX_CALLS_PER_RUN = 1
    try:
        r1 = drv.next_ticket()
        if r1.get("status") != "PARK_CONSENSUS_ELIGIBLE":
            failures.append(f"[cap] first ticket (under the cap) should get an F5 offer, got {r1}")
        r2 = drv.next_ticket()
        if r2.get("status") != "DRAINED":
            failures.append(f"[cap] once the per-run F5 ceiling is reached, remaining tickets must "
                            f"fall through to a normal park (never a second offer), got {r2}")
        for tid in ("t-cap-0", "t-cap-1", "t-cap-2"):
            o = drv.store.read(tid)
            if o is None or o.state != OutcomeState.PARKED_DECISION:
                failures.append(f"[cap] {tid} should end up parked, got {o}")
    finally:
        driver_mod._F5_MAX_CALLS_PER_RUN = original_cap


def _run_cli(args, cwd, env):
    proc = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"CLI {args} exited {proc.returncode}: {proc.stderr}")
    return json.loads(proc.stdout)


def _write_config(repo):
    """Second fixture fix discovered in Task 6 TDD (not in the original brief): `cmd_next` refuses
    with NON_DESTRUCTIVE whenever CLAUDE_UNATTENDED=1 and no config was ever persisted to disk
    (run.py's `ensure_config`/wizard path never writes one unattended — by design, see config.py).
    A real CLI round-trip test therefore needs a saved config, exactly like
    acceptance/test_agent_bridge.py's `_write_config` (same sandbox, same gate command)."""
    cfg = {
        "schema_version": 1,
        "gates": [{"name": "tests",
                  "command": [sys.executable, "-m", "unittest", "discover", "-s", ".",
                              "-p", "test_*.py"],
                  "blocking": True}],
        "budget": {"per_ticket_timeout_s": 60, "per_ticket_fix_iterations": 3},
        "autonomy": {"non_destructive_only": False, "requirement_ambiguity": "hybrid"},
        "report": {"local_path": "morning-report.md"},
    }
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w",
             encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def _git_repo_with_ticket(work, ticket_id):
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    # BLOCKER 1 fix: the ticket must live INSIDE the repo, not under `work` — _Context resolves
    # --tickets relative to --repo (run.py:125; precedent: acceptance/test_agent_bridge.py:83-86).
    tickets_dir = os.path.join(repo, "tickets")
    os.makedirs(tickets_dir)
    with open(os.path.join(tickets_dir, f"{ticket_id}.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\nid: {ticket_id}\ntitle: Add a widget\n---\n\n"
                f"Add a widget — unclear which kind of widget?\n")
    subprocess.run(["git", "add", "tickets"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add ticket"], cwd=repo, check=True)
    _write_config(repo)
    return repo


def test_cli_resolve_park_round_trip(failures):
    work = tempfile.mkdtemp(prefix="ue-f5-cli-")
    repo = _git_repo_with_ticket(work, "t-cli")
    env = dict(os.environ, PYTHONPATH=SKILL_ROOT, CLAUDE_UNATTENDED="1")
    common = ["--repo", repo, "--tickets", "tickets"]

    offer = _run_cli(["next", *common], repo, env)
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[cli] expected PARK_CONSENSUS_ELIGIBLE from `next`, got {offer}")
        return
    attempt_id = offer.get("attempt_id")
    if not attempt_id:
        failures.append(f"[cli] `next` offer missing attempt_id: {offer}")
        return
    # A mismatched attempt-id must be REFUSED with status ERROR + exit 2 (the one exit-code
    # convention for every subcommand's ERROR, matching `next`'s sentinel hard-fail) — and the
    # refusal must NOT consume the outstanding offer: the real resolve below still works.
    bad = subprocess.run([sys.executable, "-m", "agents_never_sleep.run", "resolve-park",
                          *common, "--ticket-id", "t-cli", "--attempt-id", "deadbeef00000000",
                          "--resolved", "--chosen-reading", "x", "--evidence", "y",
                          "--dissent-count", "0", "--synthesis-text", "z"],
                         cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    if bad.returncode != 2:
        failures.append(f"[cli] mismatched attempt-id: expected exit 2, got {bad.returncode} "
                        f"({bad.stdout!r})")
    try:
        if json.loads(bad.stdout).get("status") != "ERROR":
            failures.append(f"[cli] mismatched attempt-id: expected status ERROR, got {bad.stdout!r}")
    except json.JSONDecodeError:
        failures.append(f"[cli] mismatched attempt-id: non-JSON output {bad.stdout!r}")
    resumed = _run_cli(["resolve-park", *common, "--ticket-id", "t-cli", "--attempt-id", attempt_id,
                       "--resolved", "--chosen-reading", "a status badge",
                       "--evidence", "components/Badge.tsx already renders status",
                       "--dissent-count", "0", "--synthesis-text", "clearly reading A"], repo, env)
    if resumed.get("status") != "PROCEED":
        failures.append(f"[cli] expected PROCEED from `resolve-park`, got {resumed}")
        return
    with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("\n# cli f5 note\n")
    done = _run_cli(["complete", *common, "--attempted", "implemented via CLI"], repo, env)
    if done.get("state") != "DONE":
        failures.append(f"[cli] expected DONE after complete, got {done}")


def test_cli_resolve_park_not_resolved_flag(failures):
    """--not-resolved must be honoured symmetrically to --resolved (argparse wiring check)."""
    work = tempfile.mkdtemp(prefix="ue-f5-cli-decline-")
    repo = _git_repo_with_ticket(work, "t-cli2")
    env = dict(os.environ, PYTHONPATH=SKILL_ROOT, CLAUDE_UNATTENDED="1")
    common = ["--repo", repo, "--tickets", "tickets"]

    offer = _run_cli(["next", *common], repo, env)
    if offer.get("status") != "PARK_CONSENSUS_ELIGIBLE":
        failures.append(f"[cli-decline] expected PARK_CONSENSUS_ELIGIBLE, got {offer}")
        return
    attempt_id = offer.get("attempt_id")
    if not attempt_id:
        failures.append(f"[cli-decline] `next` offer missing attempt_id: {offer}")
        return
    result = _run_cli(["resolve-park", *common, "--ticket-id", "t-cli2", "--attempt-id", attempt_id,
                       "--not-resolved"], repo, env)
    if result.get("status") != "KEPT_PARKED":
        failures.append(f"[cli-decline] --not-resolved should KEEP_PARKED, got {result}")


def main() -> int:
    failures: list = []
    test_ledger_f5_attempted(failures)
    test_orchestrator_resolve_park_resolve_and_decline(failures)
    test_orchestrator_resolve_park_rejects_forged_hard_category(failures)
    test_report_shows_f5_declined_block(failures)
    test_next_ticket_offers_f5_only_when_eligible(failures)
    test_f5_offer_survives_a_simulated_crash(failures)
    test_f5_budget_counter_increments(failures)
    test_driver_resolve_park_resolve_branch_completes_done(failures)
    test_driver_resolve_park_decline_branch_keeps_parked(failures)
    test_resolve_park_halts_if_safety_net_lost(failures)
    test_resolve_park_twice_is_idempotent(failures)
    test_f5_budget_ceiling_stops_offering(failures)
    test_cli_resolve_park_round_trip(failures)
    test_cli_resolve_park_not_resolved_flag(failures)
    test_resolve_park_proceed_has_council_parity(failures)
    test_resolve_park_proceed_stops_on_credits_exhausted(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — F5 wiring not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — F5 wiring holds so far")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
