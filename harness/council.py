"""Council review — risk routing + disposition (the deterministic half of the multi-model funnel).

The actual multi-model calls are made by the AGENT via the tokonomix MCP gateway (the harness is a
stdlib CLI and cannot call LLMs). This module is the provider-neutral, deterministic scaffolding the
harness owns:

  * BUDGET GATE: decide_budget() is the single deterministic function the driver calls before each
    council to decide PROCEED / DEGRADE / STOP. It checks the running balance (agent-supplied), the
    per-night €-cap, and the call-count cap. Which of DEGRADE/STOP is chosen follows the configured
    `on_credits_exhausted` policy ("stop" = A, "degrade" = B). A council that reports HTTP 402 is
    mapped here too — the agent passes http_402=True and the same decision fires.

  * ROUTE the council tier from the ACTUAL DIFF (not the ticket text). A live council de-anchoring
    review of this very design flagged ticket-text routing as the single biggest weakness: "rename
    field" can touch auth, a "typo fix" can edit a permission check. So the binding route is computed
    from the changed files + diff content at complete-time and OVERRIDES any pre-work keyword hint.
  * PLAN the call (which proposers/judges/mode + an estimated € cost from static config prices) so the
    mandatory pre-council summary can be shown.
  * DISPOSE: the council is ADVISORY and NEVER blocks the run — but "advisory" must not become review
    theater. The same review's key fix: *don't block the run, block the automatic trust upgrade*. So a
    HEAVY-risk change whose council raised concerns, errored, or simply wasn't run is recorded
    DONE_LOW_CONFIDENCE with a "needs daylight review" action instead of a silent DONE. Deterministic
    gates remain the only HARD gate on execution.
"""
from __future__ import annotations

import dataclasses
import enum
import re

from .state import OutcomeState

# --- Credits budget gate -------------------------------------------------------------------


class BudgetDecision(str, enum.Enum):
    PROCEED = "proceed"   # within all caps and balance ≥ threshold — convene the council
    DEGRADE = "degrade"   # credits exhausted; skip council, mark low-confidence (policy B)
    STOP = "stop"         # credits exhausted; stop the run cleanly after this ticket (policy A)


class CouncilTier(str, enum.Enum):
    NONE = "none"     # docs/comments/whitespace only — skip the council
    LIGHT = "light"   # routine code change — light de-anchoring pass (advisory)
    HEAVY = "heavy"   # schema/security/api/money/cross-service — heavy plan+diff review


# What the agent reports back after running (or failing to run) the council via MCP.
class CouncilVerdict(str, enum.Enum):
    PASS = "pass"          # council ran and raised no material concern
    CONCERNS = "concerns"  # council ran and flagged something worth a human look
    ERROR = "error"        # council could not run (timeout / gateway / no key) — a blind spot
    SKIPPED = "skipped"    # tier was NONE, or council disabled — not required


# Files whose changes never need a council (pure prose / config-free docs).
_DOC_ONLY = re.compile(r"\.(md|markdown|rst|txt|adoc)$", re.IGNORECASE)

# HEAVY-risk signals, matched against BOTH changed file paths and the diff body. Deliberately broad
# on the side of caution: a false HEAVY costs an advisory review; a false LIGHT misses the net.
_HEAVY_PATH = re.compile(
    r"(migrat|schema|/sql/|\.sql$|auth|security|permission|rbac|tenant|session|middleware|"
    r"billing|payment|invoice|checkout|pricing|/api/|/routes?/|controller|openapi|\.proto$|"
    r"webhook|oauth|crypto|secret|password|token)", re.IGNORECASE)
_HEAVY_CONTENT = re.compile(
    r"(create table|alter table|drop (table|column)|add column|grant |revoke |"
    r"\bjwt\b|authoriz|authenticat|tenant_id|set_cookie|password|secret|api[_-]?key|"
    r"\beval\(|\bexec\(|subprocess|os\.system|child_process|innerhtml|"
    r"price|charge|refund|\btax\b|\bvat\b|stripe|paypal)", re.IGNORECASE)


