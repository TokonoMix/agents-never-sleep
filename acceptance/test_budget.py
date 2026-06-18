#!/usr/bin/env python3
"""Budget gate + A/B credit-exhaustion policy tests.

Covers:
  - decide_budget: three gate decisions (PROCEED / DEGRADE / STOP)
  - A vs B policy branch (on_credits_exhausted)
  - 402 mapping via decide_budget(http_402=True)
  - running-total accumulation + cap headroom (remaining_headroom)
  - run-start preflight: interactive ASK vs unattended-policy (logged, no block)
  - degrade flag persistence across processes (via _set_degrade_flag / _load_progress)
  - end-to-end: STOP terminates as STOPPED_CREDITS; DEGRADE floors DONE to DONE_LOW_CONFIDENCE

Exit 0 = GREEN.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness import council                                  # noqa: E402
from harness.council import BudgetDecision, CouncilTier     # noqa: E402
from harness.driver import _run_start_credits_preflight, StepDriver  # noqa: E402
from harness.gates import GateRunner                         # noqa: E402
from harness.ledger import AttemptLedger                     # noqa: E402
from harness.orchestrator import Orchestrator                # noqa: E402
from harness.state import OutcomeState, OutcomeStore         # noqa: E402
from harness.tickets import load_tickets                     # noqa: E402

# Minimal config with council + tokonomix enabled.
CFG_STOP = {
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

CFG_DEGRADE = {**CFG_STOP, "budget": {**CFG_STOP["budget"], "on_credits_exhausted": "degrade"}}


# ── 1. decide_budget — three gate decisions ─────────────────────────────────────────────────────

def test_decide_budget_proceed(failures):
    decision, reason = council.decide_budget(
        CFG_STOP,
        {"council_cost_eur": 0.5, "council_calls": 2},
        balance_eur=5.0, est_cost_eur=0.1)
    if decision != BudgetDecision.PROCEED:
        failures.append(f"[decide] under all caps should be PROCEED, got {decision} ({reason})")
    if reason:
        failures.append(f"[decide] PROCEED should have empty reason, got {reason!r}")


def test_decide_budget_stop_balance(failures):
    decision, reason = council.decide_budget(
        CFG_STOP,
        {"council_cost_eur": 0.0, "council_calls": 0},
        balance_eur=0.5)  # below threshold 1.0
    if decision != BudgetDecision.STOP:
        failures.append(f"[decide/stop] low balance should → STOP (policy A), got {decision}")
    if "balance" not in reason.lower():
        failures.append(f"[decide/stop] reason should mention balance: {reason!r}")


def test_decide_budget_degrade_balance(failures):
    decision, reason = council.decide_budget(
        CFG_DEGRADE,
        {"council_cost_eur": 0.0, "council_calls": 0},
        balance_eur=0.5)  # below threshold 1.0, policy B
    if decision != BudgetDecision.DEGRADE:
        failures.append(f"[decide/degrade] low balance + policy B should → DEGRADE, got {decision}")


def test_decide_budget_euro_cap(failures):
    # Spent 1.9, est 0.2: 1.9 + 0.2 > 2.0 → exhausted
    decision, _ = council.decide_budget(
        CFG_STOP,
        {"council_cost_eur": 1.9, "council_calls": 1},
        balance_eur=10.0, est_cost_eur=0.2)
    if decision != BudgetDecision.STOP:
        failures.append(f"[decide/euro-cap] would exceed cap → STOP, got {decision}")


def test_decide_budget_call_cap(failures):
    decision, reason = council.decide_budget(
        CFG_STOP,
        {"council_cost_eur": 0.0, "council_calls": 5},  # at max_council_calls_per_night=5
        balance_eur=10.0, est_cost_eur=0.0)
    if decision != BudgetDecision.STOP:
        failures.append(f"[decide/call-cap] call cap reached → STOP, got {decision}")
    if "cap" not in reason.lower():
        failures.append(f"[decide/call-cap] reason should mention cap: {reason!r}")


def test_decide_budget_no_caps(failures):
    cfg = {"budget": {"balance_threshold_euro": 0.0, "on_credits_exhausted": "stop"},
           "integrations": {"tokonomix": {"enabled": True}}}
    decision, _ = council.decide_budget(
        cfg,
        {"council_cost_eur": 999.0, "council_calls": 999},
        balance_eur=10.0, est_cost_eur=5.0)
    if decision != BudgetDecision.PROCEED:
        failures.append(f"[decide/no-caps] no caps → always PROCEED, got {decision}")


# ── 2. 402 mapping ──────────────────────────────────────────────────────────────────────────────

def test_402_maps_to_stop(failures):
    decision, reason = council.decide_budget(CFG_STOP, {}, http_402=True)
    if decision != BudgetDecision.STOP:
        failures.append(f"[402/stop] 402 + policy A → STOP, got {decision}")
    if "402" not in reason:
        failures.append(f"[402/stop] reason should mention 402: {reason!r}")


def test_402_maps_to_degrade(failures):
    decision, reason = council.decide_budget(CFG_DEGRADE, {}, http_402=True)
    if decision != BudgetDecision.DEGRADE:
        failures.append(f"[402/degrade] 402 + policy B → DEGRADE, got {decision}")


# ── 3. Running-total accumulation + headroom ─────────────────────────────────────────────────────

def test_remaining_headroom(failures):
    h = council.remaining_headroom(CFG_STOP, {"council_cost_eur": 0.75, "council_calls": 2})
    if h["euro_remaining"] is None:
        failures.append("[headroom] euro_remaining should be set when cap configured")
    elif abs(h["euro_remaining"] - 1.25) > 0.001:
        failures.append(f"[headroom] euro_remaining expected 1.25, got {h['euro_remaining']}")
    if h["calls_remaining"] is None:
        failures.append("[headroom] calls_remaining should be set when call cap configured")
    elif h["calls_remaining"] != 3:
        failures.append(f"[headroom] calls_remaining expected 3, got {h['calls_remaining']}")


def test_remaining_headroom_no_caps(failures):
    cfg = {"budget": {}}
    h = council.remaining_headroom(cfg, {"council_cost_eur": 5.0, "council_calls": 10})
    if h["euro_remaining"] is not None:
        failures.append(f"[headroom/no-caps] euro_remaining should be None, got {h['euro_remaining']}")
    if h["calls_remaining"] is not None:
        failures.append(f"[headroom/no-caps] calls_remaining should be None, got {h['calls_remaining']}")


def test_driver_running_total(failures):
    """StepDriver._bump_council accumulates across calls (resumable)."""
    work = tempfile.mkdtemp(prefix="ue-budget-total-")
    try:
        state_dir = os.path.join(work, "state")
        repo = tempfile.mkdtemp(prefix="ue-budget-repo-", dir=work)
        store = OutcomeStore(state_dir)
        gate = GateRunner(command=["true"], cwd=repo, timeout=5)
        gate.noop = True
        ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
        orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                            artifacts_dir=os.path.join(work, "arts"), unattended=True, ledger=ledger)
        driver = StepDriver(orch=orch, tickets=[], store=store, state_dir=state_dir,
                            report_path=os.path.join(work, "report.md"), config=CFG_STOP)
        driver._bump_council(0.30)
        driver._bump_council(0.45)
        p = driver._load_progress()
        if p["council_calls"] != 2:
            failures.append(f"[running-total] council_calls expected 2, got {p['council_calls']}")
        if abs(p["council_cost_eur"] - 0.75) > 0.001:
            failures.append(f"[running-total] cost expected 0.75, got {p['council_cost_eur']}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 4. Run-start preflight: interactive vs unattended ────────────────────────────────────────────

def test_preflight_interactive_ask(failures):
    """Interactive mode with low balance: asks the A/B question and records the choice."""
    cfg = {
        "budget": {"balance_threshold_euro": 1.0, "per_night_euro_cap": None,
                   "max_council_calls_per_night": 50, "on_credits_exhausted": "stop"},
        "integrations": {"tokonomix": {"enabled": True}},
        "council": {
            "enabled": True,
            "light": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "heavy": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "prices_cents_per_mtok": {}, "est_prompt_tokens": 100,
        },
    }
    # Simulate user typing "B" for degrade
    with unittest.mock.patch("builtins.input", return_value="B"):
        _run_start_credits_preflight(balance_eur=0.5, config=cfg, unattended=False)
    if cfg["budget"].get("on_credits_exhausted") != "degrade":
        failures.append("[preflight/interactive] user chose B → on_credits_exhausted should be 'degrade', "
                        f"got {cfg['budget'].get('on_credits_exhausted')!r}")


def test_preflight_interactive_ask_default_a(failures):
    """Interactive mode: empty answer defaults to A (stop)."""
    cfg = {
        "budget": {"balance_threshold_euro": 1.0, "per_night_euro_cap": None,
                   "max_council_calls_per_night": 50, "on_credits_exhausted": "degrade"},
        "integrations": {"tokonomix": {"enabled": True}},
        "council": {
            "enabled": True,
            "light": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "heavy": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "prices_cents_per_mtok": {}, "est_prompt_tokens": 100,
        },
    }
    with unittest.mock.patch("builtins.input", return_value=""):
        _run_start_credits_preflight(balance_eur=0.5, config=cfg, unattended=False)
    if cfg["budget"].get("on_credits_exhausted") != "stop":
        failures.append("[preflight/interactive/default] empty answer → 'stop', "
                        f"got {cfg['budget'].get('on_credits_exhausted')!r}")


def test_preflight_unattended_no_input(failures):
    """Unattended mode: applies configured policy without ever calling input()."""
    cfg = {
        "budget": {"balance_threshold_euro": 1.0, "per_night_euro_cap": None,
                   "max_council_calls_per_night": 50, "on_credits_exhausted": "stop"},
        "integrations": {"tokonomix": {"enabled": True}},
        "council": {
            "enabled": True,
            "light": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "heavy": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "prices_cents_per_mtok": {}, "est_prompt_tokens": 100,
        },
    }
    input_called = []
    with unittest.mock.patch("builtins.input", side_effect=lambda _: input_called.append(True) or "A"):
        _run_start_credits_preflight(balance_eur=0.5, config=cfg, unattended=True)
    if input_called:
        failures.append("[preflight/unattended] must NEVER call input() in unattended mode")
    # Config unchanged — unattended applies, doesn't prompt-record
    if cfg["budget"]["on_credits_exhausted"] != "stop":
        failures.append("[preflight/unattended] config should remain unchanged in unattended mode")


def test_preflight_sufficient_balance_silent(failures):
    """When balance comfortably covers the run, no output and no prompt."""
    cfg = {
        "budget": {"balance_threshold_euro": 1.0, "per_night_euro_cap": 2.0,
                   "max_council_calls_per_night": 5, "on_credits_exhausted": "stop"},
        "integrations": {"tokonomix": {"enabled": True}},
        "council": {
            "enabled": True,
            "light": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "heavy": {"proposers": [], "judges": [], "mode": "consensus", "max_tokens": 100},
            "prices_cents_per_mtok": {}, "est_prompt_tokens": 100,
        },
    }
    input_called = []
    captured = io.StringIO()
    with unittest.mock.patch("builtins.input", side_effect=lambda _: input_called.append(True) or "A"):
        with unittest.mock.patch("sys.stdout", captured):
            # Balance 100.0 >> threshold 1.0, cap 2.0, estimated very small
            _run_start_credits_preflight(balance_eur=100.0, config=cfg, unattended=False)
    if input_called:
        failures.append("[preflight/silent] should not prompt when balance is sufficient")


def test_preflight_none_balance_skips(failures):
    """When balance_eur is None, no preflight (agent didn't call tokonomix_get_balance)."""
    cfg = {
        "budget": {"balance_threshold_euro": 1.0, "on_credits_exhausted": "stop"},
        "integrations": {"tokonomix": {"enabled": True}},
        "council": {"enabled": True},
    }
    input_called = []
    with unittest.mock.patch("builtins.input", side_effect=lambda _: input_called.append(True) or "A"):
        _run_start_credits_preflight(balance_eur=None, config=cfg, unattended=False)
    if input_called:
        failures.append("[preflight/none-balance] None balance should skip preflight entirely")


# ── 5. Degrade flag persistence ─────────────────────────────────────────────────────────────────

def test_degrade_flag_persistence(failures):
    """_set_degrade_flag persists across _load_progress calls (durable across processes)."""
    work = tempfile.mkdtemp(prefix="ue-budget-degrade-")
    try:
        state_dir = os.path.join(work, "state")
        repo = tempfile.mkdtemp(prefix="ue-budget-repo-", dir=work)
        store = OutcomeStore(state_dir)
        gate = GateRunner(command=["true"], cwd=repo, timeout=5)
        gate.noop = True
        ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
        orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                            artifacts_dir=os.path.join(work, "arts"), unattended=True, ledger=ledger)
        driver = StepDriver(orch=orch, tickets=[], store=store, state_dir=state_dir,
                            report_path=os.path.join(work, "report.md"), config=CFG_DEGRADE)
        if driver._load_progress().get("credits_exhausted_degrade"):
            failures.append("[degrade-flag] should start as False")
        driver._set_degrade_flag()
        if not driver._load_progress().get("credits_exhausted_degrade"):
            failures.append("[degrade-flag] should be True after _set_degrade_flag()")
        # Simulate a new process by creating a fresh driver object (re-reads from disk)
        driver2 = StepDriver(orch=orch, tickets=[], store=store, state_dir=state_dir,
                             report_path=os.path.join(work, "report.md"), config=CFG_DEGRADE)
        if not driver2._load_progress().get("credits_exhausted_degrade"):
            failures.append("[degrade-flag] should persist across driver instances (durable)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 6. End-to-end: STOP terminates as STOPPED_CREDITS ───────────────────────────────────────────

def test_e2e_stop_credits(failures):
    """next_ticket with very low balance + policy A should return STOPPED_CREDITS."""
    work = tempfile.mkdtemp(prefix="ue-budget-e2e-stop-")
    try:
        repo = os.path.join(work, "repo")
        shutil.copytree(os.path.join(HERE, "sandbox"), repo)
        state_dir = os.path.join(work, "state")
        tickets = load_tickets(os.path.join(HERE, "tickets"))
        store = OutcomeStore(state_dir)
        gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                                   "-p", "test_*.py"], cwd=repo, timeout=30)
        ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
        orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                            artifacts_dir=os.path.join(work, "arts"), unattended=True, ledger=ledger)
        # Use a config with 5-call cap and stop policy; but don't exhaust calls yet —
        # instead test the balance gate (balance below threshold).
        cfg = {**CFG_STOP,
               "budget": {**CFG_STOP["budget"], "balance_threshold_euro": 5.0,
                          "on_credits_exhausted": "stop"}}
        driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                            report_path=os.path.join(work, "report.md"), config=cfg)
        # Provide a balance of 0.50 — below threshold of 5.0 → decide_budget fires STOP on first
        # council hint → _credits_stop signal → _terminate("STOPPED_CREDITS")
        result = driver.next_ticket(balance_eur=0.50)
        if result.get("status") != "STOPPED_CREDITS":
            failures.append(f"[e2e/stop] expected STOPPED_CREDITS, got {result.get('status')!r} "
                            f"(reason: {result.get('reason', result.get('error', ''))!r})")
        if "reason" not in result:
            failures.append("[e2e/stop] STOPPED_CREDITS should carry a 'reason' field")
        if "report_path" not in result:
            failures.append("[e2e/stop] STOPPED_CREDITS should carry 'report_path'")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 7. End-to-end: DEGRADE floors DONE to DONE_LOW_CONFIDENCE ───────────────────────────────────

