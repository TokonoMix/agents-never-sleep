#!/usr/bin/env python3
"""Tokonomix onboarding-gate test — proves the deterministic decision, the interactive directive vs
the unattended degradation, and that a degraded night is surfaced as a BLIND SPOT in the report.

Onboarding fires ONLY when tokonomix is configured but its credential is missing (a real "the key
went away" case), so projects that never wanted it are never nagged. The actual onboard handshake is
the agent's MCP call; the harness owns only this gate. Credential probing is made deterministic on
any box via the TOKONOMIX_CREDS_FILE override. Exit 0 = GREEN.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import onboarding  # noqa: E402
from agents_never_sleep.driver import StepDriver  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator  # noqa: E402
from agents_never_sleep.report import build_report  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore, TicketOutcome  # noqa: E402
from agents_never_sleep.tickets import load_tickets  # noqa: E402

# A credential CONSUMER (council) is enabled → onboarding is relevant when the key is missing.
CFG_ON = {"council": {"enabled": True}, "integrations": {"tokonomix": {"enabled": True}}}
# Integration "on" but NO consumer enabled → onboarding must stay silent (the inverse false-positive).
CFG_OFF = {"council": {"enabled": False}, "specialists": {"enabled": False},
           "integrations": {"tokonomix": {"enabled": True}}}
# Council enabled but NO integrations block → council.enabled() still True (defaults the sub-key);
# onboarding MUST fire (the silent-blind-spot the gate exists for). Critical regression case.
CFG_NO_INTEGRATIONS = {"council": {"enabled": True}}


def _no_credential(env_tmp):
    """Point the probe at a nonexistent creds file + clear the env key → credential absent."""
    os.environ.pop("TOKONOMIX_API_KEY", None)
    os.environ["TOKONOMIX_CREDS_FILE"] = os.path.join(env_tmp, "nope.json")


def _with_credential(env_tmp):
    path = os.path.join(env_tmp, "creds.json")
    with open(path, "w") as fh:
        fh.write("{}")
    os.environ.pop("TOKONOMIX_API_KEY", None)
    os.environ["TOKONOMIX_CREDS_FILE"] = path


def test_logic(failures, tmp):
    _no_credential(tmp)
    if onboarding.credential_present():
        failures.append("[logic] credential should be absent with override at a nonexistent file")
    if not onboarding.needs_onboarding(CFG_ON):
        failures.append("[logic] consumer enabled + no credential should NEED onboarding")
    if onboarding.needs_onboarding(CFG_OFF):
        failures.append("[logic] integration on but NO consumer enabled must NOT need onboarding")
    if not onboarding.needs_onboarding(CFG_NO_INTEGRATIONS):
        failures.append("[logic] council enabled w/o integrations block must STILL need onboarding "
                        "(council.enabled() defaults the sub-key True — the silent-blind-spot case)")

    di = onboarding.directive(CFG_ON, interactive=True)
    if not di or di.get("action") != "onboard" or "tokonomix_onboard" not in di.get("protocol", ""):
        failures.append(f"[logic] interactive directive should guide the onboard handshake: {di}")
    du = onboarding.directive(CFG_ON, interactive=False)
    if not du or du.get("action") != "degraded" or not du.get("blind_spot"):
        failures.append(f"[logic] unattended directive should be a degradation blind-spot: {du}")
    if onboarding.directive(CFG_OFF, interactive=True) is not None:
        failures.append("[logic] no directive when tokonomix isn't configured")

    _with_credential(tmp)
    if not onboarding.credential_present():
        failures.append("[logic] credential should be present when the creds file exists")
    if onboarding.needs_onboarding(CFG_ON) or onboarding.directive(CFG_ON, interactive=False):
        failures.append("[logic] a present credential should suppress onboarding entirely")


def test_report_note(failures):
    o = TicketOutcome(ticket_id="t1", state=OutcomeState.DONE, why="ok")
    rep = build_report([o], notes=[onboarding.degradation_note()])
    if "BLIND SPOT" not in rep or "tokonomix credential missing" not in rep:
        failures.append(f"[report] degradation note not rendered as a blind spot: {rep!r}")
    if "BLIND SPOT" in build_report([o]):
        failures.append("[report] no blind-spot banner should appear without notes")


def _drive(tmp, cfg, *, unattended):
    work = tempfile.mkdtemp(prefix="ue-onb-", dir=tmp)
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    state_dir = os.path.join(work, "state")
    tickets = load_tickets(os.path.join(HERE, "tickets"))
    store = OutcomeStore(state_dir)
    gate = GateRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", ".",
                               "-p", "test_*.py"], cwd=repo, timeout=60)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=os.path.join(work, "artifacts"), unattended=unattended,
                        ledger=ledger)
    report_path = os.path.join(work, "report.md")
    driver = StepDriver(orch=orch, tickets=tickets, store=store, state_dir=state_dir,
                        report_path=report_path, config=cfg)
    first_onboarding = None
    for _ in range(20):
        res = driver.next_ticket()
        if res["status"] != "PROCEED":
            break
        if first_onboarding is None:
            first_onboarding = res.get("onboarding")
        with open(os.path.join(repo, "n.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# x\n")
        driver.complete_ticket(attempted="x")
    report = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else ""
    return first_onboarding, report


def test_end_to_end(failures, tmp):
    # Unattended + configured + no credential → PROCEED carries a 'degraded' directive AND the
    # report ends with a BLIND SPOT note.
    _no_credential(tmp)
    onb, report = _drive(tmp, CFG_ON, unattended=True)
    if not onb or onb.get("action") != "degraded":
        failures.append(f"[e2e] unattended PROCEED should carry a degraded onboarding directive: {onb}")
    if "BLIND SPOT" not in report or "tokonomix" not in report:
        failures.append("[e2e] unattended degraded night must surface a BLIND SPOT in the report")

    # Interactive + configured + no credential → PROCEED carries an 'onboard' directive.
    _no_credential(tmp)
    onb_i, _ = _drive(tmp, CFG_ON, unattended=False)
    if not onb_i or onb_i.get("action") != "onboard":
        failures.append(f"[e2e] interactive PROCEED should carry an onboard directive: {onb_i}")

    # Credential present → no onboarding directive, no blind spot.
    _with_credential(tmp)
    onb_ok, report_ok = _drive(tmp, CFG_ON, unattended=True)
    if onb_ok is not None:
        failures.append(f"[e2e] present credential should yield no onboarding directive: {onb_ok}")
    if "BLIND SPOT" in report_ok:
        failures.append("[e2e] present credential should yield no blind-spot note")


def main() -> int:
    failures = []
    saved = {k: os.environ.get(k) for k in ("TOKONOMIX_API_KEY", "TOKONOMIX_CREDS_FILE")}
    tmp = tempfile.mkdtemp(prefix="ue-onb-env-")
    try:
        test_logic(failures, tmp)
        test_report_note(failures)
        test_end_to_end(failures, tmp)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — onboarding gate not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — onboarding fires only when configured+keyless; interactive guides the "
          "handshake, unattended degrades gracefully + flags a report blind spot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
