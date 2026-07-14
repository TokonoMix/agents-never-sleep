#!/usr/bin/env python3
"""Consent-AUDIT ACCEPTANCE test (t04, unattended safety model Part B follow-on).

Proves the audit trail a consent-upgraded ALLOW leaves behind, and the morning report's
provenance section built from it:
  * consent_store.append_audit / read_audit round-trip a list of json-line events, fail safe on
    a missing path, and skip (not raise on) a malformed line,
  * enforce.py end-to-end: a consent-upgraded ALLOW appends EXACTLY one audit line (slug, command,
    ts_utc); a DENY (no consent) appends nothing; UE_CONSENT_AUDIT unset never crashes the hook
    (best-effort audit, non-best-effort enforcement),
  * report.py renders both the pre-authorized-slugs manifest and the audit-events list, collapses
    a newline/CR inside a logged command to a single report line (no forged report rows), and
    omits the whole section when there is nothing to show,
  * both existing build_report call sites (run.py cmd_report, driver.py terminal report) keep
    working when the two new kwargs are omitted (BC).
Exit 0 = GREEN. NOT pytest — a plain main()/failures list, like the rest of acceptance/.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

# Every consent_store call below must honor a store-dir override, and this suite must NEVER touch
# the real ~/.config/agents-never-sleep tree.
os.environ["ANS_TEST_MODE"] = "1"

from agents_never_sleep import consent_store  # noqa: E402
from agents_never_sleep.report import build_report, frozen_consent_provenance  # noqa: E402
from agents_never_sleep.state import OutcomeState, TicketOutcome  # noqa: E402


def _outcome(ticket_id="T1", state=OutcomeState.DONE, why="did the thing"):
    return TicketOutcome(ticket_id=ticket_id, state=state, why=why)


def test_append_read_round_trip(failures):
    work = tempfile.mkdtemp(prefix="ans-audit-test-")
    try:
        path = os.path.join(work, "sub", "repo.audit.jsonl")
        events = [
            {"ts_utc": "2026-07-14T00:00:00Z", "slug": "redis_flush", "command": "redis-cli flushall"},
            {"ts_utc": "2026-07-14T00:01:00Z", "slug": "force_push", "command": "git push --force origin main"},
        ]
        for ev in events:
            consent_store.append_audit(path, ev)
        got = consent_store.read_audit(path)
        if got != events:
            failures.append(f"[audit-round-trip] mismatch: {got!r} != {events!r}")
        # File must exist, be a regular file, and (best-effort) chmod 0600.
        mode = os.stat(path).st_mode & 0o777
        if mode != 0o600:
            failures.append(f"[audit-round-trip] expected 0600, got {oct(mode)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_read_audit_missing_path(failures):
    got = consent_store.read_audit("/nonexistent/path/does-not-exist.jsonl")
    if got != []:
        failures.append(f"[audit-missing] missing path must read as []; got {got!r}")


def test_read_audit_skips_malformed_line(failures):
    work = tempfile.mkdtemp(prefix="ans-audit-test-")
    try:
        path = os.path.join(work, "audit.jsonl")
        good = {"ts_utc": "2026-07-14T00:00:00Z", "slug": "redis_flush", "command": "redis-cli flushall"}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
            fh.write(json.dumps(good) + "\n")
        got = consent_store.read_audit(path)
        if got != [good]:
            failures.append(f"[audit-malformed] expected only the good line; got {got!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_audit_path_out_of_repo(failures):
    work = tempfile.mkdtemp(prefix="ans-audit-test-")
    store = os.path.join(work, "store")
    repo = os.path.join(work, "repo")
    os.makedirs(repo, exist_ok=True)
    try:
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": store}):
            path = consent_store.audit_path(repo)
        if repo in path:
            failures.append(f"[audit-path] must not contain repo_dir: {path!r}")
        if not path.endswith(".audit.jsonl"):
            failures.append(f"[audit-path] expected a .audit.jsonl suffix; got {path!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_enforce(payload, extra_env):
    env = dict(os.environ)
    env.pop("CLAUDE_UNATTENDED", None)
    env["UE_UNATTENDED"] = "1"
    env.pop("UE_CONSENT", None)
    env.pop("UE_CONSENT_AUDIT", None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agents_never_sleep.enforce", "claude", "pre_tool"],
        input=json.dumps(payload), text=True, capture_output=True, cwd=SKILL_ROOT, env=env)


def test_enforce_consent_allow_appends_one_line(failures):
    work = tempfile.mkdtemp(prefix="ans-audit-e2e-")
    try:
        audit_path = os.path.join(work, "audit.jsonl")
        payload = {"tool_name": "Bash", "tool_input": {"command": "redis-cli flushall"}}
        consent_json = json.dumps({"redis_flush": {"allowed": True}})
        p = _run_enforce(payload, {"UE_CONSENT": consent_json, "UE_CONSENT_AUDIT": audit_path})

        try:
            out = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            out = {}
        denied = out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if denied:
            failures.append(f"[e2e-allow] consented command should ALLOW (out={p.stdout!r})")

        events = consent_store.read_audit(audit_path)
        if len(events) != 1:
            failures.append(f"[e2e-allow] expected exactly one audit line; got {events!r}")
        else:
            ev = events[0]
            if ev.get("slug") != "redis_flush":
                failures.append(f"[e2e-allow] slug mismatch: {ev!r}")
            if ev.get("command") != "redis-cli flushall":
                failures.append(f"[e2e-allow] command mismatch: {ev!r}")
            if not ev.get("ts_utc"):
                failures.append(f"[e2e-allow] ts_utc must be present: {ev!r}")
            if "ticket" in ev:
                failures.append(f"[e2e-allow] no ticket field expected in v1: {ev!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_enforce_deny_appends_nothing(failures):
    work = tempfile.mkdtemp(prefix="ans-audit-e2e-")
    try:
        audit_path = os.path.join(work, "audit.jsonl")
        payload = {"tool_name": "Bash", "tool_input": {"command": "redis-cli flushall"}}
        # No UE_CONSENT -> DENY.
        p = _run_enforce(payload, {"UE_CONSENT_AUDIT": audit_path})
        try:
            out = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            out = {}
        denied = out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if not denied:
            failures.append(f"[e2e-deny] uncontested command must DENY (out={p.stdout!r})")
        if os.path.exists(audit_path):
            failures.append("[e2e-deny] a DENY must append nothing to the audit trail")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_enforce_consent_allow_no_audit_env_no_crash(failures):
    payload = {"tool_name": "Bash", "tool_input": {"command": "redis-cli flushall"}}
    consent_json = json.dumps({"redis_flush": {"allowed": True}})
    # UE_CONSENT_AUDIT deliberately unset.
    p = _run_enforce(payload, {"UE_CONSENT": consent_json})
    if p.returncode != 0:
        failures.append(f"[e2e-no-audit-env] hook must not crash without UE_CONSENT_AUDIT "
                         f"(rc={p.returncode}, stderr={p.stderr!r})")
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except ValueError:
        out = {}
    denied = out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    if denied:
        failures.append(f"[e2e-no-audit-env] the consent-ALLOW itself must still work (out={p.stdout!r})")


def test_report_renders_both_parts(failures):
    manifest = {"redis_flush": {"allowed": True}, "send_email": {"allowed": True},
                "force_push": {"allowed": False}}
    events = [{"ts_utc": "2026-07-14T00:00:00Z", "slug": "redis_flush", "command": "redis-cli flushall"}]
    report = build_report([_outcome()], consent_manifest=manifest, consent_events=events)
    if "Pre-authorized this run" not in report:
        failures.append("[report-both] manifest section missing")
    if "redis_flush" not in report or "send_email" not in report:
        failures.append("[report-both] expected allowed slugs not both listed")
    if "force_push" in report.split("Pre-authorized this run")[1].split("\n")[0]:
        failures.append("[report-both] a NOT-allowed slug must not be listed as pre-authorized")
    if "action(s) ran under consent" not in report:
        failures.append("[report-both] events section missing")
    if "redis-cli flushall" not in report:
        failures.append("[report-both] event command missing from report")


def test_report_collapses_newline_in_command(failures):
    events = [{"ts_utc": "2026-07-14T00:00:00Z", "slug": "redis_flush",
               "command": "redis-cli flushall\nrm -rf /\r\nextra"}]
    report = build_report([_outcome()], consent_events=events)
    line = [ln for ln in report.splitlines() if "redis-cli flushall" in ln]
    if len(line) != 1:
        failures.append(f"[report-newline] expected exactly one report line for the event; got {line!r}")
    elif "\n" in line[0] or "\r" in line[0]:
        failures.append(f"[report-newline] embedded newline/CR survived into the line: {line[0]!r}")
    if "rm -rf /" not in "".join(line):
        failures.append("[report-newline] collapsed command text must still be present")


def test_report_empty_manifest_and_events_omits_section(failures):
    report = build_report([_outcome()], consent_manifest={}, consent_events=[])
    if "Pre-authorized this run" in report or "ran under consent" in report:
        failures.append("[report-empty] section must be entirely absent when both are empty")
    report_none = build_report([_outcome()])  # defaults
    if "Pre-authorized this run" in report_none or "ran under consent" in report_none:
        failures.append("[report-empty] defaults (no kwargs) must also omit the section")


def test_existing_callers_unaffected_by_defaults(failures):
    """run.py cmd_report and driver.py's terminal report both call build_report WITHOUT the two
    new kwargs — must keep working byte-identical to a call with explicit empty provenance."""
    outcomes = [_outcome()]
    baseline = build_report(outcomes, run_label="unattended run", backup_refs=())
    explicit_empty = build_report(outcomes, run_label="unattended run", backup_refs=(),
                                  consent_manifest=None, consent_events=None)
    if baseline != explicit_empty:
        failures.append("[bc] omitting the new kwargs must be byte-identical to passing None")

    driver_style = build_report(outcomes, run_label="unattended run", halted=False, halt_reason="",
                                stopped_low_yield=False, notes=[], work_branch=None, backup_refs=())
    if "Pre-authorized this run" in driver_style or "ran under consent" in driver_style:
        failures.append("[bc] driver-style call (no provenance kwargs) must show no consent section")


def test_frozen_consent_provenance_reads_env(failures):
    import unittest.mock
    work = tempfile.mkdtemp(prefix="ans-audit-provenance-")
    try:
        audit_path = os.path.join(work, "audit.jsonl")
        consent_store.append_audit(audit_path, {"ts_utc": "2026-07-14T00:00:00Z",
                                                  "slug": "redis_flush", "command": "redis-cli flushall"})
        env = {"UE_CONSENT": json.dumps({"redis_flush": {"allowed": True}}),
               "UE_CONSENT_AUDIT": audit_path}
        with unittest.mock.patch.dict(os.environ, env):
            manifest, events = frozen_consent_provenance()
        if manifest != {"redis_flush": {"allowed": True}}:
            failures.append(f"[provenance-env] manifest mismatch: {manifest!r}")
        if len(events) != 1 or events[0].get("slug") != "redis_flush":
            failures.append(f"[provenance-env] events mismatch: {events!r}")

        # Malformed/missing env -> fail safe, never raises.
        with unittest.mock.patch.dict(os.environ, {"UE_CONSENT": "{bad", "UE_CONSENT_AUDIT": ""}):
            manifest2, events2 = frozen_consent_provenance()
        if manifest2 != {}:
            failures.append(f"[provenance-env] malformed UE_CONSENT must read as {{}}; got {manifest2!r}")
        if events2 != []:
            failures.append(f"[provenance-env] empty UE_CONSENT_AUDIT must read as []; got {events2!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    failures = []
    test_append_read_round_trip(failures)
    test_read_audit_missing_path(failures)
    test_read_audit_skips_malformed_line(failures)
    test_audit_path_out_of_repo(failures)
    test_enforce_consent_allow_appends_one_line(failures)
    test_enforce_deny_appends_nothing(failures)
    test_enforce_consent_allow_no_audit_env_no_crash(failures)
    test_report_renders_both_parts(failures)
    test_report_collapses_newline_in_command(failures)
    test_report_empty_manifest_and_events_omits_section(failures)
    test_existing_callers_unaffected_by_defaults(failures)
    test_frozen_consent_provenance_reads_env(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — consent audit trail / report provenance not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — consent audit round-trips fail-safe, enforce.py logs exactly one "
          "line per consent-ALLOW (none on DENY, never crashes without UE_CONSENT_AUDIT), and the "
          "report renders provenance from the frozen env with BC defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