@dataclasses.dataclass
class CouncilPlan:
    tier: CouncilTier
    proposers: list
    judges: list
    mode: str
    max_tokens: int
    est_cost_eur: float

    def summary_line(self, task_abbrev: str) -> str:
        """The mandatory pre-council summary: task · models · judges · type · est cost."""
        if self.tier == CouncilTier.NONE:
            return f"[council] SKIP ({task_abbrev}) — diff is docs/trivial, no review needed"
        return (f"[council] {self.tier.value.upper()} ({task_abbrev}) · "
                f"proposers={','.join(self.proposers)} · judges={','.join(self.judges)} · "
                f"mode={self.mode} · est≈€{self.est_cost_eur:.2f}")


def route_from_diff(changed_files: list, diff_text: str) -> CouncilTier:
    """Binding, deterministic council tier from the ACTUAL change. Overrides any ticket-text hint."""
    files = [f for f in (changed_files or []) if f.strip()]
    if not files:
        return CouncilTier.NONE
    if all(_DOC_ONLY.search(f) for f in files):
        return CouncilTier.NONE
    blob = "\n".join(files) + "\n" + (diff_text or "")
    if _HEAVY_PATH.search("\n".join(files)) or _HEAVY_CONTENT.search(diff_text or ""):
        return CouncilTier.HEAVY
    if blob.strip():
        return CouncilTier.LIGHT
    return CouncilTier.NONE


def pre_work_hint(ticket_text: str) -> CouncilTier:
    """A NON-binding hint from the ticket text, surfaced at `next` so the agent can plan to convene
    the right council before implementing. The binding decision is route_from_diff() at complete."""
    if _HEAVY_PATH.search(ticket_text) or _HEAVY_CONTENT.search(ticket_text):
        return CouncilTier.HEAVY
    return CouncilTier.LIGHT


def _tier_cfg(config: dict, tier: CouncilTier) -> dict:
    return (config.get("council", {}) or {}).get(tier.value, {}) or {}


def estimate_cost_eur(config: dict, tier: CouncilTier) -> float:
    """Rough € estimate from static per-model prices in config. Proposers each read the prompt and
    write up to max_tokens; judges read the proposers' outputs and write a synthesis. Approximate by
    design — the live post-call summary reports the real charged cost."""
    cc = config.get("council", {}) or {}
    prices = cc.get("prices_cents_per_mtok", {}) or {}
    prompt_tok = cc.get("est_prompt_tokens", 3000)
    tc = _tier_cfg(config, tier)
    proposers = tc.get("proposers", [])
    judges = tc.get("judges", [])
    out_tok = tc.get("max_tokens", 1000)
    cents = 0.0
    for m in proposers:
        pin, pout = prices.get(m, [150, 600])[:2]
        cents += (prompt_tok / 1_000_000) * pin + (out_tok / 1_000_000) * pout
    judge_in = prompt_tok + out_tok * max(len(proposers), 1)
    for m in judges:
        pin, pout = prices.get(m, [300, 1500])[:2]
        cents += (judge_in / 1_000_000) * pin + (out_tok / 1_000_000) * pout
    # The figures above are raw provider cost; the gateway adds markup and real token use runs higher.
    # An empirical ~2.5× factor (measured: a 5/3 heavy call estimated ~26c billed ~61c) keeps the
    # pre-call "est≈€" honest rather than rosy. Tune via config council.markup_factor.
    markup = cc.get("markup_factor", 2.5)
    return round((cents / 100.0) * markup, 4)


def plan(config: dict, tier: CouncilTier) -> CouncilPlan:
    tc = _tier_cfg(config, tier)
    return CouncilPlan(
        tier=tier, proposers=tc.get("proposers", []), judges=tc.get("judges", []),
        mode=tc.get("mode", "consensus"), max_tokens=tc.get("max_tokens", 1000),
        est_cost_eur=estimate_cost_eur(config, tier))


