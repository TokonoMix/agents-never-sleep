"""The unattended ticket loop.

Invariants (the whole point of the system):
  * Never asks the human anything in unattended mode (no input(), no prompts, ASK->PARK).
  * Never stops the run on a single ticket problem — it records an outcome and moves on.
  * Only HALTs the whole run on a genuine no-safety-net / irreversible condition.
  * Every ticket ends in exactly one durable OutcomeState (resume-safe).

Two drivers share ONE set of per-ticket helpers so the proven spine logic is never forked:
  * `Orchestrator.run(tickets)` — the in-process loop (DemoWorker / acceptance demo).
  * `harness.driver.StepDriver` — the agent-as-worker bridge: the AGENT is the worker, so the
    loop is driven one ticket at a time across separate processes/turns. It calls the very same
    `classify_ticket` / `begin_proceed` / `finalize_after_edit` helpers below.

Per-ticket spine: snapshot -> decide -> (implement) -> gate -> classify -> revert-or-commit ->
write outcome -> next.
"""
from __future__ import annotations

import dataclasses
import os

from . import gate_cache
from .decide import Action, Decision, classify
from .fsutil import ensure_private_dir
from .gates import GateResult, GateRunner
from .state import OutcomeState, OutcomeStore, TicketOutcome
from .vcs import Git, GitError
from .worker import Worker, WorkerCannotImplement


# Outcome states that count against the low-yield circuit breaker (parked/blocked/failed work).
# DONE and DONE_LOW_CONFIDENCE are productive and never "bad".
BAD_STATES = frozenset({
    OutcomeState.PARKED_DECISION,
    OutcomeState.PARKED_FOUNDATIONAL,
    OutcomeState.BLOCKED_ENV,
    OutcomeState.FAILED_RETRYABLE,
    OutcomeState.FAILED_BUG_IN_AGENT,
})


