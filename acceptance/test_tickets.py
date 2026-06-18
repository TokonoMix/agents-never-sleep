#!/usr/bin/env python3
"""ANS-OSS F2-declarative — ticket front-matter `agent:` hint + report grouping.

Proves the guarantees F2 exists for (metadata ONLY — the run never switches CLIs mid-flight):
  * a ticket with `agent: codex` front-matter parses and EXPOSES the value (Ticket.declared_agent);
  * a ticket WITHOUT the key is unchanged — declared_agent is None, body/meta intact;
  * the morning report emits the grouped follow-up recommendation ONLY when a declared agent
    differs from the run's active agent, naming the right ticket ids + the `ans-run --agent` command;
  * it does NOT recommend when the hint equals the active agent, when no active agent is known,
    or for a ticket that declared a hint but was never processed (no outcome).

Exit 0 = GREEN.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from harness.tickets import load_tickets                 # noqa: E402
from harness.report import build_report                  # noqa: E402
from harness.state import TicketOutcome, OutcomeState    # noqa: E402


def _ticket_dir(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="ue-f2-tickets-")
    for name, text in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


def _outcome(tid: str, state=OutcomeState.DONE) -> TicketOutcome:
    return TicketOutcome(ticket_id=tid, state=state, why="did the thing")


def test_agent_hint_parses_and_exposes(failures):
    d = _ticket_dir({"t1.md": "---\nid: T1\nagent: codex\n---\nDo the work in the body.\n"})
    tickets = load_tickets(d)
    if len(tickets) != 1:
        failures.append(f"[parse] expected 1 ticket, got {len(tickets)}")
        return
    t = tickets[0]
    if t.declared_agent != "codex":
        failures.append(f"[parse] declared_agent expected 'codex', got {t.declared_agent!r}")
    if "Do the work in the body." not in t.body:
        failures.append(f"[parse] body lost/garbled: {t.body!r}")
    if t.meta.get("agent") != "codex":
        failures.append(f"[parse] meta['agent'] not captured: {t.meta!r}")


def test_no_agent_hint_is_unchanged(failures):
    d = _ticket_dir({"t2.md": "---\nid: T2\nblast_radius: file\n---\nNo agent hint here.\n"})
    t = load_tickets(d)[0]
    if t.declared_agent is not None:
        failures.append(f"[no-hint] declared_agent should be None, got {t.declared_agent!r}")
    if "No agent hint here." not in t.body:
        failures.append("[no-hint] body altered")
    # An agent: key whose value is blank must also read as None (not "").
    d2 = _ticket_dir({"t3.md": "---\nid: T3\nagent:\n---\nBlank value.\n"})
    if load_tickets(d2)[0].declared_agent is not None:
        failures.append("[no-hint] blank agent: value should read as None")


def test_unsafe_agent_value_is_rejected(failures):
    # A ticket author controls `agent:`; it is interpolated into a copy-pasteable command in the
    # report. A value with a shell metachar / space must read as NO hint (the boundary chokepoint),
    # so it can never inject into the recommended `ans-run --agent …` command.
    d = _ticket_dir({"bad.md": "---\nid: B1\nagent: codex; rm -rf ~\n---\nEvil hint.\n"})
    if load_tickets(d)[0].declared_agent is not None:
        failures.append("[unsafe] a shell-injection agent value should read as None")
    # And it must NOT appear in any report recommendation.
    outcomes = [_outcome("B1")]
    hints = {"B1": load_tickets(d)[0].declared_agent}  # None -> filtered by run.py's truthy guard
    report = build_report(outcomes, active_agent="claude",
                          agent_hints={k: v for k, v in hints.items() if v})
    if "rm -rf" in report:
        failures.append("[unsafe] injection payload reached the report command")


def test_report_recommends_only_on_difference(failures):
    outcomes = [_outcome("T1"), _outcome("T2", OutcomeState.PARKED_DECISION), _outcome("T3")]
    # T1 wants codex (differs), T2 wants codex (differs, and was PARKED — still processed),
    # T3 wants claude (== active, no rec). T9 wants gemini but has NO outcome (never processed).
    hints = {"T1": "codex", "T2": "codex", "T3": "claude", "T9": "gemini"}
    report = build_report(outcomes, active_agent="claude", agent_hints=hints)

    line = "> 💡 2 ticket(s) requested a different agent — re-run: `ans-run --agent codex T1 T2`"
    if line not in report:
        failures.append(f"[report] expected grouped codex recommendation; report was:\n{report}")
    if "--agent claude" in report:
        failures.append("[report] recommended re-running on the ACTIVE agent (claude) — wrong")
    if "--agent gemini" in report:
        failures.append("[report] recommended a NEVER-PROCESSED ticket (T9/gemini) — wrong")


def test_report_withholds_command_on_unsafe_ticket_id(failures):
    # Sibling-asymmetry guard: the agent name is slug-validated, but the ticket IDS are also
    # interpolated into the copy-pasteable command (a crafted .md filename -> a shell-meta id).
    # Both halves must be command-safe or the whole command is withheld — never half-built/injectable.
    outcomes = [_outcome("good-1"), _outcome("evil; rm -rf ~")]
    hints = {"good-1": "codex", "evil; rm -rf ~": "codex"}
    report = build_report(outcomes, active_agent="claude", agent_hints=hints)
    # The payload legitimately appears as prose in the per-state section (a pre-existing ticket-id
    # surface); what must NEVER happen is it appearing inside a runnable `ans-run` command.
    if "ans-run --agent codex evil" in report or "rm -rf ~`" in report:
        failures.append("[id-asym] an unsafe ticket id reached the recommended command")
    if "command withheld" not in report:
        failures.append(f"[id-asym] expected the command to be withheld; report:\n{report}")


def test_report_silent_when_no_difference_or_no_active(failures):
    outcomes = [_outcome("T1"), _outcome("T2")]
    # All hints equal the active agent -> no recommendation at all.
    r1 = build_report(outcomes, active_agent="codex", agent_hints={"T1": "codex", "T2": "codex"})
    if "requested a different agent" in r1:
        failures.append("[silent] recommended despite every hint matching the active agent")
    # Active agent unknown -> a hint cannot 'differ', so stay silent (conservative).
    r2 = build_report(outcomes, active_agent=None, agent_hints={"T1": "codex"})
    if "requested a different agent" in r2:
        failures.append("[silent] recommended with no known active agent")
    # No hints at all -> unchanged report.
    r3 = build_report(outcomes)
    if "requested a different agent" in r3:
        failures.append("[silent] recommended with no hints supplied")


def main() -> int:
    failures: list = []
    test_agent_hint_parses_and_exposes(failures)
    test_no_agent_hint_is_unchanged(failures)
    test_unsafe_agent_value_is_rejected(failures)
    test_report_recommends_only_on_difference(failures)
    test_report_withholds_command_on_unsafe_ticket_id(failures)
    test_report_silent_when_no_difference_or_no_active(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — F2 declarative agent-hint guarantees not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — agent: front-matter parses & exposes, and the report groups "
          "differing-agent tickets into an `ans-run --agent` recommendation (metadata only, "
          "no CLI switch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
