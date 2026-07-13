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

Eligibility is intentionally NARROW: F5 always touches the `requirement_meaning` PARK branch of
decide.classify() (FILE-scoped, foundational=False, reversible), and ALSO any hard-PARK category a
project/ticket has explicitly opted into via `consensus_assisted_categories` — once per ticket
lifetime either way. For hard categories the consensus is asked a grounded SOUNDNESS question (not
disambiguation), a found defect is a deterministic PARK veto (interpret_soundness_verdict), and any
resolution is forced to DONE_LOW_CONFIDENCE + daylight review. NEVER credentials, dependencies, or
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
    defect_found: bool = False      # soundness path: the consensus found a CONCRETE defect -> hard
                                    # veto (interpret_soundness_verdict KEEP_PARKED regardless of
                                    # resolved/evidence). Default False keeps disambiguation identical.


def eligible(decision, *, has_safety_net: bool, already_attempted: bool,
             consensus_assisted_categories=()) -> bool:
    """F5 is structurally unreachable unless ALL hold: the decision is a PARK whose category is
    eligible (either `requirement_meaning`, which is ALWAYS eligible, or a hard-PARK category the
    project/ticket explicitly opted into via `consensus_assisted_categories`), a reversibility
    safety net exists, and F5 has not already been attempted on this ticket (one call per lifetime
    — kills park-thrash on resume).

    `consensus_assisted_categories` is the EFFECTIVE set already resolved by the caller (project
    default ± ticket override — see consensus_scope.effective_categories). This function does NOT
    read config or re-classify: it only checks membership, so the same set can be snapshotted into
    the durable offer record and re-checked verbatim at resolve time (anti-TOCTOU). The `()` default
    is a fail-closed safety net (empty/absent set = no hard categories opted in = today's behavior);
    real call sites still pass the effective set explicitly.

    The old `consensus_resolvable and not foundational` pair is deliberately GONE: four of the five
    hard categories are foundational, so keeping the foundational exclusion would make the opt-in a
    near no-op. The safety compensation for widening is the forced DONE_LOW_CONFIDENCE + daylight
    review on every non-requirement_meaning resolution (see orchestrator.resolve_park /
    _finalize_impl), NOT a narrower eligibility gate here."""
    category_ok = (
        decision.category == "requirement_meaning"
        or decision.category in consensus_assisted_categories
    )
    return (
        decision.action == Action.PARK
        and category_ok
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


def interpret_soundness_verdict(v: F5Verdict) -> tuple["F5Result", str]:
    """Hard-category (decision B) gate. A found defect is a DETERMINISTIC veto — KEEP_PARKED BEFORE
    the shared gate, so even a mis-framed resolved=True on a defect ('resolved: SQL injection at
    line 42') fails closed. This is the split the disambiguation gate can't express: there
    resolved=True has one meaning; here it must not be trusted to also mean 'proceed-safe'. With no
    defect, delegates to interpret_verdict UNCHANGED — RESOLVE still requires an evidence-cited,
    zero-dissent, hedge-free AFFIRMATIVE soundness conclusion."""
    if v.defect_found:
        return F5Result.KEEP_PARKED, "consensus found a concrete soundness defect — stays parked"
    return interpret_verdict(v)


def build_grounding_prompt(*, ticket_title: str, ticket_body: str, candidate_readings: list,
                           repo_context: str, safety_net_desc: str) -> str:
    """The grounded prompt. It asks the council to DISAMBIGUATE using cited evidence — explicitly
    NOT 'should I proceed?' (the most dangerous framing). A verdict with no cited evidence is
    treated as no-resolution by interpret_verdict, so the prompt tells the models to answer
    'undetermined' rather than guess.

    `repo_context` MUST be a short pointer/summary, never a raw repo blob: bulk grounding
    content is routed AGENT-SIDE via consensus_context.plan_grounding (inline/upload/degrade) per
    SKILL.md — this builder only inlines whatever string it is handed and applies no size guard, so
    handing it bulk content would blind-inline an over-budget context."""
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


def build_soundness_prompt(*, ticket_title: str, ticket_body: str, category: str,
                           repo_context: str, safety_net_desc: str) -> str:
    """The grounded prompt for an OPTED-IN HARD category (design decision B). Unlike the
    disambiguation prompt, the requirement is NOT ambiguous — the customer already decided WHAT to
    build. This asks the council for a grounded SOUNDNESS verdict on that already-decided change,
    explicitly NOT 'should I proceed?' (the most dangerous framing) and NOT 'which reading?'.

    Interpreted by interpret_soundness_verdict: the agent reports `resolved=True` + `defect_found=
    False` + cited evidence ONLY when the context AFFIRMATIVELY establishes soundness; a concrete
    cited defect is reported `defect_found=True` (hard veto -> PARK), and an ungrounded 'looks fine'
    fails the shared evidence gate -> PARK. So the prompt tells the models to answer 'undetermined'
    rather than guess.

    `repo_context` MUST be a short pointer/summary, never a raw repo blob (same contract as
    build_grounding_prompt): bulk grounding content is routed AGENT-SIDE via
    consensus_context.plan_grounding per SKILL.md — this builder applies no size guard."""
    return (
        f"A ticket in the high-risk category '{category}' is about to be PARKED for human review. "
        "The requirement is NOT ambiguous — assume the author already decided this change is wanted. "
        "Do NOT decide whether to proceed. Decide ONLY this: does the provided repository/spec "
        "context affirmatively establish that applying this already-decided change is SOUND — "
        "reversible, correctly scoped, with no data loss, no contract/interface break, and no "
        "security or tenant-isolation hole — citing the EXACT evidence? Or is there a concrete, "
        "cited defect that should keep it parked?\n\n"
        f"TICKET: {ticket_title}\n{ticket_body}\n\n"
        f"RISK AREA: {category}\n\n"
        f"REPOSITORY / SPEC CONTEXT:\n{repo_context}\n\n"
        f"REVERSIBILITY: {safety_net_desc}\n\n"
        "State in one line the soundness conclusion (what is sound and why), citing the exact "
        "evidence from the context. If you find a concrete defect, say so explicitly (it will keep "
        "the ticket parked). If the context does not let you GROUND an affirmative soundness "
        "conclusion in cited evidence, the answer is 'undetermined' — do not guess. An ungrounded "
        "'looks fine' is not a resolution."
    )