@dataclasses.dataclass
class ProceedToken:
    """A ticket that PASSED the decision gate and is mid-flight (snapshot taken, attempt counted,
    edits not yet made). Durable so a crash between 'begin' and 'finalize' is resumable: revert to
    `snapshot` to discard any partial edits, then re-schedule the ticket (the cap still protects)."""
    ticket_id: str
    snapshot: str
    baseline_green: bool
    attempt_n: int
    force_daylight_review: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(d: dict) -> "ProceedToken":
        known = {f.name for f in dataclasses.fields(ProceedToken)}
        return ProceedToken(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class RunResult:
    outcomes: list
    halted: bool = False
    halt_reason: str = ""
    stopped_low_yield: bool = False


@dataclasses.dataclass
class LowYieldBreaker:
    min_tickets: int = 8        # don't trip on a tiny backlog (the 3-ticket demo is safe)
    bad_ratio: float = 0.75     # parked+blocked+failed / processed

    def tripped(self, processed: int, bad: int) -> bool:
        return processed >= self.min_tickets and (bad / max(processed, 1)) >= self.bad_ratio


class Orchestrator:
    def __init__(self, *, repo_dir: str, store: OutcomeStore, gate: GateRunner,
                 worker: Worker, artifacts_dir: str, unattended: bool = True,
                 breaker: LowYieldBreaker | None = None, ledger=None,
                 fix_cap: int = 3, loop_threshold: int = 2, heartbeat=None,
                 protect_paths: list | None = None, classify_overrides: dict | None = None,
                 consensus_assisted_categories: list | None = None,
                 gate_baseline_reuse: bool = False):
        self.repo_dir = repo_dir
        self.store = store
        self.gate = gate
        self.worker = worker
        self.artifacts_dir = artifacts_dir
        self.unattended = unattended
        self.git = Git(repo_dir, protect=protect_paths)
        self.breaker = breaker or LowYieldBreaker()
        self.ledger = ledger
        self.fix_cap = fix_cap
        self.loop_threshold = loop_threshold
        self.heartbeat = heartbeat
        # Operator-supplied per-ticket classification overrides (INT-1825 bug 1): {ticket_id: action}.
        # Config-sourced and trusted; never an agent-runtime loosening of its own PARK gate.
        self.classify_overrides = classify_overrides or {}
        # Project-default hard-PARK opt-in set (Plan 2 §2/§3) — held on the instance, consumed by
        # the driver's F5 offer path (Task 7). Existing callers pass nothing -> [].
        self.consensus_assisted_categories = list(consensus_assisted_categories or [])
        # Q&A item 14: reuse a just-proven-green complete as the next ticket's baseline instead
        # of re-running the full gate. Default OFF (unchanged behaviour). The receipt lives under
        # the store's own state dir — store already owns durable per-run bookkeeping, so this
        # needs no new constructor wiring from driver.py beyond the flag itself.
        self.gate_baseline_reuse = gate_baseline_reuse
        # Derived, not injected: the store's state dir is already the durable per-run home. A
        # store without one (a test double) simply gets no cache path — reuse silently disables
        # (fail-safe: the gate then always runs for real), never a constructor crash.
        _state_dir = getattr(store, "state_dir", None)
        self.gate_cache_path = (os.path.join(_state_dir, gate_cache.CACHE_FILENAME)
                                if _state_dir else None)

    # ---- shared per-ticket helpers (used by BOTH run() and StepDriver) --------------------

    def is_terminal(self, ticket_id: str) -> TicketOutcome | None:
        """Return the prior outcome if this ticket is already in a skip-on-resume terminal state."""
        prior = self.store.read(ticket_id)
        if prior and prior.state in {
            OutcomeState.DONE, OutcomeState.DONE_LOW_CONFIDENCE,
            OutcomeState.PARKED_DECISION, OutcomeState.PARKED_FOUNDATIONAL,
        }:
            return prior
        return None

    def classify_ticket(self, ticket, has_safety_net: bool) -> Decision:
        """Decide PROCEED/PARK/HALT. ASK is collapsed to PARK (never ask while unattended)."""
        decision = classify(
            f"{ticket.title}\n{ticket.body}",
            unattended=self.unattended, has_safety_net=has_safety_net,
            override=self.classify_overrides.get(ticket.id),
        )
        if decision.action == Action.ASK:
            decision.action = Action.PARK
        return decision

    def park(self, ticket, decision: Decision) -> TicketOutcome:
        """Write + return a PARK outcome (the run keeps moving)."""
        state = (OutcomeState.PARKED_FOUNDATIONAL if decision.foundational
                 else OutcomeState.PARKED_DECISION)
        outcome = TicketOutcome(
            ticket_id=ticket.id, state=state, why=decision.why,
            category=field_or_blank(decision, "category"),
            attempted="classified before implementation",
            human_action_required=f"decide: {ticket.title}",
            contamination_scope=decision.contamination_scope,
        )
        self.store.write(outcome)
        return outcome

    def resolve_park(self, ticket, offer: dict, verdict):
        """F5 consumer. `offer` is the durable ledger record from the PARK_CONSENSUS_ELIGIBLE offer
        (attempt_id/category/foundational/has_safety_net/status) — the trusted category anchor, so we
        NEVER re-classify the (possibly-mutated) ticket text here. RESOLVE routes into begin_proceed
        ONLY when the ticket is still structurally F5-eligible per the RECORDED category + a
        CURRENT safety-net check; else park with a full audit trail. Idempotency + one-shot are the
        caller's job (StepDriver.resolve_park checks is_terminal + consumes the offer)."""
        from . import f5
        has_net = self.git.ensure_safety_net()
        # Rebuild a minimal Decision from the RECORDED offer, not a fresh classify():
        recorded = Decision(action=Action.PARK,
                            why="F5 offer replay (category taken from the durable offer record)",
                            category=offer.get("category", ""),
                            foundational=bool(offer.get("foundational", False)))
        recorded_categories = offer.get("consensus_assisted_categories", [])
        structurally_eligible = f5.eligible(
            recorded, has_safety_net=has_net, already_attempted=False,
            consensus_assisted_categories=recorded_categories)
        if offer.get("category") == "requirement_meaning":
            result, reason = f5.interpret_verdict(verdict)
        else:
            result, reason = f5.interpret_soundness_verdict(verdict)
        if result == f5.F5Result.RESOLVE and structurally_eligible:
            proceed = self.begin_proceed(ticket)
            # Spec §5: a resolution of ANY category other than requirement_meaning applied a change
            # that used to be a hard stop — force the after-the-fact human look. requirement_meaning
            # keeps its plain-DONE behavior. begin_proceed may return a capped PARK outcome instead
            # of a token; only stamp a real token.
            if (isinstance(proceed, ProceedToken)
                    and recorded.category != "requirement_meaning"):
                proceed.force_daylight_review = (
                    f"F5 resolved a hard-PARK category: {recorded.category}")
            return proceed
        if result == f5.F5Result.RESOLVE and not structurally_eligible:
            reason = ("verdict claimed RESOLVE but the RECORDED offer is not F5-structurally-eligible "
                      "(category/foundational/safety-net) — ignored")
        outcome = self.park(ticket, recorded)
        outcome.why = f"{outcome.why} — F5 consensus tried and declined: {reason}"
        outcome.evidence = verdict.evidence or outcome.evidence
        outcome.attempted = (f"F5 consensus attempted: resolved={verdict.resolved}, "
                            f"chosen_reading={verdict.chosen_reading!r}, "
                            f"dissent_count={verdict.dissent_count}, "
                            f"synthesis={verdict.synthesis_text!r}")
        outcome.review_coverage = "f5-attempted-declined"
        self.store.write(outcome)
        return outcome

    def begin_proceed(self, ticket) -> ProceedToken | TicketOutcome:
        """Account the attempt (cross-resume cap), then snapshot + baseline.

        Returns a ProceedToken when the ticket may proceed, or a written capped PARK outcome when
        the ticket has exceeded its attempt cap (so the night is never burned on one cursed item)."""
        attempt_n = self.ledger.record_attempt(ticket.id) if self.ledger else 1
        if self.ledger and self.ledger.over_cap(ticket.id, self.fix_cap):
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.PARKED_DECISION,
                why=f"exceeded attempt cap ({self.fix_cap}) across resumes — parked to "
                    "avoid burning the night on one ticket",
                attempts=attempt_n, attempted="capped before re-attempt",
                human_action_required=f"manual attention: {ticket.title}",
            )
            self.store.write(outcome)
            return outcome
        try:
            snapshot = self.git.commit_all(f"pre:{ticket.id}")
        except GitError as exc:
            # Cannot take a reversibility snapshot -> do NOT risk an unrevertible edit. Record
            # BLOCKED_ENV (env problem, not the ticket's fault) and keep the run moving.
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.BLOCKED_ENV,
                why="could not snapshot before editing (git unavailable/timed out)",
                exact_blocker=str(exc), attempted="snapshot failed", attempts=attempt_n,
                human_action_required=f"check git/VCS health for: {ticket.title}",
            )
            self.store.write(outcome)
            return outcome

        baseline_green = None
        if self.gate_baseline_reuse and self.gate_cache_path:
            # On ANY doubt fall through to running the gate for real: a clean tree_id that
            # matches a cached PASS with the EXACT same (non-empty) gate command is the only
            # reuse case. An empty/missing command identifies no gate, so it never matches.
            current_tree = gate_cache.tree_id(self.repo_dir)
            command = getattr(self.gate, "command", None) or []
            if command and gate_cache.hit(self.gate_cache_path, current_tree_id=current_tree,
                                          command=command):
                baseline_green = True
                print("[agents-never-sleep] baseline reused from previous green complete "
                      f"(tree {current_tree[:8]})")
        if baseline_green is None:
            baseline_green = self.gate.baseline(self.repo_dir)
        return ProceedToken(ticket_id=ticket.id, snapshot=snapshot,
                            baseline_green=baseline_green, attempt_n=attempt_n)

    def finalize_after_edit(self, ticket, token: ProceedToken, attempted: str, *,
                            cannot_implement: bool = False,
                            review_coverage: str | None = None,
                            council_config: dict | None = None,
                            council_verdict: str | None = None,
                            council_verdict_structured: dict | None = None,
                            specialist_concerns: list | None = None,
                            credits_degrade: bool = False) -> TicketOutcome:
        """Gate the (already-applied) edits, classify, revert-or-commit, write + return the outcome.

        Wraps the implementation so a git failure (timeout / missing binary / hung lock) during the
        revert-or-commit becomes a clean BLOCKED_ENV outcome instead of crashing the run.

        credits_degrade: when True (policy B), councils were skipped due to exhausted credits; floor
        any DONE disposition to DONE_LOW_CONFIDENCE so unreviewed work is never auto-trusted."""
        try:
            return self._finalize_impl(ticket, token, attempted,
                                       cannot_implement=cannot_implement,
                                       review_coverage=review_coverage,
                                       council_config=council_config,
                                       council_verdict=council_verdict,
                                       council_verdict_structured=council_verdict_structured,
                                       specialist_concerns=specialist_concerns,
                                       credits_degrade=credits_degrade)
        except GitError as exc:
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.BLOCKED_ENV,
                why="git failed during gate/revert/commit (env problem, not the diff)",
                exact_blocker=str(exc), attempted=attempted, attempts=token.attempt_n,
                human_action_required=f"check git/VCS health; re-run: {ticket.title}",
            )
            self.store.write(outcome)
            return outcome

    def _finalize_impl(self, ticket, token: ProceedToken, attempted: str, *,
                       cannot_implement: bool = False,
                       review_coverage: str | None = None,
                       council_config: dict | None = None,
                       council_verdict: str | None = None,
                       council_verdict_structured: dict | None = None,
                       specialist_concerns: list | None = None,
                       credits_degrade: bool = False) -> TicketOutcome:
        """`cannot_implement=True` means the agent could not implement the ticket: revert and record
        BLOCKED_ENV honestly. When a council is configured, a PASSED gate on a high-risk DIFF whose
        council raised concerns / errored / wasn't run is recorded DONE_LOW_CONFIDENCE (needs daylight
        review) rather than a silent DONE — the council never blocks the run, only the trust upgrade."""
        ensure_private_dir(self.artifacts_dir)

        if cannot_implement:
            self.git.revert_to(token.snapshot)
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.BLOCKED_ENV,
                why="worker could not implement the ticket",
                exact_blocker=attempted or "worker.apply raised",
                attempted="worker.apply raised", attempts=token.attempt_n,
                human_action_required=f"implement manually or refine: {ticket.title}",
            )
            self.store.write(outcome)
            return outcome

        gaterun = self.gate.run_after_edit(token.baseline_green)

        if gaterun.result == GateResult.PASS:
            # Council disposition is decided from the ACTUAL diff BEFORE we commit (advisory only —
            # the gate already passed, so this never reverts; it only sets DONE vs needs-review).
            state = OutcomeState.DONE
            coverage = review_coverage or "deterministic-gates"
            human_action = ""
            why = "gates green"
            files = difftext = None
            from . import council as _council
            if council_config and _council.enabled(council_config):
                files, difftext = self.git.diff_files(token.snapshot)
                tier = _council.route_from_diff(files, difftext)
                verdict = _coerce_verdict(council_verdict, tier)
                # integrity cross-check: distrust a rosy PASS that contradicts its own summary
                verdict = _council.reconcile(verdict, review_coverage or "")
                # ticket 03: fold in the gateway's INDEPENDENT machine-readable verdict,
                # DOWNGRADE-ONLY, when opted in (config.council.structured_verdict). This is
                # the full fix reconcile() flagged — the judge's own verdict, not the agent's
                # self-report. A structured PASS never upgrades a self-reported concern.
                if _council.structured_verdict_enabled(council_config) and council_verdict_structured:
                    verdict = _council.verdict_from_structured(verdict, council_verdict_structured)
                disp = _council.dispose(tier, verdict, review_coverage, ticket_title=ticket.title)
                state, coverage = disp.state, disp.review_coverage
                human_action = disp.human_action_required
                if disp.needs_daylight_review:
                    why = "gates green; high-risk diff needs daylight review (council)"
            # Specialist reviewers — advisory, the SAME trust-gating as the council. Record which
            # lenses the ACTUAL diff needed, and fold a high-blast-radius concern (architect /
            # security / tenant-safety) into the daylight-review disposition: such a reported concern
            # must never auto-merge. It withholds the trust upgrade only; it never reverts/blocks.
            from . import specialists as _spec
            if council_config and _spec.enabled(council_config):
                if files is None:
                    files, difftext = self.git.diff_files(token.snapshot)
                selected = _spec.select_from_diff(files, difftext)
                coverage = f"{coverage} · {_spec.coverage_tag(selected)}"
                forced = _spec.daylight_concerns(specialist_concerns)
                if forced:
                    rs = ",".join(r.value for r in forced)
                    state = OutcomeState.DONE_LOW_CONFIDENCE
                    # COMPOSE with any council daylight reason — don't clobber it. When the council
                    # already flagged this diff, the human needs BOTH diagnostics in the report.
                    spec_why = f"specialist concern ({rs}) needs daylight review"
                    why = f"gates green; {spec_why}" if why == "gates green" else f"{why}; {spec_why}"
                    spec_action = f"daylight review (specialist {rs} concern): {ticket.title}"
                    human_action = f"{human_action} + {spec_action}" if human_action else spec_action
                    if "NEEDS-DAYLIGHT-REVIEW" not in coverage:
                        coverage += " · NEEDS-DAYLIGHT-REVIEW"
            # INT-1675 #4: when NO real gate is configured (no-op trivially-green gate),
            # nothing was actually verified — so a passing "gate" must never be reported
            # as a trusted DONE/"gates green". Floor it to DONE_LOW_CONFIDENCE with an
            # honest reason. Applied AFTER council/specialist so it can only lower trust,
            # never raise it, and composes with any daylight-review reason.
            if getattr(self.gate, "noop", False) and state == OutcomeState.DONE:
                state = OutcomeState.DONE_LOW_CONFIDENCE
                nog = "no gate configured — unverified"
                why = nog if why == "gates green" else f"{why}; {nog}"
                if "unverified" not in coverage:
                    coverage = f"{coverage} · no-gate"
            # Credits-degrade (policy B): councils were skipped due to exhausted credits, so no
            # multi-model review ran. Floor any remaining DONE to DONE_LOW_CONFIDENCE so unreviewed
            # work is never auto-trusted. Composes with existing daylight-review reasons (can only
            # lower trust, never raise it).
            if credits_degrade and state == OutcomeState.DONE:
                state = OutcomeState.DONE_LOW_CONFIDENCE
                deg = "unverified, needs daylight review (credits exhausted — council skipped)"
                why = deg if why == "gates green" else f"{why}; {deg}"
                if "unverified" not in coverage:
                    coverage = f"{coverage} · no-council (credits-degrade)"
            # Spec §5: an F5-resolved HARD category is never auto-trusted as a clean DONE — floor to
            # DONE_LOW_CONFIDENCE so the human's after-the-fact daylight look always happens. Model
            # the SPECIALIST block (orchestrator.py:326-336), NOT the credits block: floor AND compose
            # the reason regardless of prior state, so the "F5 resolved a hard-PARK category" audit
            # fact survives even when the council/specialist ALREADY set DONE_LOW_CONFIDENCE first (a
            # DONE-only gate would silently drop the single most important fact on the highest-risk
            # path). It can only lower trust, never raise it.
            fdr = getattr(token, "force_daylight_review", None)
            if fdr and state in (OutcomeState.DONE, OutcomeState.DONE_LOW_CONFIDENCE):
                state = OutcomeState.DONE_LOW_CONFIDENCE
                why = fdr if why == "gates green" else f"{why}; {fdr}"
                if "NEEDS-DAYLIGHT-REVIEW" not in coverage:
                    coverage = f"{coverage} · NEEDS-DAYLIGHT-REVIEW"
                fdr_action = f"daylight review (F5 hard-category resolution): {ticket.title}"
                human_action = f"{human_action} + {fdr_action}" if human_action else fdr_action
            self.git.commit_all(f"done:{ticket.id}")
            if self.gate_baseline_reuse and self.gate_cache_path:
                # Receipt reflects "this exact gate command PASSED on this exact tree" — that is
                # independent of the DONE vs DONE_LOW_CONFIDENCE trust-downgrades above (council/
                # specialist/no-op/credits-degrade), so it is written unconditionally here. Only
                # this PASS branch ever writes; any non-PASS outcome leaves the existing cache
                # alone — safe, because reuse matches on the content-addressed tree id, so a
                # stale entry can never produce a false hit. An empty/missing gate command
                # identifies no gate, so nothing is recorded for it.
                command = getattr(self.gate, "command", None) or []
                fresh_tree = gate_cache.tree_id(self.repo_dir)
                if command and fresh_tree is not None:
                    gate_cache.write(self.gate_cache_path, tree_id=fresh_tree, command=command)
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=state, why=why,
                attempted=attempted, evidence="gate PASS", attempts=token.attempt_n,
                review_coverage=coverage, human_action_required=human_action,
            )
        elif gaterun.result == GateResult.FAIL_INTRODUCED_BY_DIFF:
            artifact = self._save_artifact(ticket.id, gaterun.output)
            self.git.revert_to(token.snapshot)
            looping = self._note_failure(ticket.id, gaterun.output)
            if looping:
                outcome = TicketOutcome(
                    ticket_id=ticket.id, state=OutcomeState.PARKED_DECISION,
                    why=f"same gate failure recurred ≥{self.loop_threshold}x — unproductive "
                        "looping; parked instead of retrying forever",
                    attempted=attempted, evidence="FAIL_INTRODUCED_BY_DIFF (looping)",
                    exact_blocker="repeating introduced failure", artifact_path=artifact,
                    attempts=token.attempt_n, human_action_required=f"manual fix: {ticket.title}",
                )
            else:
                outcome = TicketOutcome(
                    ticket_id=ticket.id, state=OutcomeState.FAILED_RETRYABLE,
                    why="gate failure introduced by the diff; reverted to last green",
                    attempted=attempted, evidence="FAIL_INTRODUCED_BY_DIFF",
                    exact_blocker="introduced test/compile failure", artifact_path=artifact,
                    attempts=token.attempt_n, human_action_required=f"re-approach: {ticket.title}",
                )
        elif gaterun.result == GateResult.FAIL_PREEXISTING:
            # the diff didn't cause the red; keep it but flag low confidence
            self.git.commit_all(f"done-lowconf:{ticket.id}")
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.DONE_LOW_CONFIDENCE,
                why="pre-existing gate failures present; change applied, confidence reduced",
                attempted=attempted, evidence="FAIL_PREEXISTING", attempts=token.attempt_n,
                review_coverage=(review_coverage or "deterministic-gates") + "(degraded)",
            )
        else:  # FAIL_ENV / timeout
            artifact = self._save_artifact(ticket.id, gaterun.output)
            self.git.revert_to(token.snapshot)
            outcome = TicketOutcome(
                ticket_id=ticket.id, state=OutcomeState.BLOCKED_ENV,
                why="gate could not run cleanly (env/timeout)",
                attempted=attempted, evidence="FAIL_ENV", attempts=token.attempt_n,
                exact_blocker="gate timed out or env failure", artifact_path=artifact,
                human_action_required=f"check test/build env for: {ticket.title}",
            )

        self.store.write(outcome)
        return outcome

    # ---- the in-process loop (acceptance demo / DemoWorker) ------------------------------

    def run(self, tickets: list) -> RunResult:
        ensure_private_dir(self.artifacts_dir)

        has_safety_net = self.git.ensure_safety_net()
        outcomes: list = []
        bad = 0

        for index, ticket in enumerate(tickets, start=1):
            if self.heartbeat:
                self.heartbeat.beat(ticket_id=ticket.id, phase="decide")
            # resume: skip tickets already in a terminal state
            prior = self.is_terminal(ticket.id)
            if prior:
                outcomes.append(prior)
                continue

            decision = self.classify_ticket(ticket, has_safety_net)

            # HALT: a genuine run-stopping condition (no reversibility guarantee).
            if decision.action == Action.HALT:
                return RunResult(outcomes, halted=True, halt_reason=decision.why)

            if decision.action == Action.PARK:
                outcomes.append(self.park(ticket, decision))
                bad += 1
                if self.breaker.tripped(index, bad):
                    return RunResult(outcomes, stopped_low_yield=True)
                continue

            # PROCEED: attempt accounting + cap check BEFORE any work
            began = self.begin_proceed(ticket)
            if isinstance(began, TicketOutcome):  # capped
                outcomes.append(began)
                bad += 1
                if self.breaker.tripped(index, bad):
                    return RunResult(outcomes, stopped_low_yield=True)
                continue

            # PROCEED: implement -> gate -> revert-or-commit
            try:
                attempted = self.worker.apply(ticket, self.repo_dir)
                cannot = False
            except WorkerCannotImplement as exc:
                attempted = str(exc)
                cannot = True

            outcome = self.finalize_after_edit(ticket, began, attempted, cannot_implement=cannot)
            outcomes.append(outcome)
            if outcome.state in BAD_STATES:
                bad += 1
            if self.breaker.tripped(index, bad):
                return RunResult(outcomes, stopped_low_yield=True)

        return RunResult(outcomes)

    def _note_failure(self, ticket_id: str, output: str) -> bool:
        """Record a failure signature; return True if this ticket is provably looping."""
        if not self.ledger:
            return False
        from .ledger import failure_signature
        sig = failure_signature(output)
        self.ledger.record_failure(ticket_id, sig)
        return self.ledger.loop_detected(ticket_id, sig, self.loop_threshold)

    def _save_artifact(self, ticket_id: str, content: str) -> str:
        from .redact import redact  # raw gate stdout is the biggest leak surface — scrub on write
        path = os.path.join(self.artifacts_dir, f"{ticket_id}.gate.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(redact(content))
        return path


def field_or_blank(obj, name: str) -> str:
    return getattr(obj, name, "") or ""


def _coerce_verdict(verdict_str, tier):
    """Map the agent-supplied council verdict string to a CouncilVerdict, FAIL-SAFE. A missing verdict
    means the agent ran no council (SKIPPED — a deliberate omission). A malformed/unrecognized value
    is suspicious, not benign, so it maps to ERROR (which withholds trust) rather than SKIPPED."""
    from .council import CouncilTier, CouncilVerdict
    if tier == CouncilTier.NONE or not verdict_str:
        return CouncilVerdict.SKIPPED
    try:
        return CouncilVerdict(verdict_str.strip().lower())
    except ValueError:
        return CouncilVerdict.ERROR