def test_e2e_degrade_floors_done(failures):
    """In degrade mode, a ticket that would normally be DONE becomes DONE_LOW_CONFIDENCE."""
    from harness.orchestrator import Orchestrator, ProceedToken
    from harness.state import OutcomeState
    work = tempfile.mkdtemp(prefix="ue-budget-e2e-degrade-")
    try:
        repo = os.path.join(work, "repo")
        shutil.copytree(os.path.join(HERE, "sandbox"), repo)
        state_dir = os.path.join(work, "state")
        tickets = load_tickets(os.path.join(HERE, "tickets"))
        store = OutcomeStore(state_dir)
        gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                                   "-p", "test_*.py"], cwd=repo, timeout=30)
        ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
        orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                            artifacts_dir=os.path.join(work, "arts"), unattended=True, ledger=ledger)
        # Use degrade policy config.
        driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                            report_path=os.path.join(work, "report.md"), config=CFG_DEGRADE)
        # Get the first ticket (ticket-01-trivial) and simulate implementing it.
        result = driver.next_ticket(balance_eur=100.0)  # balance fine → PROCEED
        if result.get("status") != "PROCEED":
            failures.append(f"[e2e/degrade] expected PROCEED, got {result.get('status')!r}")
            return
        # Manually set the degrade flag to simulate credits running out mid-run.
        driver._set_degrade_flag()
        # Make a trivial edit so the gate stays green.
        with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# credits-degrade test note\n")
        # complete_ticket with no council verdict — in degrade mode, DONE should become DLC.
        cr = driver.complete_ticket(attempted="trivial edit", council_verdict=None)
        if cr.get("status") != "RECORDED":
            failures.append(f"[e2e/degrade] complete expected RECORDED, got {cr.get('status')!r}")
            return
        outcome = store.read("ticket-01-trivial")
        if outcome is None:
            failures.append("[e2e/degrade] outcome not recorded")
        elif outcome.state != OutcomeState.DONE_LOW_CONFIDENCE:
            failures.append(f"[e2e/degrade] degrade mode should floor DONE → DONE_LOW_CONFIDENCE, "
                            f"got {outcome.state.value!r}")
        elif "unverified" not in (outcome.why or "").lower() and \
                "credits" not in (outcome.why or "").lower():
            failures.append(f"[e2e/degrade] why should mention unverified/credits: {outcome.why!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 7b. End-to-end: reactive 402 under policy A persists STOP across the bumps ───────────────────

def test_e2e_402_roundtrip_stops(failures):
    """The reactive backstop: a council that reports HTTP 402 under policy A must make the NEXT
    next_ticket() terminate as STOPPED_CREDITS — proving credits_stop_requested survives the
    read-modify-write bumps inside complete_ticket (regression guard for the lossy-schema bug)."""
    from harness.state import OutcomeState  # noqa: F401
    work = tempfile.mkdtemp(prefix="ue-budget-e2e-402-")
    try:
        repo = os.path.join(work, "repo")
        shutil.copytree(os.path.join(HERE, "sandbox"), repo)
        state_dir = os.path.join(work, "state")
        tickets = load_tickets(os.path.join(HERE, "tickets"))
        store = OutcomeStore(state_dir)
        gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                                   "-p", "test_*.py"], cwd=repo, timeout=30)
        ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
        orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                            artifacts_dir=os.path.join(work, "arts"), unattended=True, ledger=ledger)
        # Policy A (stop). Balance high enough that the proactive balance-gate does NOT fire — the
        # ONLY thing that can stop the run is the reactive 402 path.
        driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                            report_path=os.path.join(work, "report.md"), config=CFG_STOP)
        first = driver.next_ticket(balance_eur=100.0)
        if first.get("status") != "PROCEED":
            failures.append(f"[e2e/402] expected first PROCEED, got {first.get('status')!r}")
            return
        with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# 402 roundtrip test note\n")
        # Council attempt hit 402 — complete the ticket reporting the 402 status.
        cr = driver.complete_ticket(attempted="trivial edit", council_verdict=None,
                                    council_http_status=402)
        if cr.get("status") != "RECORDED":
            failures.append(f"[e2e/402] complete expected RECORDED, got {cr.get('status')!r}")
            return
        # The NEXT call must terminate the run — the regression: prior to the _load_progress fix the
        # credits_stop_requested flag was erased by the bumps and this returned PROCEED.
        nxt = driver.next_ticket(balance_eur=100.0)
        if nxt.get("status") != "STOPPED_CREDITS":
            failures.append(f"[e2e/402] reactive 402 STOP failed: next returned {nxt.get('status')!r} "
                            "(credits_stop_requested likely dropped by a bump)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── 8. on_credits_exhausted config default ──────────────────────────────────────────────────────

def test_config_default(failures):
    """default_config includes on_credits_exhausted='stop'."""
    from harness.config import default_config
    from harness.preflight import CapabilityProfile
    profile = CapabilityProfile(
        platform="test", has_git=False, git_clean=False, exec_mode="direct",
        gates=[], has_tokonomix=False, has_vault=False, has_paperclip=False,
        unattended=True, expected_yield="medium", warnings=[])
    cfg = default_config(profile)
    b = cfg.get("budget", {})
    if "on_credits_exhausted" not in b:
        failures.append("[config] default_config.budget should include 'on_credits_exhausted'")
    elif b["on_credits_exhausted"] != "stop":
        failures.append(f"[config] default on_credits_exhausted should be 'stop', "
                        f"got {b['on_credits_exhausted']!r}")


def main() -> int:
    failures = []
    test_decide_budget_proceed(failures)
    test_decide_budget_stop_balance(failures)
    test_decide_budget_degrade_balance(failures)
    test_decide_budget_euro_cap(failures)
    test_decide_budget_call_cap(failures)
    test_decide_budget_no_caps(failures)
    test_402_maps_to_stop(failures)
    test_402_maps_to_degrade(failures)
    test_remaining_headroom(failures)
    test_remaining_headroom_no_caps(failures)
    test_driver_running_total(failures)
    test_preflight_interactive_ask(failures)
    test_preflight_interactive_ask_default_a(failures)
    test_preflight_unattended_no_input(failures)
    test_preflight_sufficient_balance_silent(failures)
    test_preflight_none_balance_skips(failures)
    test_degrade_flag_persistence(failures)
    test_e2e_stop_credits(failures)
    test_e2e_degrade_floors_done(failures)
    test_e2e_402_roundtrip_stops(failures)
    test_config_default(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — budget gate tests failed")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — budget gate, A/B policy, 402 mapping, accumulation, e2e all proven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
