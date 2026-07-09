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
import sys
import tempfile
import time

from .orchestrator import BAD_STATES, Orchestrator, ProceedToken
from .vcs import GitError
from .decide import Action
from .report import build_report
from .state import (ContaminationScope, OutcomeState, TERMINAL_SKIP_ON_RESUME,
                    OutcomeStore, TicketOutcome)


class RunResumeUnsafe(Exception):
    """A persisted run-branch cannot be safely resumed: its recorded base is no longer an ancestor
    of the operator's branch (a stale/stranger run left behind by a prior kill-9), or the branch/base
    vanished. Raised — deliberately NOT a GitError, so the `_enter_run_branch` degrade-catch does not
    swallow it — to force a LOUD HALT (non-zero exit, run-branch.json left intact for inspection)
    instead of a silent checkout that would move HEAD off live commits and delete untracked files."""


def live_tree_decision(is_linked: bool, is_clean: bool, policy) -> str:
    """Pure policy for the G4a live-tree isolation gate. Returns 'ok' | 'warn' | 'halt'.

    Risk = running UNISOLATED (the primary worktree, not a linked `git worktree`) in a tree that
    still has clobberable live state (not clean). No risk → 'ok' for every policy. On risk:
    'ack' → 'ok' (operator acknowledged), 'require_isolation' → 'halt', anything else (the default
    'warn', or a missing/unknown value) → 'warn'."""
    risk = (not is_linked) and (not is_clean)
    if not risk:
        return "ok"
    # Normalize so a case/format variant of a policy fails SAFE, not open: without this,
    # "Require_Isolation" would fall through to warn (proceed) when the operator demanded a HALT.
    p = policy.strip().lower() if isinstance(policy, str) else ""
    if p == "ack":
        return "ok"
    if p == "require_isolation":
        return "halt"
    return "warn"


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


