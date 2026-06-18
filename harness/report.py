"""Morning report — the local canonical, provider-neutral record of what the night did.

The council's mandate: per-ticket outcome + the WHY + the exact human next-action, severity
ranked, with a LOW-YIELD headline when most work was parked/low-confidence (so "the run
finished" can never be mistaken for "the work got done"). This is the local markdown source of
truth; Paperclip mirroring + push are Slice-2/Phase-2.
"""
from __future__ import annotations

import re

from .redact import redact
from .state import OutcomeState

# A token safe to interpolate into the copy-pasteable `ans-run --agent <name> <ids>` command the
# report emits — same slug rule the ticket loader enforces on a declared agent. Applied to BOTH the
# agent name AND the ticket ids (redact() scrubs secrets, not shell syntax), so neither half of that
# command can carry whitespace / shell-meta / a newline a careless operator would paste or that would
# forge report lines.
_CMD_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Severity order for the report (worst/most-actionable first).
_ORDER = [
    OutcomeState.FAILED_BUG_IN_AGENT,
    OutcomeState.FAILED_RETRYABLE,
    OutcomeState.BLOCKED_ENV,
    OutcomeState.PARKED_FOUNDATIONAL,
    OutcomeState.PARKED_DECISION,
    OutcomeState.DONE_LOW_CONFIDENCE,
    OutcomeState.DONE,
]

_LOW_YIELD = {
    OutcomeState.PARKED_DECISION, OutcomeState.PARKED_FOUNDATIONAL,
    OutcomeState.BLOCKED_ENV, OutcomeState.FAILED_RETRYABLE,
    OutcomeState.FAILED_BUG_IN_AGENT, OutcomeState.DONE_LOW_CONFIDENCE,
}


def build_report(outcomes: list, *, run_label: str = "unattended run",
                 halted: bool = False, halt_reason: str = "",
                 stopped_low_yield: bool = False, notes=(),
                 work_branch: str | None = None,
                 active_agent: str | None = None, agent_hints: dict | None = None) -> str:
    total = len(outcomes)
    done = sum(1 for o in outcomes if o.state == OutcomeState.DONE)
    low_yield = sum(1 for o in outcomes if o.state in _LOW_YIELD)
    lines: list[str] = []

    lines.append(f"# Morning report — {run_label}")
    lines.append("")
    if halted:
        lines.append(f"> ⛔ RUN HALTED: {halt_reason}")
        lines.append("")
    if stopped_low_yield or (total and low_yield / total > 0.5):
        lines.append("> ⚠️ **LOW-YIELD NIGHT — most work is parked/low-confidence. "
                     "Review before trusting any 'done'.**")
        lines.append("")
    # Run-level blind spots (e.g. review ran without a tokonomix credential) — surfaced so "the run
    # finished" can never hide "a whole quality layer was off all night".
    for note in (notes or ()):
        lines.append(f"> ⚠️ **BLIND SPOT:** {note}")
        lines.append("")
    # Council "needs daylight review": gates passed but a high-risk diff was not cleanly vetted, so
    # the work is NOT auto-trusted. Surface it up top — it looks done but must not be merged blind.
    daylight = [o for o in outcomes if "NEEDS-DAYLIGHT-REVIEW" in (o.review_coverage or "")]
    if daylight:
        lines.append(f"> 🔎 **{len(daylight)} HIGH-RISK change(s) NEED DAYLIGHT REVIEW before merge "
                     "(gates passed, but the council flagged/couldn't vet them):**")
        for o in daylight:
            lines.append(f">   - {o.ticket_id}: {o.human_action_required or o.why}")
        lines.append("")
    # Credits-degraded run (policy B): councils were skipped because Tokonomix credits ran out, so a
    # whole verification layer was off. Surface it loudly and separately — "done" here means
    # "the local agent did it, unreviewed", which must never be mistaken for verified-done.
    degraded = [o for o in outcomes if "credits-degrade" in (o.review_coverage or "")]
    if degraded:
        lines.append(f"> 💸 **RAN IN DEGRADED MODE — Tokonomix credits exhausted. "
                     f"{len(degraded)} ticket(s) completed WITHOUT consensus (unverified, "
                     "needs daylight review). Top up credits and re-verify before trusting them:**")
        for o in degraded:
            lines.append(f">   - {o.ticket_id}: {o.why}")
        lines.append("")
    # Where the work lives (INT-1825 bug 2): the run is isolated to its own branch, so point the
    # operator at it explicitly — this is an info pointer, NOT a blind spot.
    if work_branch:
        lines.append(f"> 📦 This run's work is on branch `{work_branch}` (your branch was left "
                     f"untouched). Review and merge it: `git merge {work_branch}`.")
        lines.append("")
    # F2-declarative: tickets may carry an `agent:` front-matter hint. The run NEVER switches CLIs
    # mid-flight (that nested design was rejected) — so here we only GROUP processed/parked tickets
    # whose declared agent differs from the run's active agent and RECOMMEND a focused follow-up.
    # Info-level pointer, never an automatic action. Only emitted when the active agent is known
    # (a hint can't "differ" from an unknown active agent).
    if agent_hints and active_agent:
        outcome_ids = {o.ticket_id for o in outcomes}
        by_agent: dict = {}
        for tid in sorted(agent_hints):                 # deterministic id order in the command
            want = agent_hints[tid]
            if tid in outcome_ids and want and want != active_agent:
                by_agent.setdefault(want, []).append(tid)
        for want in sorted(by_agent):                   # deterministic per-agent grouping
            ids = by_agent[want]
            # Withhold the convenience command if ANY id (or the agent) is not command-safe — never
            # emit a half-built or injectable command. The ticket(s) still appear in their per-state
            # section, so nothing is dropped from the report; only the paste-ready line is withheld.
            if not (_CMD_SAFE.match(want) and all(_CMD_SAFE.match(tid) for tid in ids)):
                # Echo `want` only when it is itself slug-safe; never interpolate an unsafe token.
                label = f"agent `{want}`" if _CMD_SAFE.match(want) else "a different agent"
                lines.append(f"> 💡 {len(ids)} ticket(s) requested {label} (re-run command withheld "
                             "— an unsafe ticket id; see the per-state sections below)")
                lines.append("")
                continue
            lines.append(f"> 💡 {len(ids)} ticket(s) requested a different agent — re-run: "
                         f"`ans-run --agent {want} {' '.join(ids)}`")
            lines.append("")

    lines.append(f"**{done}/{total} DONE clean.** "
                 f"{low_yield} need attention (parked / blocked / failed / low-confidence).")
    lines.append("")

    by_state = {}
    for o in outcomes:
        by_state.setdefault(o.state, []).append(o)

    for state in _ORDER:
        items = by_state.get(state)
        if not items:
            continue
        lines.append(f"## {state.value} ({len(items)})")
        for o in items:
            lines.append(f"- **{o.ticket_id}** — {o.why}")
            if o.human_action_required:
                lines.append(f"  - next action: {o.human_action_required}")
            if o.exact_blocker:
                lines.append(f"  - blocker: {o.exact_blocker}")
            if o.artifact_path:
                lines.append(f"  - artifact: {o.artifact_path}")
            if o.category:
                lines.append(f"  - category: {o.category}")
        lines.append("")

    # Single chokepoint: scrub any secret that leaked into an outcome field (attempted summary,
    # blocker, gate excerpt) before it lands in the human-readable report.
    return redact("\n".join(lines).rstrip() + "\n")
