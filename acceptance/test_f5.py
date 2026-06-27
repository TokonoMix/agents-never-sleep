#!/usr/bin/env python3
"""F5 — consensus-assisted PARK resolution (build-narrow). Step-1 unit proof of the PURE core.

F5 lets a ticket that is about to PARK *only* because its requirement meaning is ambiguous get one
grounded consensus call that may DOWNGRADE the risk to PROCEED — but only on cited evidence, and
only for locally-reversible FILE-scoped work. This test pins the guardrails that make that safe
(architect + 3-vendor review, 2026-06-10):

  * ELIGIBILITY is deterministic and narrow — F5 is *unreachable* for any hard category, HALT,
    missing safety net, or a second attempt. It can only ever touch the `requirement_meaning`
    branch of decide.classify().
  * INTERPRETATION is downgrade-only and evidence-gated — RESOLVE only on a resolved, single-reading,
    evidence-cited, zero-dissent, concern-language-free verdict. Anything short stays PARK. It NEVER
    escalates (PROCEED never becomes PARK here).

No network, no driver, no live loop — those are Step 2. Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep import f5  # noqa: E402
from agents_never_sleep.decide import classify, Action  # noqa: E402


def _req_meaning_decision():
    # A ticket that classify() routes to the requirement_meaning PARK branch.
    d = classify("Add a widget — unclear which kind of widget?", unattended=True,
                 has_safety_net=True)
    assert d.action == Action.PARK and d.category == "requirement_meaning", d
    return d


def test_tag_only_on_requirement_meaning(failures):
    d = _req_meaning_decision()
    if not getattr(d, "consensus_resolvable", False):
        failures.append("[tag] requirement_meaning PARK should be tagged consensus_resolvable")
    # A hard-category PARK must NOT be tagged.
    hard = classify("Run a database schema migration to add a column", unattended=True,
                    has_safety_net=True)
    if hard.action != Action.PARK or hard.consensus_resolvable:
        failures.append(f"[tag] hard-category PARK must not be consensus_resolvable: {hard}")
    # A routine PROCEED must not be tagged.
    proc = classify("Rename a local helper variable", unattended=True, has_safety_net=True)
    if proc.consensus_resolvable:
        failures.append("[tag] PROCEED must not be consensus_resolvable")
    # HALT (no safety net) must not be tagged.
    halt = classify("Add a widget — which kind?", unattended=True, has_safety_net=False)
    if halt.action == Action.PARK or halt.consensus_resolvable:
        failures.append(f"[tag] no-safety-net must HALT and never be consensus_resolvable: {halt}")


def test_eligibility_is_narrow(failures):
    d = _req_meaning_decision()
    if not f5.eligible(d, has_safety_net=True, already_attempted=False):
        failures.append("[elig] requirement_meaning + safety net + first attempt should be eligible")
    if f5.eligible(d, has_safety_net=True, already_attempted=True):
        failures.append("[elig] a second attempt (one per lifetime) must NOT be eligible")
    if f5.eligible(d, has_safety_net=False, already_attempted=False):
        failures.append("[elig] no safety net must NOT be eligible")
    hard = classify("Change the public API contract response shape", unattended=True,
                    has_safety_net=True)
    if f5.eligible(hard, has_safety_net=True, already_attempted=False):
        failures.append("[elig] a hard-category PARK must NOT be eligible")
    proc = classify("Rename a local variable", unattended=True, has_safety_net=True)
    if f5.eligible(proc, has_safety_net=True, already_attempted=False):
        failures.append("[elig] a PROCEED must NOT be eligible (F5 never escalates)")


def test_interpret_downgrade_only_and_evidence_gated(failures):
    # The one path that RESOLVES: resolved + single reading + cited evidence + no dissent + clean synth.
    good = f5.F5Verdict(resolved=True, chosen_reading="Reading A: a status badge",
                        evidence="components/Badge.tsx already renders status; the ticket links it",
                        dissent_count=0, synthesis_text="The context clearly intends reading A.")
    res, _ = f5.interpret_verdict(good)
    if res != f5.F5Result.RESOLVE:
        failures.append(f"[interp] strong evidence-grounded verdict should RESOLVE, got {res}")

    # Every short-of-strong verdict must KEEP_PARKED:
    cases = {
        "unresolved": f5.F5Verdict(resolved=False, chosen_reading="A", evidence="x"),
        "no-reading": f5.F5Verdict(resolved=True, chosen_reading="  ", evidence="x"),
        "no-evidence": f5.F5Verdict(resolved=True, chosen_reading="A", evidence="  "),
        "dissent": f5.F5Verdict(resolved=True, chosen_reading="A", evidence="x", dissent_count=1),
        "concern-language": f5.F5Verdict(resolved=True, chosen_reading="A", evidence="x",
                                         dissent_count=0,
                                         synthesis_text="It could be either reading, unclear."),
    }
    for name, v in cases.items():
        res, why = f5.interpret_verdict(v)
        if res != f5.F5Result.KEEP_PARKED:
            failures.append(f"[interp] '{name}' must KEEP_PARKED, got {res} ({why})")


def test_prompt_asks_to_disambiguate_not_to_proceed(failures):
    p = f5.build_grounding_prompt(
        ticket_title="Add a widget", ticket_body="unclear which kind",
        candidate_readings=["a status badge", "a settings panel"],
        repo_context="components/Badge.tsx exists", safety_net_desc="git revert available")
    low = p.lower()
    if "should i proceed" in low or "should we proceed" in low:
        failures.append("[prompt] must NOT ask 'should I proceed' — that is the dangerous framing")
    if "evidence" not in low or "disambiguat" not in low:
        failures.append("[prompt] must ask to disambiguate using cited evidence")
    if "do not guess" not in low:
        failures.append("[prompt] must instruct: no evidence -> undetermined, do not guess")


def main() -> int:
    failures = []
    test_tag_only_on_requirement_meaning(failures)
    test_eligibility_is_narrow(failures)
    test_interpret_downgrade_only_and_evidence_gated(failures)
    test_prompt_asks_to_disambiguate_not_to_proceed(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — F5 core guardrails not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — narrow eligibility, downgrade-only evidence-gated interpretation, "
          "and disambiguate-not-proceed prompt all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
