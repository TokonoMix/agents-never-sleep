"""Morning report — the local canonical, provider-neutral record of what the night did.

The council's mandate: per-ticket outcome + the WHY + the exact human next-action, severity
ranked, with a LOW-YIELD headline when most work was parked/low-confidence (so "the run
finished" can never be mistaken for "the work got done"). This is the local markdown source of
truth; Paperclip mirroring + push are Slice-2/Phase-2.
"""
from __future__ import annotations

import json
import os
import re

from . import consent_store
from .redact import redact
from .state import OutcomeState

# A token safe to interpolate into the copy-pasteable `ans-run --agent <name> <ids>` command the
# report emits — same slug rule the ticket loader enforces on a declared agent. Applied to BOTH the
# agent name AND the ticket ids (redact() scrubs secrets, not shell syntax), so neither half of that
# command can carry whitespace / shell-meta / a newline a careless operator would paste or that would
# forge report lines..
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


def council_blind_spots(outcomes: list, progress: dict | None) -> list:
    """Run-level council blind spots, derived DETERMINISTICALLY from what the run recorded.

    INT-2658: a transient gateway 502 on the first ticket made a worker conclude the gateway was
    down for the whole run and stop convening councils entirely from ticket 4 on. The gateway was
    provably fine minutes later. The run report said the backlog finished; the fact that the
    compensating cross-vendor review had been off for two thirds of it was only visible by reading
    every per-ticket `review_coverage` field by hand.

    Two signals the harness can see without calling any LLM:

      1. **A RUN of consecutive council errors.** One error is a transient upstream hiccup and
         needs no banner. Two or more IN A ROW is the footprint of generalisation — the worker
         caching a negative result across tickets — which is exactly the failure mode. Reported in
         RECORDING order (`updated_at`), not filename order, because `OutcomeStore.all()` sorts by
         filename and a backlog whose ids don't sort in processing order would otherwise show a
         run that never happened.
      2. **`council_calls > 0` while `council_cost_eur == 0.0`.** Calls were counted but nothing
         was charged. Those two cannot both be true, so at least one is not what it claims — the
         €-cap brake reads the same number, so a silent contradiction here also means the brake is
         being fed a fiction.

    Fail-safe by construction: a missing/garbage `progress` degrades to "no cost signal" and never
    raises — a report that crashes on its own diagnostics is worse than one missing a banner.
    """
    notes: list = []

    ordered = sorted(outcomes or [], key=lambda o: (getattr(o, "updated_at", 0.0) or 0.0))
    best_len, best_first, best_last = 0, None, None
    cur_len, cur_first = 0, None
    for o in ordered:
        if "council:error" in (o.review_coverage or ""):
            cur_len += 1
            if cur_len == 1:
                cur_first = o.ticket_id
            if cur_len > best_len:
                best_len, best_first, best_last = cur_len, cur_first, o.ticket_id
        else:
            cur_len, cur_first = 0, None
    if best_len >= 2:
        notes.append(
            f"COUNCIL OFF FOR {best_len} CONSECUTIVE TICKET(S) ({best_first} → {best_last}) — "
            "the cross-vendor review that compensates for the PROCEED overrides did not run on "
            "that stretch, so those diffs are effectively unreviewed no matter what their outcome "
            "says. A gateway error is TRANSIENT: it must be retried within the ticket and must "
            "never be cached across tickets (INT-2658). Re-review those diffs in daylight."
        )

    try:
        calls = int((progress or {}).get("council_calls", 0) or 0)
    except (TypeError, ValueError):
        calls = 0
    try:
        cost = float((progress or {}).get("council_cost_eur", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if calls > 0 and cost == 0.0:
        notes.append(
            f"CONTRADICTORY COUNCIL ACCOUNTING — {calls} council call(s) recorded but €0.00 "
            "charged. Either the councils did not actually run, or their real cost was never "
            "reported back via --council-cost. The per-run €-brake reads this same number, so it "
            "is currently being fed a fiction (INT-2658)."
        )

    return notes


def frozen_consent_provenance() -> tuple[dict, list]:
    """The report's ONLY source for consent provenance (t04) — the env vars the launcher froze at
    launch time (UE_CONSENT, UE_CONSENT_AUDIT), NEVER a re-read of the consent store by repo path.
    An isolated-worktree run has a different realpath than where the wizard captured consent, so
    a store re-read keyed on the CURRENT repo would miss the grant entirely; the env is already
    pinned to the primary repo and inherited by whatever in-session agent renders this report.

    Fail-safe: a missing/malformed UE_CONSENT reads as {} (never raises); a non-dict payload is
    also {}. UE_CONSENT_AUDIT unset/unreadable -> [] (read_audit is itself fail-safe).
    """
    try:
        manifest = json.loads(os.environ.get("UE_CONSENT") or "{}")
    except ValueError:
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    audit = consent_store.read_audit(os.environ.get("UE_CONSENT_AUDIT") or "")
    return manifest, audit


def build_report(outcomes: list, *, run_label: str = "unattended run",
                 halted: bool = False, halt_reason: str = "",
                 stopped_low_yield: bool = False, notes=(),
                 work_branch: str | None = None,
                 active_agent: str | None = None, agent_hints: dict | None = None,
                 backup_refs=(),
                 consent_manifest: dict | None = None,
                 consent_events: list | None = None) -> str:
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
    # F5 (build-narrow, requirement_meaning + opted-in hard-PARK categories, spec §6): a PARK whose
    # ticket got a grounded consensus attempt that DECLINED to resolve. Distinct from a cold park —
    # the human sees it was TRIED, not skipped — tagged by orchestrator.resolve_park via
    # review_coverage, category-agnostic (fires for a declined hard-category park too), so the
    # per-ticket line names the RECORDED category rather than assuming requirement-meaning.
    f5_declined = [o for o in outcomes if "f5-attempted-declined" in (o.review_coverage or "")]
    if f5_declined:
        lines.append(f"> 🧭 **{len(f5_declined)} PARK(ed) ticket(s) had an F5 consensus attempt "
                     "that declined to resolve (stayed PARK):**")
        for o in f5_declined:
            cat = f" [{o.category}]" if o.category else ""
            lines.append(f">   - {o.ticket_id}{cat}: {o.why}")
        lines.append("")
    # Recoverable WIP backups (G1'): a `revert_to` set working-tree changes aside but anchored them
    # (tracked + untracked) into a durable `refs/ans-backup/*` commit before the reset. Surface them
    # so "recoverable in principle" becomes "findable in practice" — the peer incident had to dig one
    # out by hand. The restore hint is NON-destructive (inspect in a scratch worktree first).
    if backup_refs:
        lines.append(f"> ♻️ **{len(backup_refs)} recoverable WIP backup(s)** — a revert set "
                     "working-tree changes aside but anchored them (tracked + untracked):")
        for ref, sha in backup_refs:
            lines.append(f">   - `{ref}` ({sha})")
        newest = backup_refs[-1][0]
        lines.append(f">   Inspect the latest without touching your tree: "
                     f"`git worktree add /tmp/ans-recover {newest}` "
                     f"(or restore one file: `git checkout {newest} -- <path>`).")
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

    # Consent provenance (t04): which deny-list action classes a human pre-authorized for this run,
    # and which actual commands rode that consent — so "the run finished" can never hide "an
    # irreversible-class action ran under a human-granted exception". Absent when there is nothing
    # to show (no manifest AND no events) — never an empty header.
    allowed_slugs = sorted(slug for slug, v in (consent_manifest or {}).items()
                          if isinstance(v, dict) and v.get("allowed") is True)
    if allowed_slugs:
        lines.append("> 🔓 **Pre-authorized this run (deny-list floor lowered for these classes, "
                     f"all targets):** {', '.join(allowed_slugs)}")
        lines.append("")
    if consent_events:
        lines.append(f"> 🔓 **{len(consent_events)} action(s) ran under consent:**")
        for ev in consent_events:
            ts = ev.get("ts_utc", "")
            slug = ev.get("slug", "")
            # One event = one report line: a command carrying a newline/CR could otherwise forge
            # extra report rows, so collapse it to a single line before rendering.
            cmd = str(ev.get("command", "")).replace("\r", " ").replace("\n", " ")
            lines.append(f">   - {ts} {slug}: {cmd}")
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