def enabled(config: dict) -> bool:
    cc = config.get("council", {}) or {}
    if not cc.get("enabled"):
        return False
    # the council needs the tokonomix gateway; if the integration is off, the council is inert
    return bool((config.get("integrations", {}).get("tokonomix", {}) or {}).get("enabled", True))


def budget_exhausted(config: dict, progress: dict) -> tuple[bool, str]:
    """Per-night cost brake so an unattended run can't quietly burn money on councils. Checks the
    reported cumulative € spend against budget.per_night_euro_cap AND a deterministic call-count cap
    (budget.max_council_calls_per_night) that does NOT depend on the agent reporting cost honestly.
    Returns (exhausted, human-readable reason)."""
    budget = config.get("budget", {}) or {}
    spent = float(progress.get("council_cost_eur", 0.0) or 0.0)
    calls = int(progress.get("council_calls", 0) or 0)
    euro_cap = budget.get("per_night_euro_cap")
    if euro_cap is not None and spent >= float(euro_cap):
        return True, f"per-night €cap reached (≈€{spent:.2f} / €{float(euro_cap):.2f})"
    call_cap = budget.get("max_council_calls_per_night")
    if call_cap is not None and calls >= int(call_cap):
        return True, f"council call cap reached ({calls} / {int(call_cap)})"
    return False, ""


def decide_budget(config: dict, progress: dict, *,
                  balance_eur: float | None = None,
                  est_cost_eur: float = 0.0,
                  http_402: bool = False) -> tuple[BudgetDecision, str]:
    """Single deterministic gate before each council call.

    Returns (BudgetDecision, human-readable reason).

    Trigger conditions for exhaustion (any ONE is sufficient):
      - balance_eur < budget.balance_threshold_euro (if balance is known)
      - running-total + est_cost_eur would exceed budget.per_night_euro_cap (if cap is set)
      - council call count already at budget.max_council_calls_per_night
      - http_402 == True (the gateway refused the last call with insufficient_balance)

    Which of DEGRADE/STOP is returned follows `budget.on_credits_exhausted`:
      "stop"    (policy A, default) → STOP
      "degrade" (policy B)          → DEGRADE
    """
    budget = config.get("budget", {}) or {}
    policy = (budget.get("on_credits_exhausted") or "stop").lower().strip()

    def _action(reason: str) -> tuple[BudgetDecision, str]:
        if policy == "degrade":
            return BudgetDecision.DEGRADE, reason
        return BudgetDecision.STOP, reason

    if http_402:
        return _action("tokonomix gateway returned HTTP 402 (insufficient_balance)")

    if balance_eur is not None:
        threshold = float(budget.get("balance_threshold_euro", 1.0) or 1.0)
        if balance_eur < threshold:
            return _action(
                f"balance (€{balance_eur:.2f}) below threshold (€{threshold:.2f})")

    spent = float(progress.get("council_cost_eur", 0.0) or 0.0)
    calls = int(progress.get("council_calls", 0) or 0)

    euro_cap = budget.get("per_night_euro_cap")
    if euro_cap is not None:
        if spent + est_cost_eur > float(euro_cap):
            return _action(
                f"per-night €cap would be exceeded "
                f"(≈€{spent:.2f} spent + €{est_cost_eur:.2f} est > €{float(euro_cap):.2f})")

    call_cap = budget.get("max_council_calls_per_night")
    if call_cap is not None and calls >= int(call_cap):
        return _action(f"council call cap reached ({calls} / {int(call_cap)})")

    return BudgetDecision.PROCEED, ""


