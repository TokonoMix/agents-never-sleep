"""F5 — consensus-assisted PARK resolution (build-narrow). PURE core; no network, no live loop.

The crux (architect + 3-vendor review, 2026-06-10): ANS exists to PARK rather than gamble. A
consensus that confidently hallucinates an "unblock" would convert a safe PARK into a bad PROCEED —
the exact failure ANS prevents. So F5 is defensible ONLY as a downgrade-only, evidence-gated,
one-shot, narrowly-eligible amplifier — strip any of those four and it becomes that failure.

This module is the deterministic half (mirrors the decide.py / council.py split): it decides
ELIGIBILITY and INTERPRETS the verdict. The actual consensus call is made by the agent via the
tokonomix MCP (the harness can't call LLMs) — the agent reports a structured `F5Verdict` back, and
`interpret_verdict` applies the hard, deterministic downgrade-only gate. The DECISION stays
deterministic even though the evidence-gathering is a model call.

Eligibility is intentionally NARROW: F5 can only ever touch the `requirement_meaning` PARK branch of
decide.classify() (FILE-scoped, foundational=False, reversible), once per ticket lifetime. A wrong
unblock there is at worst a revertible FILE-scoped commit that the normal post-edit gate + council +
daylight review still catch. NEVER credentials, dependencies, foundational/blast-radius, money, or
HALT — those are facts/authority, not interpretation, and no number of correlated models can supply
them.
"""
from __future__ import annotations

import dataclasses
import enum
import re

from .decide import Action


class F5Result(str, enum.Enum):
    RESOLVE = "RESOLVE"          # consensus disambiguated on cited evidence -> allow PROCEED
    KEEP_PARKED = "KEEP_PARKED"  # anything short of strong+grounded -> stays PARK


# Hedge/concern language in the judge synthesis -> treat as no-resolution (mirrors the intent of
# council.reconcile's concern sniff). A synthesis that itself waffles is not a strong resolution.
_CONCERN = re.compile(
    r"\b(unclear|ambiguous|could be either|either reading|both readings|not sure|uncertain|"
    r"insufficient|cannot determine|undetermined|no clear|hard to say|needs? clarification|"
    r"either way|might be|may be)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass
class F5Verdict:
    """What the agent reports AFTER running the grounded consensus via the tokonomix MCP.

    The agent fills these from the council result; `interpret_verdict` then applies the
    deterministic gate. Keeping the gate here (not in the prompt) is what makes a wrong model
    answer fail closed."""
    resolved: bool                  # did the council reach a single-reading resolution?
    chosen_reading: str = ""        # the reading it picked
    evidence: str = ""              # exact evidence cited FROM the repo/spec context
    dissent_count: int = 0          # proposers disagreeing with the chosen reading
    synthesis_text: str = ""        # the judge synthesis (concern-language backstop)


def eligible(decision, *, has_safety_net: bool, already_attempted: bool) -> bool:
    """F5 is structurally unreachable unless ALL hold: the decision is a `requirement_meaning`
    PARK (FILE-scoped, non-foundational), a reversibility safety net exists, and F5 has not already
    been attempted on this ticket (one call per lifetime — kills park-thrash on resume). Any hard
    category / HALT / PROCEED is ineligible by construction — F5 never escalates and never touches
    fact/authority/blast-radius parks."""
    return (
        decision.action == Action.PARK
        and getattr(decision, "consensus_resolvable", False)
        and decision.category == "requirement_meaning"
        and not decision.foundational
        and has_safety_net
        and not already_attempted
    )


def interpret_verdict(v: F5Verdict) -> tuple["F5Result", str]:
    """DOWNGRADE-ONLY and evidence-gated. PARK->PROCEED only on a resolved, single-reading,
    evidence-cited, zero-dissent, concern-language-free verdict. Anything short stays PARK. Never
    escalates. The evidence gate is the load-bearing one: a vote with no cited evidence is exactly
    the ungrounded-consensus anti-pattern the design warns against."""
    if not v.resolved:
        return F5Result.KEEP_PARKED, "consensus did not resolve the ambiguity"
    if not v.chosen_reading.strip():
        return F5Result.KEEP_PARKED, "no single reading chosen"
    if not v.evidence.strip():
        return F5Result.KEEP_PARKED, ("no cited evidence — a vote without grounding is not a "
                                      "resolution")
    if v.dissent_count > 0:
        return F5Result.KEEP_PARKED, f"{v.dissent_count} proposer(s) dissented — not low-dissent"
    if _CONCERN.search(v.synthesis_text or ""):
        return F5Result.KEEP_PARKED, "judge synthesis carries hedge/concern language"
    return F5Result.RESOLVE, f"disambiguated on cited evidence: {v.chosen_reading.strip()}"


def build_grounding_prompt(*, ticket_title: str, ticket_body: str, candidate_readings: list,
                           repo_context: str, safety_net_desc: str) -> str:
    """The grounded prompt. It asks the council to DISAMBIGUATE using cited evidence — explicitly
    NOT 'should I proceed?' (the most dangerous framing). A verdict with no cited evidence is
    treated as no-resolution by interpret_verdict, so the prompt tells the models to answer
    'undetermined' rather than guess."""
    readings = "\n".join(f"  - Reading {chr(65 + i)}: {r}"
                         for i, r in enumerate(candidate_readings))
    return (
        "A ticket is about to be PARKED because its requirement meaning is ambiguous. Do NOT decide "
        "whether to proceed. Decide ONLY this: does the provided repository/spec context "
        "DISAMBIGUATE which reading was intended — and if so, which one, citing the exact "
        "evidence?\n\n"
        f"TICKET: {ticket_title}\n{ticket_body}\n\n"
        f"CANDIDATE READINGS:\n{readings}\n\n"
        f"REPOSITORY / SPEC CONTEXT:\n{repo_context}\n\n"
        f"REVERSIBILITY: {safety_net_desc}\n\n"
        "Answer whether ONE reading is clearly intended BY THE CONTEXT (cite the exact evidence), "
        "or whether it is genuinely undetermined. If you cannot point to evidence in the context, "
        "the answer is 'undetermined' — do not guess."
    )