# F5 (build-narrow, requirement_meaning only): a deterministic PER-RUN ceiling on how many tickets
# get an F5 offer. Separate from the council's €/call caps — F5 makes its own, cheaper single-call
# tokonomix requests and must never silently borrow the council's per-night budget. NOT a config
# field in Plan 1 (no config-schema change in scope) — Plan 2 may expose it as
# budget.max_f5_calls_per_night once F5 gains a config schema.
_F5_MAX_CALLS_PER_RUN = 5


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
        # The sentinel is pinned to the Stop-hook's path (usually inside the repo — see __init__),
        # NOT to state_dir, so the read-only-repo state-dir redirect cannot cover it. An unwritable
        # repo must degrade to a blind-spot note, not crash `next` with a raw PermissionError
        # (2026-07-08 E2E, 2.1): writing it anywhere else is pointless anyway — the Stop-hook only
        # reads THIS path, so never-stop is inactive either way.
        try:
            os.makedirs(os.path.dirname(self.sentinel_path), exist_ok=True)
            if not os.path.exists(self.sentinel_path):
                with open(self.sentinel_path, "w", encoding="utf-8") as fh:
                    fh.write("run in progress\n")
        except OSError as exc:
            self.key_blind_spots.append(
                f"run-incomplete sentinel could not be written at {self.sentinel_path!r} "
                f"({exc}) — the Stop-hook never-stop guard is INACTIVE for this run and every "
                "`next` call will look like a fresh run start")

    def _clear_sentinel(self) -> None:
        try:
            if os.path.exists(self.sentinel_path):
                os.unlink(self.sentinel_path)
        except OSError as exc:
            # A sentinel that survives the terminal keeps the Stop-hook blocking stops after the
            # run is over — surface it instead of crashing the terminal JSON (read-only repo case).
            self.key_blind_spots.append(
                f"run-incomplete sentinel at {self.sentinel_path!r} could not be removed ({exc}) "
                "— the Stop-hook may keep blocking agent stops until it is deleted manually")

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

    def _save_runbranch(self, run_branch: str, original_branch: str, base: str) -> None:
        # `base` = HEAD SHA at branch creation. Persisted so a later process can assert the run is
        # still descended from the operator's branch before resuming it (stale-resume guard).
        _atomic_write_json(self.runbranch_path,
                           {"run_branch": run_branch, "original_branch": original_branch,
                            "base": base})

    def _resume_is_safe(self, git, state: dict) -> bool:
        """Deterministic freshness check before resuming a persisted run branch. Safe iff the branch
        still exists, the recorded base still resolves, and that base is an ancestor of BOTH the
        operator's branch (`original_branch` — the ground truth, NEVER current HEAD, which in a
        shared checkout may be junk left by the prior process) AND the run branch's own tip (so a
        force-rewound run branch is rejected too). Any missing field / vanished ref → unsafe."""
        run_branch = state.get("run_branch")
        base = state.get("base")
        original = state.get("original_branch")
        if not run_branch or not base or not original:
            return False  # pre-guard or truncated state — unverifiable, treat as stale
        try:
            if not git.branch_exists(run_branch) or not git.object_exists(base):
                return False
            return git.is_ancestor(base, original) and git.is_ancestor(base, run_branch)
        except GitError:
            # The safety check itself hit the raise class (git binary missing / hung / timeout), so
            # the lineage is UNVERIFIABLE. Fail to the SAFE side (⇒ HALT) — matching this guard's
            # stated 'unverifiable ⇒ HALT' intent. Without this, the outer `except GitError` in
            # _enter_run_branch would swallow it and silently degrade-and-proceed (the opposite of
            # HALT). A transient timeout self-heals: the next invocation with healthy git re-checks
            # and resumes, since the HALT left run-branch.json intact.
            return False

    def _clear_runbranch(self) -> None:
        if os.path.exists(self.runbranch_path):
            os.unlink(self.runbranch_path)

    def _clear_resume_halt(self) -> None:
        """Clear the launcher stop-marker written by run.py on a RunResumeUnsafe HALT. Called on
        every healthy run-branch entry so that once the operator resolves the stale state (or a
        fresh run starts cleanly), the fresh-session loop stops treating the run as halted."""
        try:
            os.unlink(os.path.join(self.state_dir, "resume-halt"))
        except OSError:
            pass

    _LIVE_TREE_MSG = (
        "running UNISOLATED in a live shared working tree that has uncommitted/untracked changes — "
        "the harness's snapshots and reverts act on that live state, so a revert can discard a "
        "human's work. Run inside a `git worktree`, or set `autonomy.live_tree: ack` to acknowledge.")

    def _live_tree_gate(self):
        """G4a fresh-run isolation check. Returns a terminal dict to return (HALT) or None to
        continue. On 'warn' persists a note (so the terminal report — a later process — can surface
        it) and prints to stderr; on 'ack'/'ok' clears any stale note. Best-effort: a git error here
        does NOT block (the run-branch layer's own degrade path handles an unhealthy git)."""
        git = self.orch.git
        # Drop any stale note from a prior run FIRST, so a GitError below (git unhealthy → we cannot
        # re-evaluate) never leaves an old warning to be surfaced by this run's terminal report.
        note_path = os.path.join(self.state_dir, "live-tree-note")
        try:
            os.unlink(note_path)
        except OSError:
            pass
        try:
            # Measure HUMAN dirt only: exclude the harness's own paths (self.protect, e.g. .unattended),
            # which are untracked here because ensure_safety_net has not gitignored them yet — counting
            # them would false-positive a pristine tree (spurious warn, or a spurious HALT).
            decision = live_tree_decision(
                git.is_linked_worktree(), git.is_clean(exclude=git.protect),
                (self.config.get("autonomy", {}) or {}).get("live_tree", "warn"))
        except GitError:
            return None
        if decision == "halt":
            return self._terminate("HALTED", reason="live-tree isolation required: " + self._LIVE_TREE_MSG)
        if decision == "warn":
            try:
                os.makedirs(self.state_dir, exist_ok=True)
                with open(note_path, "w", encoding="utf-8") as fh:
                    fh.write(self._LIVE_TREE_MSG)
            except OSError:
                pass
            print(f"[agents-never-sleep] WARNING: {self._LIVE_TREE_MSG}", file=sys.stderr)
        return None

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
                # Stale-resume guard: never blindly check out a persisted run branch. A kill-9 leaves
                # run-branch.json behind; resuming a run whose base no longer descends from the
                # operator's branch would move HEAD off live commits and delete untracked files.
                if not self._resume_is_safe(git, state):
                    raise RunResumeUnsafe(
                        f"refusing to resume run branch {state.get('run_branch')!r}: its recorded "
                        f"base is no longer an ancestor of {state.get('original_branch')!r} (stale "
                        "state from a prior interrupted run). No files were touched. Inspect the run "
                        f"branch, then remove {self.runbranch_path} (or run in an isolated worktree) "
                        "to start a fresh run.")
                if git.current_ref() != state["run_branch"]:
                    git.checkout(state["run_branch"])
                self._clear_resume_halt()  # safe resume — no longer halted
                return
            original = git.current_ref()
            base = git.head()  # the operator-branch tip the run branch is cut from
            if not git.object_exists(base):
                # Unborn HEAD (a `git init`'d repo with no commit yet): git.head() yields a non-commit
                # ("HEAD" or ""), not a SHA. Persisting it as `base` would either wedge the next
                # process on a false HALT or, worse, silently DEFEAT the guard (is_ancestor of a
                # symbolic HEAD against itself is always true). There is no baseline to record or
                # revert to, so defer isolation (like the git-unavailable degrade) until a real
                # baseline commit exists — the next process re-creates the branch off a real base.
                self.key_blind_spots.append(
                    "run-branch isolation deferred: repo has no baseline commit yet (unborn HEAD)")
                return
            run_branch = f"ans/run-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
            git.create_run_branch(run_branch)
            self._save_runbranch(run_branch, original, base)
            self._clear_resume_halt()  # fresh run started cleanly — no longer halted
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
                "credits_exhausted_degrade": False, "credits_stop_requested": None, "f5_calls": 0}
        if not os.path.exists(self.progress_path):
            return base
        try:
            with open(self.progress_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return {"processed": int(d.get("processed", 0)), "bad": int(d.get("bad", 0)),
                    "council_cost_eur": float(d.get("council_cost_eur", 0.0) or 0.0),
                    "council_calls": int(d.get("council_calls", 0) or 0),
                    "credits_exhausted_degrade": bool(d.get("credits_exhausted_degrade", False)),
                    "credits_stop_requested": d.get("credits_stop_requested"),
                    "f5_calls": int(d.get("f5_calls", 0) or 0)}
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return base

    def _reset_progress(self) -> None:
        _atomic_write_json(self.progress_path,
                           {"processed": 0, "bad": 0, "council_cost_eur": 0.0, "council_calls": 0,
                            "credits_exhausted_degrade": False, "credits_stop_requested": None,
                            "f5_calls": 0})

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
                            "credits_exhausted_degrade": False, "credits_stop_requested": None,
                            "f5_calls": p["f5_calls"]})

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

    def _bump_f5_calls(self) -> None:
        """Accumulate this run's F5 call count for the deterministic per-run ceiling (gap #5).
        Mirrors _bump_council's accounting shape but is a SEPARATE counter — F5 must never silently
        borrow the council's per-night €/call budget."""
        p = self._load_progress()
        p["f5_calls"] = int(p.get("f5_calls", 0)) + 1
        _atomic_write_json(self.progress_path, p)

    def _breaker_tripped(self) -> bool:
        p = self._load_progress()
        return self.orch.breaker.tripped(p["processed"], p["bad"])

    def _ticket_by_id(self, ticket_id: str):
        for t in self.tickets:
            if t.id == ticket_id:
                return t
        return None

    def _f5_offer(self, ticket, decision, has_net: bool) -> dict | None:
        """F5 (build-narrow, gap #3): check eligibility for a PARK decision. If eligible, open a
        durable OFFER RECORD OPTIMISTICALLY — BEFORE the agent runs consensus — so a crash, a
        persistently-erroring council, or the agent simply calling `next` again without resolving
        can never cause an infinite re-offer of the same ticket; the very next scheduling pass sees
        already_attempted=True and falls through to a normal park. The record's category/foundational
        snapshot (taken HERE, at offer time) is the trusted anchor resolve_park re-checks against
        later — never a fresh re-classification of the ticket text.

        Returns the PARK_CONSENSUS_ELIGIBLE payload (including a fresh attempt_id the agent must echo
        back on resolve-park), or None (caller falls through to a normal park: either structurally
        ineligible, already attempted, or the per-run budget ceiling is reached — in ALL cases the
        ticket then gets an ordinary, terminal park, exactly like any other PARK reason; hitting the
        ceiling does not defer or re-queue it, it just doesn't get F5 this run)."""
        if self.orch.ledger is None:
            return None
        from . import f5
        already = self.orch.ledger.f5_attempted(ticket.id)
        from . import consensus_scope
        cats = consensus_scope.effective_categories(
            self.orch.consensus_assisted_categories, ticket.declared_consensus_assisted)
        if cats is None:
            return None  # ticket explicitly opted OUT (consensus_assisted: false) — normal park
        if not f5.eligible(decision, has_safety_net=has_net, already_attempted=already,
                           consensus_assisted_categories=cats):
            return None
        if self._load_progress().get("f5_calls", 0) >= _F5_MAX_CALLS_PER_RUN:
            return None
        import uuid
        attempt_id = uuid.uuid4().hex[:16]
        self.orch.ledger.open_f5_offer(
            ticket.id, attempt_id=attempt_id, category=decision.category,
            has_safety_net=has_net, foundational=decision.foundational,
            consensus_assisted_categories=cats)
        self._bump_f5_calls()
        if decision.category == "requirement_meaning":
            prompt = f5.build_grounding_prompt(
                ticket_title=ticket.title, ticket_body=ticket.body,
                candidate_readings=["derive the plausible distinct readings from the ticket body below"],
                repo_context=f"See the repository under the ticket's stated path "
                            f"({ticket.path or 'repo root'}) for existing conventions relevant to "
                            "disambiguation.",
                safety_net_desc="git revert to the pre-ticket snapshot is available "
                               "(reversibility safety net confirmed).")
        else:
            prompt = f5.build_soundness_prompt(
                ticket_title=ticket.title, ticket_body=ticket.body, category=decision.category,
                repo_context=f"See the repository under the ticket's stated path "
                            f"({ticket.path or 'repo root'}) for existing conventions, migration "
                            "style, auth/tenant patterns, and interface contracts relevant to "
                            "judging soundness.",
                safety_net_desc="git revert to the pre-ticket snapshot is available "
                               "(reversibility safety net confirmed).")
        if decision.category == "requirement_meaning":
            instructions = (
                "This ticket would otherwise PARK because its requirement meaning is ambiguous. Run "
                "a grounded tokonomix consensus (parallel+blind proposers, a disjoint judge) using "
                "the supplied `prompt` VERBATIM — never ask a free-text 'should I proceed?'. Then "
                "call `resolve-park --ticket-id <id> --attempt-id <the attempt_id from this payload> "
                "[--resolved --chosen-reading ... --evidence ... --dissent-count N "
                "--synthesis-text ... | --not-resolved]`.")
        else:
            instructions = (
                f"This ticket is in the high-risk category '{decision.category}' and would otherwise "
                "PARK for human review. The requirement is NOT ambiguous — the author already decided "
                "it. Run a grounded tokonomix consensus using the supplied `prompt` VERBATIM (a "
                "SOUNDNESS check — never a free-text 'should I proceed?'). Then call `resolve-park "
                "--ticket-id <id> --attempt-id <the attempt_id from this payload>`: report `--resolved` "
                "ONLY on an affirmatively-sound, evidence-cited verdict (with `--chosen-reading <the "
                "one-line soundness conclusion> --evidence ... --dissent-count N --synthesis-text ...`); "
                "report `--defect-found` if the consensus found a concrete defect (a deterministic veto "
                "that keeps the ticket parked); otherwise `--not-resolved`. A resolved hard-category "
                "change is applied unattended but recorded DONE_LOW_CONFIDENCE for daylight review.")
        return {
            "status": "PARK_CONSENSUS_ELIGIBLE",
            "ticket": {"id": ticket.id, "title": ticket.title, "body": ticket.body,
                       "path": ticket.path},
            "category": decision.category,
            "attempt_id": attempt_id,
            "prompt": prompt,
            "instructions": instructions,
        }

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
        # G4a: a live-tree warning was raised at run start (a different process) — surface it here.
        note_path = os.path.join(self.state_dir, "live-tree-note")
        if os.path.exists(note_path):
            try:
                with open(note_path, encoding="utf-8") as fh:
                    notes.append("LIVE-TREE: " + fh.read().strip())
            except OSError:
                pass
        done = sum(1 for o in outcomes if o.state.value.startswith("DONE"))
        stop_notes = list(notes)
        if status == "STOPPED_CREDITS":
            stop_notes.insert(0, f"CREDITS EXHAUSTED — run stopped cleanly: {reason}")
        try:
            backup_refs = self.orch.git.list_backup_refs()
        except GitError:
            backup_refs = []
        report = build_report(
            outcomes, run_label=self.run_label,
            halted=(status == "HALTED"), halt_reason=reason,
            stopped_low_yield=(status == "LOW_YIELD"), notes=stop_notes,
            work_branch=(run_branch if done else None),
            backup_refs=backup_refs,
        )
        # The terminal JSON is the agent-facing contract — it must reach the agent even when
        # the repo root is unwritable (read-only fs is the flagship HALT case, so the report
        # write fails in exactly the scenario that needs the HALTED signal most). Degrade:
        # fall back to the (usually still-writable) state dir, else report_path=null with an
        # explanatory note — never an unhandled traceback (2026-07-08 E2E finding).
        report_path, report_error = self.report_path, None
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(report)
        except OSError as exc:
            report_error = str(exc)
            fallback = os.path.join(self.state_dir, os.path.basename(self.report_path))
            try:
                with open(fallback, "w", encoding="utf-8") as fh:
                    fh.write(report)
                report_path = fallback
            except OSError:
                report_path = None
        out = {"status": status, "reason": reason, "report_path": report_path,
               "processed": len(outcomes), "done": done, "run_branch": run_branch,
               "message": (f"backlog {status}; report written to {report_path}"
                           if report_path else
                           f"backlog {status}; report could NOT be written ({report_error})")
                          + (f"; work on branch {run_branch}" if run_branch and done else "")}
        if report_error:
            out["report_error"] = report_error
        return out

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
        # A fresh run (no sentinel yet, nothing in flight) clears the per-run "set aside" list so a
        # new resume retries the tickets a prior run set aside. A continuing run keeps it. Loaded
        # early so the G4a gate below can run before ensure_safety_net.
        pending = self._load_pending()
        # Live-tree isolation gate (G4a): at a FRESH run start with work to do, apply
        # autonomy.live_tree (warn|ack|require_isolation). Measure BEFORE ensure_safety_net (which
        # writes .gitignore and would dirty a clean tree, false-positiving the check) and before any
        # run branch — require_isolation returns a HALT here, nothing has been mutated yet.
        if (self.tickets and pending is None and not os.path.exists(self.sentinel_path)
                and self.orch.git.is_repo()):
            halted = self._live_tree_gate()
            if halted is not None:
                return halted
        has_net = self.orch.git.ensure_safety_net()

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
                    git = self.orch.git
                    cur = git.head()
                    if cur and git.object_exists(pending.snapshot) \
                            and git.is_ancestor(pending.snapshot, cur):
                        git.revert_to(pending.snapshot)
                    else:
                        # Lineage guard: pending.json survived from a PRIOR run this checkout does
                        # not descend from — e.g. the operator cleared a stale run-branch.json after
                        # a HALT_RESUME_UNSAFE and a fresh run branch was just forked. `reset --hard`
                        # to that snapshot would silently graft the fresh branch onto the old run's
                        # disconnected history (and make the NEXT call HALT on the freshness check).
                        # Instead discard the carried-over partial edits WITHOUT leaving this lineage
                        # (revert to the current tip; dirty WIP is set aside as a backup ref) and let
                        # the ticket be re-scheduled under its attempt cap.
                        if cur and git.object_exists(cur):
                            git.revert_to(cur)
                        self.key_blind_spots.append(
                            f"stale pending checkpoint for {pending.ticket_id} discarded: its "
                            f"snapshot {pending.snapshot!r} is not an ancestor of the current "
                            "checkout (leftover from a prior interrupted run); any carried-over "
                            "partial edits were set aside as a recoverable backup ref and the "
                            "ticket will be re-attempted under its attempt cap")
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
                offer = self._f5_offer(ticket, decision, has_net)
                if offer is not None:
                    return offer
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
            return self._hand_out_proceed(payload, ticket, began, balance_eur=balance_eur)

        # No proceedable ticket remains -> the backlog is genuinely drained.
        return self._terminate("DRAINED")

    def _hand_out_proceed(self, payload: dict, ticket, token, *, balance_eur=None) -> dict:
        """Attach the pre-work hints to a PROCEED payload and hand it out — the SHARED tail of both
        PROCEED paths (a fresh `next` handout and an F5-resolved resolve-park handout), so the two
        stay in lockstep. `token` is the ProceedToken whose snapshot is reverted if the council
        budget gate returns STOP. On STOP: revert the accounting snapshot, clear pending, terminate
        STOPPED_CREDITS (nothing was handed out). Otherwise: attach specialist/onboarding/scratchpad
        hints and return the payload."""
        self._attach_council_hint(payload, ticket, balance_eur=balance_eur)
        if "_credits_stop" in payload:
            reason = payload.pop("_credits_stop")
            # Revert the snapshot commit (nothing was done, just an accounting commit).
            try:
                self.orch.git.revert_to(token.snapshot)
            except Exception:  # noqa: BLE001 — revert failure must not shadow the credits stop
                pass
            self._clear_pending()
            return self._terminate("STOPPED_CREDITS", reason=reason)
        self._attach_specialist_hint(payload, ticket)
        self._attach_onboarding_hint(payload)
        self._attach_scratchpad_hint(payload, ticket)
        return payload

    def resolve_park(self, ticket_id: str, attempt_id: str, verdict) -> dict:
        """Callback for the agent's grounded F5 consensus result on a ticket `next` previously
        offered as PARK_CONSENSUS_ELIGIBLE. Never re-classifies the ticket text: validates
        `attempt_id` against the durable ledger OFFER RECORD (Task 1), short-circuits as a no-op if
        the ticket already reached a terminal outcome or the offer was already consumed, and checks
        the CURRENT safety net explicitly before delegating to Orchestrator.resolve_park with the
        persisted `offer`. RESOLVE routes into the normal PROCEED path (begin_proceed); KEEP_PARKED
        writes a park outcome with a full F5 audit trail. HALT is surfaced if the safety net vanished
        meanwhile. The offer is consumed (status flipped to 'consumed') on return either way,
        closing the re-entry hole a forged/stale/duplicate resolve-park would otherwise open."""
        self._beat("resolve-park")
        self._enter_run_branch()
        ticket = self._ticket_by_id(ticket_id)
        if ticket is None:
            return {"status": "ERROR", "error": f"ticket {ticket_id} not found — pass --tickets <dir>"}
        # Idempotency: a ticket already in a terminal outcome is never re-opened (mirrors
        # next_ticket's is_terminal skip, driver.py:571). A duplicate/stale resolve-park is a no-op.
        prior = self.orch.is_terminal(ticket_id)
        if prior is not None:
            return {"status": "ALREADY_RESOLVED", "ticket_id": ticket_id, "state": prior.state.value,
                    "note": "this ticket already reached a terminal outcome; resolve-park ignored"}
        # Validate the callback against the durable offer record (closes forged/stale/no-offer).
        offer = self.orch.ledger.get_f5_offer(ticket_id) if self.orch.ledger else None
        if offer is None:
            return {"status": "ERROR", "error": f"no F5 offer was issued for {ticket_id}"}
        if offer.get("status") != "offered":
            return {"status": "ALREADY_RESOLVED", "ticket_id": ticket_id,
                    "note": "this F5 offer was already consumed; resolve-park ignored"}
        if attempt_id != offer.get("attempt_id"):
            return {"status": "ERROR", "error": "attempt-id does not match the outstanding F5 offer"}
        # Safety net must exist NOW to proceed (reversibility). Losing it mid-run is HALT-worthy —
        # checked directly, NOT by re-classifying the (possibly-mutated) ticket text.
        if not self.orch.git.ensure_safety_net():
            return self._terminate("HALTED",
                                   reason=f"reversibility safety net vanished before resolving {ticket_id}")
        result = self.orch.resolve_park(ticket, offer, verdict)
        self.orch.ledger.consume_f5_offer(ticket_id)   # flip to terminal status either way
        if isinstance(result, TicketOutcome):
            self._bump_progress(is_bad=True)
            if self._breaker_tripped():
                return self._terminate("LOW_YIELD")
            return {"status": "KEPT_PARKED", "ticket_id": ticket.id, "state": result.state.value,
                    "why": result.why, "next": "call `next` for the next ticket"}
        self._save_pending(result)
        self._set_sentinel()
        payload = {"status": "PROCEED",
                   "ticket": {"id": ticket.id, "title": ticket.title, "body": ticket.body,
                              "path": ticket.path},
                   "attempt": result.attempt_n, "snapshot": result.snapshot,
                   "instructions": ("F5 consensus resolved the ambiguity for this ticket — "
                                    "implement ONLY this ticket, then call `complete`. Do not stop "
                                    "or ask. (A hard-PARK category resolution is recorded "
                                    "DONE_LOW_CONFIDENCE for daylight review.)")}
        return self._hand_out_proceed(payload, ticket, result, balance_eur=None)

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

    def _attach_scratchpad_hint(self, payload: dict, ticket) -> None:
        """Ticket 04 (flag: autonomy.scratchpad.enabled, default off → payload byte-identical).

        Re-inject the ticket's revert-surviving notes so a resumed/fresh agent CONTINUES its
        reasoning instead of re-deriving after a crash+revert, plus a compact do-not-repeat
        digest of the dead ends already tried this run. Both are inert until the flag is set."""
        if not (self.config.get("autonomy", {}).get("scratchpad", {}) or {}).get("enabled"):
            return
        from . import scratchpad
        notes = scratchpad.read_notes(self.state_dir, ticket.id)
        if notes:
            payload["notes"] = notes
            payload["notes_path"] = scratchpad.notes_path(self.state_dir, ticket.id)
        digest = scratchpad.do_not_repeat_digest(
            self.store, self._load_skip(), current_ticket_id=ticket.id)
        if digest:
            payload["do_not_repeat"] = digest
        if notes or digest:
            # Interpolate the ACTUAL --repo/--state-dir so the note lands where read_notes looks,
            # even for a non-default run (a bare command would assume `--repo .` + default dir).
            repo = getattr(self.orch, "repo_dir", ".")
            try:
                rel_state = os.path.relpath(self.state_dir, repo)
            except ValueError:
                rel_state = self.state_dir
            payload.setdefault(
                "scratchpad_note",
                "Persistent context for this ticket is attached (payload.notes and/or "
                "do_not_repeat). CONTINUE from it — do not re-derive prior reasoning or re-try "
                "listed dead ends. Log new progress/decisions as you go with: "
                f"python3 -m agents_never_sleep.run note --repo {repo} --state-dir {rel_state} "
                f"--ticket {ticket.id} --text '...' (survives a gate-fail revert; code still "
                "rolls back to green).")

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
                        council_verdict_structured: dict | None = None,
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
            council_verdict_structured=council_verdict_structured,
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