def remaining_headroom(config: dict, progress: dict) -> dict:
    """Query how much budget remains for the rest of the run.

    Returns a dict with:
      euro_remaining: float | None  — remaining € before the per-night cap fires (None = no cap)
      calls_remaining: int | None   — remaining council calls before the count cap fires (None = no cap)
    """
    budget = config.get("budget", {}) or {}
    spent = float(progress.get("council_cost_eur", 0.0) or 0.0)
    calls = int(progress.get("council_calls", 0) or 0)
    euro_cap = budget.get("per_night_euro_cap")
    call_cap = budget.get("max_council_calls_per_night")
    return {
        "euro_remaining": (round(float(euro_cap) - spent, 4) if euro_cap is not None else None),
        "calls_remaining": (int(call_cap) - calls if call_cap is not None else None),
    }


@dataclasses.dataclass
class Disposition:
    state: OutcomeState
    review_coverage: str
    human_action_required: str
    needs_daylight_review: bool


# Concern-language a too-rosy self-reported PASS would contradict (cheap integrity cross-check).
_CONCERN_LANG = re.compile(
    r"(concern|\brisk\b|vulnerab|insecure|unsafe|blind.?spot|should (be )?review|needs? review|"
    r"disagree|caveat|\bhowever\b|not sure|uncertain|exploit|injection|race condition)", re.IGNORECASE)


def reconcile(verdict: CouncilVerdict, coverage_text: str) -> CouncilVerdict:
    """Cheap integrity check on the agent's self-reported verdict (it grades its own work, so a rosy
    PASS is the weak spot). If it claims PASS but its OWN council summary contains concern-language,
    distrust the self-report and treat it as CONCERNS. Mitigates — does not close — the independence
    gap; the full fix (harness parses a machine-readable council artifact) is the next hardening."""
    if verdict == CouncilVerdict.PASS and coverage_text and _CONCERN_LANG.search(coverage_text):
        return CouncilVerdict.CONCERNS
    return verdict


def dispose(tier: CouncilTier, verdict: CouncilVerdict, coverage: str, *,
            ticket_title: str) -> Disposition:
    """Given a PASSED deterministic gate, decide the FINAL disposition. NEVER blocks the run — it only
    decides whether the work is auto-trusted (DONE) or flagged for review (DONE_LOW_CONFIDENCE).

    FAIL-SAFE: a review FAILURE must never UPGRADE trust. PASS is the ONLY verdict that grants
    auto-trust; concerns / error / unknown all withhold it. Tiering decides the SEVERITY of withholding
    (HEAVY → daylight review), not whether a failure is acceptable. The one deliberate exception: a
    SKIPPED optional LIGHT council on a low-risk change stays trusted (light review is advisory)."""
    cov = coverage or f"deterministic-gates · council:{verdict.value}"

    # docs/trivial change — never needs a council.
    if tier == CouncilTier.NONE:
        return Disposition(OutcomeState.DONE, cov, "", False)
    # PASS is the only path to the auto-trust upgrade.
    if verdict == CouncilVerdict.PASS:
        return Disposition(OutcomeState.DONE, cov, "", False)
    # deliberate skip of the OPTIONAL light council on a low-risk diff stays trusted.
    if tier == CouncilTier.LIGHT and verdict == CouncilVerdict.SKIPPED:
        return Disposition(OutcomeState.DONE, cov, "", False)
    # HEAVY: anything other than a clean PASS needs a human in daylight.
    if tier == CouncilTier.HEAVY:
        why = {
            CouncilVerdict.CONCERNS: "council raised concerns on a high-risk change",
            CouncilVerdict.ERROR: "council could not run on a high-risk change (blind spot)",
            CouncilVerdict.SKIPPED: "high-risk change merged WITHOUT a council review",
        }.get(verdict, "high-risk change with an unrecognized council verdict")
        return Disposition(
            OutcomeState.DONE_LOW_CONFIDENCE, cov + " · NEEDS-DAYLIGHT-REVIEW",
            f"daylight review ({why}): {ticket_title}", True)
    # LIGHT with concerns / error / unknown: flagged low-confidence (fail-safe), not daylight.
    return Disposition(
        OutcomeState.DONE_LOW_CONFIDENCE, cov + " · council-flagged",
        f"review council {verdict.value}: {ticket_title}", False)
