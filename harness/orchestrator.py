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

from .decide import Action, Decision, classify
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
                 protect_paths: list | None = None, classify_overrides: dict | None = None):
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
        baseline_green = self.gate.baseline(self.repo_dir)
        return ProceedToken(ticket_id=ticket.id, snapshot=snapshot,
                            baseline_green=baseline_green, attempt_n=attempt_n)

    def finalize_after_edit(self, ticket, token: ProceedToken, attempted: str, *,
                            cannot_implement: bool = False,
                            review_coverage: str | None = None,
                            council_config: dict | None = None,
                            council_verdict: str | None = None,
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
                       specialist_concerns: list | None = None,
                       credits_degrade: bool = False) -> TicketOutcome:
        """`cannot_implement=True` means the agent could not implement the ticket: revert and record
        BLOCKED_ENV honestly. When a council is configured, a PASSED gate on a high-risk DIFF whose
        council raised concerns / errored / wasn't run is recorded DONE_LOW_CONFIDENCE (needs daylight
        review) rather than a silent DONE — the council never blocks the run, only the trust upgrade."""
        os.makedirs(self.artifacts_dir, exist_ok=True)

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
            self.git.commit_all(f"done:{ticket.id}")
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
        os.makedirs(self.artifacts_dir, exist_ok=True)

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
