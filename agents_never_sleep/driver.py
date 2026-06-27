"""Agent-as-worker bridge: drive the unattended loop ONE ticket per call.

The in-process `Orchestrator.run()` works when a deterministic Worker can be called synchronously
(the acceptance demo). In a real overnight run **the agent IS the worker** — it cannot be called
from inside a Python `for` loop; it reads a ticket, edits files, and re-enters the harness to record
the result. So the loop is inverted: the harness hands the agent exactly one PROCEED ticket, the
agent implements it, then calls back to gate+record, and asks for the next one. PARK/HALT/cap/
breaker decisions stay entirely in Python (the proven spine), so the only thing asked of the
unattended agent is the most reliable thing: "implement the ticket body I handed you, then call
complete." Everything else is structural.

Two structural guarantees live here:
  * SENTINEL OWNERSHIP. The Stop-hook blocks a premature end-of-turn while `.unattended/
    run-incomplete` exists. In the old in-process design `run.py` owned that file; now the loop is
    agent-driven, so the DRIVER owns it: it stays set while any non-terminal ticket remains and is
    cleared ONLY when the backlog is genuinely drained / halted / low-yield-tripped. "Never stop at
    2am" is therefore enforced by the file, not by the agent's diligence to keep calling.
  * RESUME-SAFE PROGRESS. Each `next`/`complete` is a fresh process, so breaker accounting is
    recomputed from the durable store every call (not held in memory), and a crash between `next`
    and `complete` is recovered on the next `next`: the partial edits are reverted to the pending
    snapshot and the ticket is re-scheduled under its attempt cap.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from .orchestrator import BAD_STATES, Orchestrator, ProceedToken
from .vcs import GitError
from .decide import Action
from .report import build_report
from .state import (ContaminationScope, OutcomeState, TERMINAL_SKIP_ON_RESUME,
                    OutcomeStore, TicketOutcome)


def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _run_start_credits_preflight(balance_eur: float | None,
                                 config: dict, unattended: bool,
                                 repo_dir: str | None = None) -> None:
    """Run-start check: when the supplied balance looks insufficient for planned council spend, either
    ASK the user (interactive) to confirm/select the A/B policy, or log loudly which policy applies
    (unattended). The harness owns the policy decision; the agent supplies the balance.

    Interactive:  shows Mes' exact framing, records the choice into config memory AND persists it to
                  disk via save_config (when repo_dir is given). Persistence matters because each
                  `next`/`complete` is a fresh process — without it an interactive "B" would be lost
                  and later processes would silently fall back to the default policy. Fires ONLY at
                  the start of fresh runs (sentinel-absent branch), so it cannot fire mid-run.
    Unattended:   applies the configured policy and prints a loud note. Never blocks on input().
    """
    from . import council as _council
    if not _council.enabled(config):
        return
    if balance_eur is None:
        return  # balance not supplied — skip the preflight (agent didn't call tokonomix_get_balance)
    budget = config.get("budget", {}) or {}
    threshold = float(budget.get("balance_threshold_euro", 1.0) or 1.0)
    euro_cap = budget.get("per_night_euro_cap")
    # Estimate total: council plan for HEAVY (worst-case) × half the call cap as a heuristic.
    call_cap = int(budget.get("max_council_calls_per_night", 50) or 50)
    heavy_plan = _council.plan(config, _council.CouncilTier.HEAVY)
    estimated_total = heavy_plan.est_cost_eur * max(call_cap // 2, 1)
    balance_sufficient = (balance_eur >= threshold and
                          (euro_cap is None or balance_eur >= float(euro_cap)) and
                          balance_eur >= estimated_total)
    if balance_sufficient:
        return  # comfortably covered — proceed silently

    policy = (budget.get("on_credits_exhausted") or "stop").lower().strip()
    framing = (
        f"Tokonomix credits may be insufficient for this run (balance ≈€{balance_eur:.2f}, "
        f"estimated need ≈€{estimated_total:.2f}). "
        "When they run out, should I: "
        "(A) stop on time, update status and stop; or "
        "(B) continue without consensus and let the local agent do the tasks?"
    )
    if not unattended:
        # Interactive: ask now, save the choice into the in-memory config so decide_budget sees it.
        try:
            import sys
            print(f"\n[agents-never-sleep] {framing}")
            ans = input("Choice [A/B, default A]: ").strip().upper()
            chosen = "degrade" if ans.startswith("B") else "stop"
            config.setdefault("budget", {})["on_credits_exhausted"] = chosen
            # Persist so subsequent fresh `next`/`complete` processes honor the choice — without
            # this the in-memory choice dies with this process and later steps revert to default.
            if repo_dir:
                try:
                    from .config import save_config
                    save_config(repo_dir, config)
                except OSError:
                    print("[agents-never-sleep] WARN: could not persist policy to config "
                          "(continuing with in-memory choice for this process only).")
            print(f"[agents-never-sleep] Credits-exhaustion policy set to: {chosen!r}")
        except EOFError:
            # stdin closed unexpectedly — fall through with the configured default
            pass
    else:
        # Unattended: apply configured policy, log loudly.
        print(f"[agents-never-sleep] {framing}")
        print(f"[agents-never-sleep] UNATTENDED — applying configured policy: {policy!r}. "
              "No input prompt possible. Set budget.on_credits_exhausted in your config "
              "to change the policy before launching an unattended run.")


class StepDriver:
    """One-ticket-at-a-time driver. See module docstring for the two structural guarantees."""

    def __init__(self, *, orch: Orchestrator, tickets: list, store: OutcomeStore,
                 state_dir: str, report_path: str, run_label: str = "unattended run",
                 non_destructive: bool = False, config: dict | None = None,
                 key_blind_spots: list | None = None):
        self.orch = orch
        self.tickets = tickets
        self.store = store
        self.state_dir = state_dir
        self.report_path = report_path
        self.run_label = run_label
        self.config = config or {}
        # Run-level secret-resolution failures (e.g. a configured Vault/env token_ref that didn't
        # resolve) — surfaced as morning-report blind spots so a degraded key source is never silent.
        self.key_blind_spots = list(key_blind_spots or [])
        # When the project config sets autonomy.non_destructive_only, the harness must NOT edit
        # files: every PROCEED ticket is triaged (parked) instead of implemented. This is a real
        # safety control, enforced here so a saved config can't be silently ignored.
        self.non_destructive = non_destructive
        self.pending_path = os.path.join(state_dir, "pending.json")
        self.skip_path = os.path.join(state_dir, "skip-this-run.json")
        self.progress_path = os.path.join(state_dir, "run-progress.json")
        # Run-branch isolation (INT-1825 bug 2): which branch the harness commits to, and the
        # operator branch to restore at the terminal. Persisted because each next/complete is a
        # fresh process and must rejoin the SAME run branch (never commit onto the operator branch).
        self.runbranch_path = os.path.join(state_dir, "run-branch.json")
        # The Stop-hook (hooks/stop_guard.sh) reads ${UE_RUN_INCOMPLETE:-$PWD/.unattended/
        # run-incomplete}. The driver MUST write the SAME file the hook checks, or never-stop
        # silently breaks when the agent's CWD != --repo (the cron/claude-run case). So honour the
        # same env var, falling back to the repo-relative path (correct when run from repo root).
        self.sentinel_path = (os.environ.get("UE_RUN_INCOMPLETE")
                              or os.path.join(orch.repo_dir, ".unattended", "run-incomplete"))
        # Fresh-session-per-N-tickets (opt-in context strategy). The launcher sets
        # UE_SESSION_TICKET_BUDGET=N and respawns a fresh agent each time the previous one stops.
        # The driver counts RECORDED completions for THIS session; on reaching N it writes the
        # session-budget-reached marker, which the Stop-hook honours to let the agent stop EARLY
        # (even though the run-incomplete sentinel still exists). DEFAULT OFF: when the env var is
        # UNSET, none of this code runs and behaviour is byte-identical to a single accumulating run.
        # The marker path is pinned via UE_SESSION_BUDGET_MARKER (like UE_RUN_INCOMPLETE) so the
        # hook and driver agree even when the agent's CWD != --repo (cron/claude-run case).
        self.session_count_path = os.path.join(state_dir, "session-ticket-count")
        self.session_marker_path = (os.environ.get("UE_SESSION_BUDGET_MARKER")
                                    or os.path.join(orch.repo_dir, ".unattended", "state",
                                                    "session-budget-reached"))

    # ---- pending checkpoint (the in-flight ticket) --------------------------------------

    def _load_pending(self) -> ProceedToken | None:
        if not os.path.exists(self.pending_path):
            return None
        try:
            with open(self.pending_path, "r", encoding="utf-8") as fh:
                return ProceedToken.from_json(json.load(fh))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def _save_pending(self, token: ProceedToken) -> None:
        _atomic_write_json(self.pending_path, token.to_json())

    def _clear_pending(self) -> None:
        if os.path.exists(self.pending_path):
            os.unlink(self.pending_path)

    # ---- "skip this run" set: retryable/blocked tickets attempted THIS run --------------
    # A FAILED_RETRYABLE / BLOCKED_ENV ticket is NOT terminal (a later resume should retry it),
    # but re-handing it immediately within the same drain would starve later independent tickets
    # and burn its whole attempt cap in one night. So we set it aside for the rest of THIS run and
    # let the next resume (a fresh run: sentinel absent at entry) pick it up again.

    def _load_skip(self) -> set:
        if not os.path.exists(self.skip_path):
            return set()
        try:
            with open(self.skip_path, "r", encoding="utf-8") as fh:
                return set(json.load(fh).get("ids", []))
        except (json.JSONDecodeError, OSError):
            return set()

    def _add_skip(self, ticket_id: str) -> None:
        ids = self._load_skip()
        ids.add(ticket_id)
        _atomic_write_json(self.skip_path, {"ids": sorted(ids)})

    def _reset_skip(self) -> None:
        if os.path.exists(self.skip_path):
            os.unlink(self.skip_path)

    # ---- sentinel ownership -------------------------------------------------------------

    def _set_sentinel(self) -> None:
        os.makedirs(os.path.dirname(self.sentinel_path), exist_ok=True)
        if not os.path.exists(self.sentinel_path):
            with open(self.sentinel_path, "w", encoding="utf-8") as fh:
                fh.write("run in progress\n")

    def _clear_sentinel(self) -> None:
        if os.path.exists(self.sentinel_path):
            os.unlink(self.sentinel_path)

    # ---- per-session ticket budget (opt-in fresh-session-per-N-tickets) -----------------
    # Active ONLY when UE_SESSION_TICKET_BUDGET is set by the launcher. Counts RECORDED ticket
    # completions for the CURRENT agent session; the launcher resets the counter + marker before
    # each fresh spawn (so "session start" = counter file absent). On reaching the budget the
    # marker is written, which the Stop-hook honours to allow an EARLY stop while the run-incomplete
    # sentinel still exists — letting the launcher resume the backlog in a fresh, un-degraded session.

    def _session_budget(self) -> int:
        """N from the launcher env, or 0 (feature off). Any non-positive/invalid value = off."""
        raw = os.environ.get("UE_SESSION_TICKET_BUDGET")
        if not raw:
            return 0
        try:
            n = int(raw)
        except ValueError:
            return 0
        return n if n > 0 else 0

    def _bump_session_count(self) -> bool:
        """Increment this session's RECORDED-completion count; write the marker on reaching budget.
        Returns True iff the budget was just reached (so `complete` can tell the agent to STOP).
        No-op returning False when the feature is off (budget unset) — default behaviour is
        byte-identical."""
        budget = self._session_budget()
        if budget <= 0:
            return False
        try:
            with open(self.session_count_path, "r", encoding="utf-8") as fh:
                count = int(fh.read().strip() or "0")
        except (OSError, ValueError):
            count = 0
        count += 1
        os.makedirs(os.path.dirname(self.session_count_path), exist_ok=True)
        with open(self.session_count_path, "w", encoding="utf-8") as fh:
            fh.write(str(count))
        if count >= budget:
            os.makedirs(os.path.dirname(self.session_marker_path), exist_ok=True)
            with open(self.session_marker_path, "w", encoding="utf-8") as fh:
                fh.write(f"session reached {count}/{budget} tickets\n")
            return True
        return False

    # ---- run-branch isolation (INT-1825 bug 2) ------------------------------------------
    # All harness snapshot/result commits must land on a dedicated ans/run-* branch, never on the
    # operator's branch. The run branch + the branch to restore are persisted so every fresh
    # next/complete process rejoins the same branch; the terminal restores the operator's branch
    # but LEAVES the run branch for review/merge.

    def _load_runbranch(self) -> dict | None:
        if not os.path.exists(self.runbranch_path):
            return None
        try:
            with open(self.runbranch_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_runbranch(self, run_branch: str, original_branch: str) -> None:
        _atomic_write_json(self.runbranch_path,
                           {"run_branch": run_branch, "original_branch": original_branch})

    def _clear_runbranch(self) -> None:
        if os.path.exists(self.runbranch_path):
            os.unlink(self.runbranch_path)

    def _enter_run_branch(self) -> None:
        """Ensure the harness is ON its dedicated run branch before any snapshot/commit/revert.

        Creates+persists the branch on the first call of a run; re-checks-it-out on every later
        process. Best-effort: a git failure degrades to the current branch (isolation lost, run
        continues) rather than crashing the night — surfaced as a morning-report blind spot."""
        git = self.orch.git
        if not git.is_repo():
            return
        try:
            state = self._load_runbranch()
            if state and state.get("run_branch"):
                if git.current_ref() != state["run_branch"]:
                    git.checkout(state["run_branch"])
                return
            original = git.current_ref()
            run_branch = f"ans/run-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
            git.create_run_branch(run_branch)
            self._save_runbranch(run_branch, original)
        except GitError as exc:
            self.key_blind_spots.append(
                f"run-branch isolation unavailable ({exc}); harness commits may land on the "
                "current branch — verify git health before the next run")

    def _exit_run_branch(self) -> None:
        """At a terminal signal, check the operator's branch back out (leaving the run branch intact
        for review/merge) and clear the run-branch state so the next run starts fresh."""
        state = self._load_runbranch()
        if state and state.get("original_branch"):
            git = self.orch.git
            try:
                if git.is_repo() and git.current_ref() != state["original_branch"]:
                    git.checkout(state["original_branch"])
            except GitError as exc:
                self.key_blind_spots.append(
                    f"could not restore the operator branch {state['original_branch']!r} ({exc}); "
                    f"the repo may be left on the run branch — checkout manually")
        self._clear_runbranch()

    # ---- run-scoped breaker accounting --------------------------------------------------
    # The low-yield breaker must measure THIS run, not all-time history: a fresh resume of a
    # backlog that already has many parked/failed tickets must not trip LOW_YIELD before doing any
    # new work. So progress is a small persisted counter reset at the start of each fresh run
    # (mirroring the in-process run()'s local index/bad counters, but durable across processes).

    def _load_progress(self) -> dict:
        # This schema is lossy by design: keys NOT listed here are dropped on every
        # read-modify-write (_bump_* helpers). Any run-level signal that must survive
        # a later bump MUST be normalized here — credits_stop_requested in particular,
        # else the reactive 402 STOP flag gets erased before `next` reads it.
        base = {"processed": 0, "bad": 0, "council_cost_eur": 0.0, "council_calls": 0,
                "credits_exhausted_degrade": False, "credits_stop_requested": None}
        if not os.path.exists(self.progress_path):
            return base
        try:
            with open(self.progress_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return {"processed": int(d.get("processed", 0)), "bad": int(d.get("bad", 0)),
                    "council_cost_eur": float(d.get("council_cost_eur", 0.0) or 0.0),
                    "council_calls": int(d.get("council_calls", 0) or 0),
                    "credits_exhausted_degrade": bool(d.get("credits_exhausted_degrade", False)),
                    "credits_stop_requested": d.get("credits_stop_requested")}
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return base

    def _reset_progress(self) -> None:
        _atomic_write_json(self.progress_path,
                           {"processed": 0, "bad": 0, "council_cost_eur": 0.0, "council_calls": 0,
                            "credits_exhausted_degrade": False, "credits_stop_requested": None})

    def _reset_run_counters(self) -> None:
        # INT-1675 P4: a fresh-run ENTRY must reset the low-yield breaker counters (processed/bad)
        # so a resume over a backlog with prior parks does not trip LOW_YIELD before doing any new
        # work — but it must NOT wipe the cumulative per-night spend accounting (council_cost_eur +
        # council_calls). The fresh-run branch keys off sentinel-absence, which a mid-run resume can
        # also hit (sentinel lost/abnormal death); zeroing spend there silently re-opens the €-cap
        # and the per-night council-call cap. So reset the breaker, PRESERVE the spend accounting.
        # The full zero (incl. spend) still happens at a clean _terminate(), so a genuinely fresh run
        # after a clean prior run already starts at 0 — this only changes the sentinel-absent-resume
        # case, in the safe (conservative, cap-preserving) direction.
        p = self._load_progress()
        _atomic_write_json(self.progress_path,
                           {"processed": 0, "bad": 0,
                            "council_cost_eur": p["council_cost_eur"], "council_calls": p["council_calls"],
                            "credits_exhausted_degrade": False, "credits_stop_requested": None})

    def reset_spend(self) -> dict:
        # INT-1675 P4 follow-on: operator escape to zero the per-night spend accounting
        # (council_cost_eur + council_calls) WITHOUT touching the low-yield breaker counters —
        # symmetric to `reset-attempts`. For when a resume / abnormal exit left the €-cap or the
        # per-night council-call cap accounting wrong and the operator wants a clean spend slate.
        p = self._load_progress()
        prior = {"council_cost_eur": p["council_cost_eur"], "council_calls": p["council_calls"]}
        p["council_cost_eur"] = 0.0
        p["council_calls"] = 0
        _atomic_write_json(self.progress_path, p)
        return prior

    def _set_degrade_flag(self) -> None:
        """Persist the B-policy (degrade) flag so subsequent `next` calls honor it across processes."""
        p = self._load_progress()
        p["credits_exhausted_degrade"] = True
        _atomic_write_json(self.progress_path, p)

    def _bump_progress(self, *, is_bad: bool) -> None:
        p = self._load_progress()
        p["processed"] += 1
        if is_bad:
            p["bad"] += 1
        _atomic_write_json(self.progress_path, p)
        # INT-1675: the cross-platform Stop dispatcher (enforce.py) caps never-stop at
        # _STOP_LOOP_CAP *blocks*, counted in a `stop-block-count` file beside the sentinel, and
        # only clears it when the whole backlog drains. Without a per-progress reset, 5 cumulative
        # stop-blocks spread across a long run would silently disable never-stop for the rest of it.
        # A completed ticket IS forward progress, so reset the counter here — making the cap
        # effectively "blocks WITHOUT progress", which is the real infinite-loop guard.
        try:
            cnt = os.path.join(os.path.dirname(self.sentinel_path) or ".", "stop-block-count")
            if os.path.exists(cnt):
                os.remove(cnt)
        except OSError:
            pass

    def _bump_council(self, cost_eur: float) -> None:
        """Accumulate reported council spend + call count for the per-night cost brake."""
        p = self._load_progress()
        p["council_calls"] += 1
        p["council_cost_eur"] = round(p["council_cost_eur"] + max(float(cost_eur or 0.0), 0.0), 4)
        _atomic_write_json(self.progress_path, p)

    def _bump_spend(self, cost_eur: float) -> None:
        """Accumulate review spend WITHOUT consuming a council call. Specialist-lens reviews are paid
        tokonomix calls too, so their € feeds the same per-night €-cap, but they must not burn the
        separate `max_council_calls_per_night` count (that caps full councils specifically)."""
        amount = max(float(cost_eur or 0.0), 0.0)
        if amount <= 0.0:
            return
        p = self._load_progress()
        p["council_cost_eur"] = round(p["council_cost_eur"] + amount, 4)
        _atomic_write_json(self.progress_path, p)

    def _breaker_tripped(self) -> bool:
        p = self._load_progress()
        return self.orch.breaker.tripped(p["processed"], p["bad"])

    def _ticket_by_id(self, ticket_id: str):
        for t in self.tickets:
            if t.id == ticket_id:
                return t
        return None

    # ---- terminal signals (always clear the sentinel + write the report) ----------------

    def _terminate(self, status: str, *, reason: str = "") -> dict:
        self._clear_sentinel()
        # Capture the run branch BEFORE _exit clears the state, so the report + payload can point the
        # operator at where the night's work actually lives (the work is on the run branch now, not on
        # their branch — without this pointer an overnight run hides its own output). INT-1825 bug 2.
        rb_state = self._load_runbranch()
        run_branch = (rb_state or {}).get("run_branch")
        # Restore the operator's branch (leaving the run branch for review/merge) before writing the
        # report onto it. Done first so the report file lands on the operator branch's working tree.
        self._exit_run_branch()
        # The run is over: clear the per-run scratch so the next run starts clean even if the
        # sentinel-clear races a concurrent resume. (A run that ends ABNORMALLY — process killed,
        # never reaching here — leaves the skip set in place; the continuation resumes with it,
        # which is correct, and the next CLEAN terminal clears it. Known bounded limitation.)
        self._reset_skip()
        self._reset_progress()
        outcomes = self.store.all()
        # Surface run-level blind spots: secret-resolution failures, degraded enforcement on the
        # current platform, then a degraded review.
        from . import capabilities, onboarding
        notes = list(self.key_blind_spots)
        notes.extend(capabilities.report_notes(capabilities.detect_platform()))
        od = onboarding.directive(self.config, interactive=not self.orch.unattended)
        if od and od.get("action") == "degraded":
            notes.append(od["blind_spot"])
        done = sum(1 for o in outcomes if o.state.value.startswith("DONE"))
        stop_notes = list(notes)
        if status == "STOPPED_CREDITS":
            stop_notes.insert(0, f"CREDITS EXHAUSTED — run stopped cleanly: {reason}")
        report = build_report(
            outcomes, run_label=self.run_label,
            halted=(status == "HALTED"), halt_reason=reason,
            stopped_low_yield=(status == "LOW_YIELD"), notes=stop_notes,
            work_branch=(run_branch if done else None),
        )
        with open(self.report_path, "w", encoding="utf-8") as fh:
            fh.write(report)
        return {"status": status, "reason": reason, "report_path": self.report_path,
                "processed": len(outcomes), "done": done, "run_branch": run_branch,
                "message": f"backlog {status}; report written to {self.report_path}"
                           + (f"; work on branch {run_branch}" if run_branch and done else "")}

    # ---- the two entry points the agent calls -------------------------------------------

    def _beat(self, phase: str) -> None:
        """Pulse the liveness heartbeat. In the agent-driven flow each next/complete is a separate
        short-lived process, so beating here is the only thing that keeps the watchdog's heartbeat
        fresh — without it the watchdog would see a permanently-stale beat and false-restart a
        healthy overnight run. (The in-process run() beats per ticket instead.)"""
        if self.orch.heartbeat is not None:
            try:
                self.orch.heartbeat.beat(phase=phase)
            except OSError:
                pass

    def next_ticket(self, *, balance_eur: float | None = None) -> dict:
        """Schedule and return the next PROCEED ticket for the agent to implement, or a terminal
        signal (DRAINED / HALTED / LOW_YIELD / STOPPED_CREDITS). Auto-writes every PARK/cap outcome
        in Python.

        balance_eur: the current Tokonomix balance as reported by tokonomix_get_balance (agent-
        supplied). When provided, a credits preflight runs at the start of each fresh run and before
        each council — the harness owns the policy (PROCEED/DEGRADE/STOP), never the agent.
        """
        self._beat("schedule")
        has_net = self.orch.git.ensure_safety_net()

        # A fresh run (no sentinel yet, nothing in flight) clears the per-run "set aside" list so a
        # new resume retries the tickets a prior run set aside. A continuing run keeps it.
        pending = self._load_pending()
        # Join the dedicated run branch BEFORE the crash-recovery revert below — otherwise the
        # recovery would reset the operator's branch tree (INT-1825 bug 2). Only when there is work
        # context (tickets to do, or a run already in flight); an empty backlog terminates untouched.
        if self.tickets or pending is not None or self._load_runbranch():
            self._enter_run_branch()
        if not os.path.exists(self.sentinel_path) and pending is None:
            self._reset_skip()
            self._reset_run_counters()
            # Run-start credits preflight: check whether the supplied balance is likely sufficient
            # to cover the planned council spend. Only when council is configured.
            _run_start_credits_preflight(balance_eur, self.config, self.orch.unattended,
                                         repo_dir=getattr(self.orch, "repo_dir", None))

        # Recover a crash while a ticket was in flight. CAREFUL: distinguish "crashed mid-edit"
        # (revert to discard partial edits) from "finalize already committed + recorded the outcome
        # but crashed before clearing pending" (reverting would DESTROY committed, recorded work).
        if pending is not None:
            prior = self.store.read(pending.ticket_id)
            if prior is not None and prior.state in TERMINAL_SKIP_ON_RESUME:
                # finalize completed (DONE / DONE_LOW_CONFIDENCE / PARKED) — its commit/revert is
                # already done; the pending record is merely stale. Do NOT revert.
                self._clear_pending()
            else:
                # no outcome yet, or a non-terminal one (FAILED_RETRYABLE/BLOCKED_ENV whose finalize
                # already reverted — re-reverting to the snapshot is idempotent). Discard any partial
                # edits and let the ticket be re-scheduled under its attempt cap.
                try:
                    self.orch.git.revert_to(pending.snapshot)
                    self._clear_pending()
                except GitError as exc:
                    # The backup-before-revert failed (broken git/env), so revert_to ABORTED to
                    # preserve the WIP. Keep pending for a healthy-git resume; surface, never crash.
                    self.key_blind_spots.append(
                        f"crash-recovery revert of {pending.ticket_id} was aborted to preserve WIP "
                        f"({exc}); the partial edit is intact on the run branch — resume when git "
                        "is healthy")

        if not self.tickets:
            return self._terminate("DRAINED")

        self._set_sentinel()
        # A run whose completed tickets already pushed it over the low-yield line stops here,
        # before handing out more work.
        if self._breaker_tripped():
            return self._terminate("LOW_YIELD")
        # Check for a deferred policy-A STOP (set by complete_ticket when the council gateway
        # returned HTTP 402 on the previous ticket). Fire it on the next `next` call so the prior
        # ticket completes normally before the run stops.
        progress = self._load_progress()
        credits_stop_reason = progress.get("credits_stop_requested")
        if credits_stop_reason:
            return self._terminate("STOPPED_CREDITS", reason=str(credits_stop_reason))
        # INT-1935: hard per-run ticket cap — stop at max_tickets_per_run so a surprise-large
        # backlog never consumes the whole night. Default 20 (config). None = no ceiling.
        _max_tickets = (self.config.get("budget") or {}).get("max_tickets_per_run")
        if _max_tickets is not None and int(progress.get("processed", 0)) >= int(_max_tickets):
            return self._terminate("DRAINED",
                                   reason=f"max_tickets_per_run={_max_tickets} reached")
        skip = self._load_skip()

        for ticket in self.tickets:
            if self.orch.is_terminal(ticket.id) is not None:
                continue
            if ticket.id in skip:        # attempted this run; leave for the next resume
                continue

            decision = self.orch.classify_ticket(ticket, has_net)

            if decision.action == Action.HALT:
                return self._terminate("HALTED", reason=decision.why)

            if decision.action == Action.PARK:
                self.orch.park(ticket, decision)
                self._bump_progress(is_bad=True)
                if self._breaker_tripped():
                    return self._terminate("LOW_YIELD")
                continue

            # Non-destructive mode: a PROCEED ticket is triaged, never implemented (no edits).
            if self.non_destructive:
                self.store.write(TicketOutcome(
                    ticket_id=ticket.id, state=OutcomeState.PARKED_DECISION,
                    why="non-destructive mode (config) — file-writing autonomy disabled; ticket "
                        "triaged, not implemented",
                    category="non_destructive", attempted="not attempted (non-destructive mode)",
                    human_action_required=("enable autonomy in .claude/agents-never-sleep.json "
                                           f"to implement: {ticket.title}"),
                    contamination_scope=ContaminationScope.NONE))
                self._bump_progress(is_bad=True)
                if self._breaker_tripped():
                    return self._terminate("LOW_YIELD")
                continue

            # PROCEED: attempt accounting + cap check happen in begin_proceed.
            began = self.orch.begin_proceed(ticket)
            if isinstance(began, TicketOutcome):  # capped/blocked -> already written
                self._bump_progress(is_bad=began.state in BAD_STATES)
                # A non-terminal block here (e.g. snapshot git-failure -> BLOCKED_ENV) is set aside
                # for this run so it isn't re-hit every `next`; retried on the next resume.
                if began.state not in TERMINAL_SKIP_ON_RESUME:
                    self._add_skip(ticket.id)
                if self._breaker_tripped():
                    return self._terminate("LOW_YIELD")
                continue

            # Hand exactly this ticket to the agent. Snapshot+baseline are captured (began); the
            # agent must not have edited anything yet — it edits now, then calls complete.
            self._save_pending(began)
            self._set_sentinel()
            payload = {
                "status": "PROCEED",
                "ticket": {"id": ticket.id, "title": ticket.title, "body": ticket.body,
                           "path": ticket.path},
                "attempt": began.attempt_n,
                "snapshot": began.snapshot,
                "instructions": ("Implement ONLY this ticket by editing files under the repo, then "
                                 "call `complete`. Do not edit any other ticket. Do not stop or ask."),
            }
            self._attach_council_hint(payload, ticket, balance_eur=balance_eur)
            # STOP decision from decide_budget: terminate cleanly before handing the ticket out.
            if "_credits_stop" in payload:
                reason = payload.pop("_credits_stop")
                # Revert the snapshot commit (nothing was done, just an accounting commit).
                try:
                    self.orch.git.revert_to(began.snapshot)
                except Exception:  # noqa: BLE001 — revert failure must not shadow the credits stop
                    pass
                self._clear_pending()
                return self._terminate("STOPPED_CREDITS", reason=reason)
            self._attach_specialist_hint(payload, ticket)
            self._attach_onboarding_hint(payload)
            return payload

        # No proceedable ticket remains -> the backlog is genuinely drained.
        return self._terminate("DRAINED")

    def _attach_council_hint(self, payload: dict, ticket, *, balance_eur=None) -> None:
        """Non-binding pre-work hint so the agent can convene the right council BEFORE implementing.
        The binding tier is recomputed from the actual diff at complete-time (which can only escalate
        the disposition, never silently downgrade it).

        Uses decide_budget() to gate the council before handing out the hint. A STOP decision is
        surfaced as the terminal STOPPED_CREDITS status; DEGRADE sets the degrade flag and disables
        the council for this ticket (but continues the run).
        """
        from . import council
        if not council.enabled(self.config):
            return
        # If the B-policy degrade flag is already set (from a prior ticket this run), councils
        # remain disabled for the rest of the run without re-checking the balance each ticket.
        progress = self._load_progress()
        if progress.get("credits_exhausted_degrade"):
            payload["council"] = {
                "disabled": "credits exhausted (policy B: degrade) — councils disabled for this run",
                "protocol": "Per-night credits exhausted (degrade mode). Do NOT convene councils. "
                            "The harness still flags high-risk diffs DONE_LOW_CONFIDENCE.",
                "degrade_active": True,
            }
            return
        hint_tier = council.pre_work_hint(f"{ticket.title}\n{ticket.body}")
        plan = council.plan(self.config, hint_tier)
        decision, reason = council.decide_budget(
            self.config, progress,
            balance_eur=balance_eur, est_cost_eur=plan.est_cost_eur)
        if decision == council.BudgetDecision.STOP:
            payload["_credits_stop"] = reason   # signal to next_ticket to terminate
            return
        if decision == council.BudgetDecision.DEGRADE:
            self._set_degrade_flag()
            payload["council"] = {
                "disabled": reason,
                "protocol": "Per-night credits exhausted (degrade mode). Do NOT convene councils. "
                            "The harness still flags high-risk diffs DONE_LOW_CONFIDENCE.",
                "degrade_active": True,
            }
            return
        payload["council"] = {
            "hint_tier": hint_tier.value,
            "summary": plan.summary_line(ticket.id),
            "proposers": plan.proposers, "judges": plan.judges, "mode": plan.mode,
            "max_tokens": plan.max_tokens, "est_cost_eur": plan.est_cost_eur,
            "protocol": ("Run a tokonomix council on your plan/diff (consensus mode). LIGHT=de-anchoring; "
                         "HEAVY (schema/security/api/money/cross-service)=plan+diff. Pull fresh slugs "
                         "from tokonomix_list_models. Show this summary + the post-call cost. If the "
                         "council errors/times out, log the blind-spot and PROCEED. Then pass "
                         "--council-verdict pass|concerns|error and --council-coverage to `complete`."),
        }

    def _attach_specialist_hint(self, payload: dict, ticket) -> None:
        """Non-binding pre-work hint so the agent can line up the right specialist lenses BEFORE
        implementing. The binding set is recomputed from the ACTUAL diff at complete-time (which only
        records coverage + can escalate to daylight review, never silently downgrade it). Specialist
        reviews are paid tokonomix calls, so they share the council's per-night spend brake."""
        from . import council, specialists
        if not specialists.enabled(self.config):
            return
        progress = self._load_progress()
        # Reuse decide_budget (no est_cost_eur for specialists — they share the same brake).
        decision, reason = council.decide_budget(self.config, progress)
        if decision != council.BudgetDecision.PROCEED:
            payload["specialists"] = {"disabled": reason,
                                      "protocol": "Per-night review spend reached — do NOT convene "
                                                  "more specialist reviews. High-risk diffs are still "
                                                  "flagged for daylight review. Proceed."}
            return
        roles = specialists.pre_work_hint(f"{ticket.title}\n{ticket.body}")
        plan = specialists.plan(self.config, roles)
        payload["specialists"] = {
            "roles": [r.value for r in roles],
            "summary": plan.summary_line(),
            "model_by_role": plan.model_by_role,
            "est_cost_eur": plan.est_cost_eur,
            "protocol": ("Run each specialist lens as a focused tokonomix review of your plan/diff "
                         "(architect + security always; the others only when the diff touches their "
                         "domain). Report any lens that raised a MATERIAL concern via "
                         "--specialist-concerns to `complete` (comma-separated roles) and the real € "
                         "via --specialist-cost. A concern in architect/security/tenant-safety forces "
                         "a daylight review; it never blocks the run."),
        }

    def _attach_onboarding_hint(self, payload: dict) -> None:
        """If tokonomix is configured but its credential is missing, surface the keyless onboard
        directive (interactive) or note that review is degraded (unattended). The actual
        tokonomix_onboard/_verify MCP calls are the agent's; the harness only flags the need."""
        from . import onboarding
        d = onboarding.directive(self.config, interactive=not self.orch.unattended)
        if d:
            payload["onboarding"] = d

    def complete_ticket(self, *, attempted: str, cannot_implement: bool = False,
                        review_coverage: str | None = None,
                        council_verdict: str | None = None,
                        council_cost_eur: float = 0.0,
                        council_http_status: int | None = None,
                        specialist_concerns: list | None = None,
                        specialist_cost_eur: float = 0.0) -> dict:
        """Gate + record the ticket the agent just implemented, then tell it to ask for the next.

        `attempted` is the agent's short self-report of what it did (or why it couldn't, when
        `cannot_implement`). `review_coverage` records which reviewers/councils ran (Phase-2 seam).
        `council_http_status`: when the council gateway returned HTTP 402 (insufficient_balance),
        pass 402 here — the harness maps it to the configured credits-exhaustion policy and persists
        the degrade flag (policy B) or signals STOP on the NEXT `next` call (policy A). The current
        ticket is still completed normally."""
        self._beat("record")
        # Rejoin the run branch before finalize commits the result / reverts (INT-1825 bug 2):
        # this is a fresh process and must not commit onto the operator's branch.
        self._enter_run_branch()
        # 402 from the council gateway: map to decide_budget with http_402=True and persist the
        # degrade flag (policy B) or set a progress marker for STOP (policy A). The current ticket
        # completes normally — the policy fires on the NEXT `next` call.
        if council_http_status == 402:
            from . import council as _council
            if _council.enabled(self.config):
                decision, reason = _council.decide_budget(
                    self.config, self._load_progress(), http_402=True)
                if decision == _council.BudgetDecision.DEGRADE:
                    self._set_degrade_flag()
                    print(f"[agents-never-sleep] 402 insufficient_balance → policy B (degrade): "
                          f"{reason}. Councils disabled for the rest of this run.")
                elif decision == _council.BudgetDecision.STOP:
                    # Persist a stop-requested flag so the NEXT `next` call returns STOPPED_CREDITS.
                    p = self._load_progress()
                    p["credits_stop_requested"] = reason
                    _atomic_write_json(self.progress_path, p)
                    print(f"[agents-never-sleep] 402 insufficient_balance → policy A (stop): "
                          f"{reason}. Run will stop after this ticket.")
        pending = self._load_pending()
        if pending is None:
            return {"status": "ERROR",
                    "error": "no ticket in flight — call `next` to get a PROCEED ticket first"}
        ticket = self._ticket_by_id(pending.ticket_id)
        if ticket is None:
            # INT-1675 #1 (data-loss fix): distinguish "ticket genuinely deleted" from
            # "ticket source was never loaded". A revert here destroys already-completed
            # work, so it must require POSITIVE evidence the ticket vanished — i.e. the
            # source WAS loaded (self.tickets non-empty) yet this id is absent. When the
            # source was not loaded at all (e.g. `complete` run without --tickets and no
            # Paperclip source), do NOT revert: preserve the working tree + the in-flight
            # pending record so the operator can re-run `complete` with the source.
            if not self.tickets:
                return {"status": "ERROR",
                        "error": (f"ticket {pending.ticket_id} could not be resolved: no ticket "
                                  f"source was loaded. Pass --tickets <dir> (or enable Paperclip) "
                                  f"and re-run `complete`. Working tree and in-flight ticket left "
                                  f"INTACT — nothing was reverted.")}
            # Source loaded but this id is genuinely absent → the file vanished mid-flight.
            try:
                self.orch.git.revert_to(pending.snapshot)
            except GitError as exc:
                # Backup-before-revert failed; revert_to aborted to preserve WIP. Keep pending +
                # working tree intact rather than crashing — the operator recovers in daylight.
                return {"status": "ERROR",
                        "error": (f"ticket {pending.ticket_id} no longer present, but the revert was "
                                  f"aborted to preserve WIP ({exc}); working tree + in-flight ticket "
                                  "left INTACT — check git health.")}
            self._clear_pending()
            return {"status": "ERROR",
                    "error": f"ticket {pending.ticket_id} no longer present; reverted and skipped"}

        # In degrade mode (policy B), councils are skipped: floor DONE to DONE_LOW_CONFIDENCE so
        # the trust-gating fires even on low-risk (LIGHT) diffs that the council never reviewed.
        degrade_active = bool(self._load_progress().get("credits_exhausted_degrade"))
        outcome = self.orch.finalize_after_edit(
            ticket, pending, attempted or "(no summary provided)",
            cannot_implement=cannot_implement, review_coverage=review_coverage,
            council_config=self.config, council_verdict=council_verdict,
            specialist_concerns=specialist_concerns, credits_degrade=degrade_active)
        self._clear_pending()
        self._bump_progress(is_bad=outcome.state in BAD_STATES)
        # a council was convened iff the agent reported a verdict — track its spend for the brake
        if council_verdict:
            self._bump_council(council_cost_eur)
        # specialist lens reviews are paid too: fold their € into the same per-night spend cap
        # (without consuming a full-council call slot).
        self._bump_spend(specialist_cost_eur)
        # Non-terminal outcomes (FAILED_RETRYABLE / BLOCKED_ENV / FAILED_BUG_IN_AGENT) are set aside
        # for the rest of this run; the next resume retries them under the cross-resume attempt cap.
        if outcome.state not in TERMINAL_SKIP_ON_RESUME:
            self._add_skip(ticket.id)
        # Count this completion toward the per-session budget (no-op unless the feature is on).
        # Counting every RECORDED completion — not just DONE — is intentional: context degradation
        # tracks session LENGTH, not success, and retryable items re-queue for the next session.
        budget_reached = self._bump_session_count()
        # When the per-session budget is just reached, instruct the agent to STOP (do NOT call
        # `next`): the agent's loop is complete->next, so a complete that says "stop" is the only
        # deterministic brake. The launcher resumes the backlog in a FRESH session, and the
        # Stop-hook allows the early stop because the session-budget-reached marker now exists. The
        # run-incomplete sentinel is untouched, so if the backlog actually drained at exactly N the
        # launcher just respawns one cheap session that DRAINs and clears the sentinel. Default-off
        # (budget unset) always takes the legacy "call next" branch — byte-identical behaviour.
        next_hint = ("session budget reached — STOP now (do NOT call `next`); the launcher will "
                     "resume you in a fresh session to continue the backlog"
                     if budget_reached else "call `next` for the next ticket")
        return {
            "status": "RECORDED",
            "ticket_id": ticket.id,
            "state": outcome.state.value,
            "why": outcome.why,
            "bad": outcome.state in BAD_STATES,
            "next": next_hint,
        }
