#!/usr/bin/env python3
"""CLI entrypoint the SKILL.md invokes.

The loop is AGENT-DRIVEN (the agent is the worker), so the primary interface is two subcommands the
agent alternates between until the backlog drains:

    python3 -m agents_never_sleep.run next      [--repo . --tickets tickets]   -> JSON: a PROCEED ticket OR a
                                                                       terminal signal (DRAINED/
                                                                       HALTED/LOW_YIELD)
    python3 -m agents_never_sleep.run complete  --attempted "what I did"        -> JSON: the recorded outcome
                                     [--cannot-implement] [--review-coverage ...]

`next` owns the run-incomplete sentinel and writes every PARK/cap outcome itself; the agent only
implements the single ticket body it is handed, then calls `complete`. See harness/driver.py.

Auxiliary subcommands:
    python3 -m agents_never_sleep.run report    -> (re)write the morning report from the durable store

All subcommands print a single JSON object to stdout so a driver/agent can parse the result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import config
from .config import ensure_config, load_config
from .driver import RunResumeUnsafe, StepDriver
from .fsutil import ensure_private_dir
from .gates import GateRunner
from .heartbeat import Heartbeat
from .ledger import AttemptLedger
from .orchestrator import Orchestrator
from .preflight import run_preflight, write_profile
from .report import build_report
from .state import OutcomeStore
from .tickets import load_tickets
def _resolve_report_path(repo: str, config: dict, args_report: str) -> str:
    """The morning-report path. `UE_REPORT_PATH` (an absolute path) wins — the G4b launcher sets it
    to the PRIMARY tree when a run is isolated in a worktree, so the report (ANS's central deliverable)
    survives worktree cleanup instead of vanishing with it. Mirrors the UE_HEARTBEAT override. Absent,
    it is the config's report.local_path (or --report) joined to the repo, as before."""
    return os.environ.get("UE_REPORT_PATH") or os.path.join(
        repo, (config.get("report", {}) or {}).get("local_path", args_report))


def _unattended() -> bool:
    return bool(os.environ.get("CLAUDE_UNATTENDED")) or not sys.stdin.isatty()


def _resolve_writable_state_dirs(repo: str, state_dir: str, artifacts_dir: str,
                                  blind_spots: list) -> tuple[str, str]:
    """The harness's own bookkeeping (outcome store, ledger, heartbeat, capability profile) lives
    under <repo>/.unattended by default. A repo that is read-only end to end (a locked-down
    working tree, a `:ro` mount) can't create that directory even when git itself still works —
    reversibility and the harness's scratch space are independent concerns, so a blocked scratch
    space must degrade, not crash. Before this, the very first disk write in _Context.__init__
    (preflight.write_profile) died with an unhandled PermissionError, so the driver never got a
    chance to run its own read-only handling (HALT / BLOCKED_ENV) (2026-07-08 E2E, second
    session). Redirect both dirs outside the repo, keyed by the repo's own path so repeated
    next/complete calls against the same repo keep finding the same fallback location."""
    try:
        ensure_private_dir(state_dir)
        ensure_private_dir(artifacts_dir)
        # makedirs(exist_ok=True) succeeds on an EXISTING dir even when it is read-only (the
        # "harness ran while writable, then the tree got locked" case), so probe an actual write —
        # otherwise the very first state write (progress/skip/outcome) still dies later with a raw
        # PermissionError instead of taking this redirect (2026-07-08 E2E, 2.1).
        for d in (state_dir, artifacts_dir):
            probe = os.path.join(d, f".writable-probe-{os.getpid()}")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("probe")
            os.remove(probe)
        return state_dir, artifacts_dir
    except OSError:
        pass
    import hashlib
    import tempfile
    tag = hashlib.sha1(os.path.abspath(repo).encode("utf-8")).hexdigest()[:16]
    base = os.path.join(tempfile.gettempdir(), f"agents-never-sleep-state-{tag}")
    fallback_state = os.path.join(base, "state")
    fallback_artifacts = os.path.join(base, "artifacts")
    try:
        ensure_private_dir(fallback_state)
        ensure_private_dir(fallback_artifacts)
        blind_spots.append(
            f"repo state dir {state_dir!r} is not writable — harness bookkeeping redirected to "
            f"{base!r} for this repo (read-only-repo degrade)")
        return fallback_state, fallback_artifacts
    except OSError as exc:
        blind_spots.append(
            f"repo state dir {state_dir!r} AND the fallback {base!r} are both unwritable "
            f"({exc}) — harness bookkeeping cannot persist")
        return state_dir, artifacts_dir


def _primary_gate(config: dict, repo: str):
    for g in config.get("gates", []):
        if g.get("blocking", True):
            return GateRunner(command=g["command"], cwd=repo,
                              timeout=config.get("budget", {}).get("per_ticket_timeout_s", 1800))
    return None


def _register_creds_file_key() -> None:
    """Register the well-known Tokonomix credentials-file key for redaction (and setdefault it as
    the env fallback), so a PATTERN-LESS key never lands verbatim in a harness-written surface —
    the shape-anchored pattern matcher cannot catch a bare Vault/creds value. Only harvests from
    the file when TOKONOMIX_API_KEY is unset (an already-set value is registered by
    register_env_secrets). Shared by _Context and cmd_note. Degrades silently on a missing/bad file."""
    if os.environ.get("TOKONOMIX_API_KEY"):
        return  # already set → register_env_secrets() harvests it (incl. pattern-less values)
    _cred_path = os.path.expanduser(
        os.environ.get("TOKONOMIX_CREDS_FILE", "~/.tokonomix/credentials.json"))
    try:
        import json as _json
        with open(_cred_path) as _f:
            _creds = _json.load(_f)
        _key = _creds.get("api_key") or _creds.get("token")
        if isinstance(_key, str):
            _key = _key.strip()
        if _key:
            from .redact import register_secret
            register_secret(_key)
            os.environ.setdefault("TOKONOMIX_API_KEY", _key)
    except (OSError, ValueError, AttributeError, TypeError):
        pass  # no file or parse error — degrade silently, onboarding gate handles it


class _Context:
    """Everything a subcommand needs, built once from args (preflight is cached on disk)."""

    def __init__(self, args):
        # Each next/complete is a fresh process, so harvest known-credential env values into the
        # redaction registry on every entry (the pattern matcher is the backstop; pattern-less
        # values like a Vault secret depend entirely on this).
        from .redact import register_env_secrets
        register_env_secrets()

        self.repo = os.path.abspath(args.repo)
        self.unattended = _unattended()
        self.key_blind_spots = []
        self.state_dir, self.artifacts_dir = _resolve_writable_state_dirs(
            self.repo, os.path.join(self.repo, args.state_dir),
            os.path.join(self.repo, args.artifacts_dir), self.key_blind_spots)

        # Preflight (git probes + a Paperclip socket probe) only needs to run when there is no
        # saved config yet — its sole consumer is config creation. On every later next/complete a
        # saved config exists, so skip it rather than re-probing the environment each call.
        existing = load_config(self.repo)
        if existing is not None:
            self.config = existing
        else:
            profile = run_preflight(self.repo, unattended=self.unattended)
            write_profile(profile, os.path.join(self.state_dir, "capability-profile.json"))
            self.config = ensure_config(self.repo, profile)

        # Fail-fast (Plan 2 §1): a misconfigured consensus-assisted opt-in must abort here, before
        # any ticket work, not surface later as a silent no-op deep in the offer path.
        config.validate_consensus_config(self.config)

        # Resolve a Vault-backed tokonomix key into the env the existing consumers read (the
        # onboarding gate + council both probe TOKONOMIX_API_KEY). Only when not already set; the
        # resolved value is registered for redaction. A failure degrades (no env set), never crashes.
        tk_ref = (self.config.get("integrations", {}).get("tokonomix", {}) or {}).get("token_ref")
        if tk_ref and not os.environ.get("TOKONOMIX_API_KEY"):
            from .keysource import resolve_ref
            r = resolve_ref(tk_ref, config=self.config)
            if r.value:
                os.environ["TOKONOMIX_API_KEY"] = r.value
            # Not `elif`: a SUCCESSFUL resolution can still carry a blind spot (e.g. L5's
            # non-loopback/non-https vault endpoint warning) — surface it either way.
            if r.blind_spot:
                self.key_blind_spots.append(r.blind_spot)

        # Fallback: if TOKONOMIX_API_KEY is still unset (token_ref null or unresolved), read it
        # from the well-known credentials file so the council fires in launcher-env contexts where
        # only the file is guaranteed to exist (e.g. `claude -p` without the key in shell env).
        _register_creds_file_key()

        # Ticket source: Paperclip (optional, site-specific) overrides the local .md dir when
        # configured. The adapter is quarantined to this entrypoint; the core never sees Paperclip.
        self.paperclip = None
        self.pcp_id_by_ticket = {}
        self.tickets = _load_paperclip_tickets_for(self)
        if self.tickets is None:
            self.tickets_dir = os.path.join(self.repo, args.tickets)
            self.tickets = load_tickets(self.tickets_dir) if os.path.isdir(self.tickets_dir) else []
        # c5994abe: decouple Paperclip WRITE from SOURCE — status-sync even for a curated
        # tickets-dir run, when write_enabled + tickets carry a `paperclip_id`.
        _init_paperclip_write_for(self)
        self.store = OutcomeStore(self.state_dir)

        gate = _primary_gate(self.config, self.repo)
        if gate is None:
            # A gate that always passes would be dishonest; use a trivially-green no-op and let the
            # report flag low confidence. (A real project configures a gate via the wizard.)
            gate = GateRunner(command=["true"] if os.name != "nt" else ["cmd", "/c", "exit 0"],
                              cwd=self.repo, timeout=30)
            # INT-1675 #4: mark the no-op so the orchestrator records DONE_LOW_CONFIDENCE
            # with an honest "no gate configured — unverified" reason instead of "gates green".
            gate.noop = True
        self.gate = gate

        self.ledger = AttemptLedger(os.path.join(self.state_dir, "ledger.json"))
        self.heartbeat = Heartbeat(os.environ.get("UE_HEARTBEAT")
                                   or os.path.join(self.state_dir, "heartbeat.json"))
        self.report_path = _resolve_report_path(self.repo, self.config, args.report)

        # Protect the harness's own dirs from being committed/reverted by the reversibility layer.
        protect = {".unattended"}
        for d in (self.state_dir, self.artifacts_dir):
            rel = os.path.relpath(d, self.repo)
            if not rel.startswith(".."):
                protect.add(rel.split(os.sep)[0])
        # worker=None: the CLI next/complete flow drives the orchestrator via StepDriver,
        # which never calls Orchestrator.run() (the agent IS the worker). Orchestrator.run()
        # with a real Worker remains the in-process reference loop used by the acceptance demo.
        self.orch = Orchestrator(
            repo_dir=self.repo, store=self.store, gate=self.gate, worker=None,
            artifacts_dir=self.artifacts_dir, unattended=self.unattended,
            ledger=self.ledger, heartbeat=self.heartbeat,
            fix_cap=self.config.get("budget", {}).get("per_ticket_fix_iterations", 3),
            protect_paths=sorted(protect),
            # Operator-trusted per-ticket classification overrides (INT-1825 bug 1).
            classify_overrides=(self.config.get("classify", {}) or {}).get("overrides", {}) or {},
            # Plan 2 §1: project opt-in set of hard-PARK categories eligible for F5 (validated above).
            consensus_assisted_categories=(
                (self.config.get("classify", {}) or {}).get("consensus_assisted_categories", []) or []),
            # Q&A item 14: operator opt-in to reuse a green complete as the next baseline.
            gate_baseline_reuse=bool(self.config.get("gate_baseline_reuse", False)),
        )
        # Parked-WIP guard (INT-1735): None unless autonomy.parked.enabled. Protects intentional
        # working-tree WIP from the `git add -A` snapshot; driven at run-begin / terminal below.
        from .parked import guard_from_config
        self.parked = guard_from_config(self.config, self.repo, self.state_dir)

        self.non_destructive = bool(
            self.config.get("autonomy", {}).get("non_destructive_only"))
        self.driver = StepDriver(
            orch=self.orch, tickets=self.tickets, store=self.store,
            state_dir=self.state_dir, report_path=self.report_path,
            non_destructive=self.non_destructive, config=self.config,
            key_blind_spots=self.key_blind_spots)


def _resolve_paperclip_token(pc_cfg: dict, config: dict):
    """Resolve the Paperclip token from the configured `token_ref` (`env:VAR` or `vault:path`),
    defaulting to `env:PAPERCLIP_TOKEN` for back-compat. Returns a keysource.Resolved (value +
    a blind_spot when a configured source couldn't be read). The value is registered for redaction."""
    from .keysource import resolve_ref
    ref = pc_cfg.get("token_ref") or "env:PAPERCLIP_TOKEN"
    return resolve_ref(ref, config=config)


def _emit(obj: dict, code: int = 0) -> int:
    from .redact import redact_obj
    print(json.dumps(redact_obj(obj), indent=2))
    return code


def _error_code(out) -> int:
    """Exit code for a driver result: `status: ERROR` exits 2, matching cmd_next's sentinel
    hard-fail — one convention for every subcommand, so a scripting agent can trust the exit
    code as well as the JSON (2026-07-08 E2E: resolve-park's attempt-id-mismatch ERROR exited 0)."""
    return 2 if isinstance(out, dict) and out.get("status") == "ERROR" else 0


def _note_unsafe_paperclip_endpoint(ctx, pc_cfg: dict) -> None:
    """L5 (post-audit): a non-loopback, non-https base_url puts the bearer token on the wire in
    cleartext on every request. Not a hard-fail (the shipped default is loopback, so this is silent
    out of the box) — a blind spot, same treatment as an unresolved token."""
    from .keysource import is_safe_endpoint
    base_url = pc_cfg.get("base_url") or ""
    if base_url and not is_safe_endpoint(base_url):
        ctx.key_blind_spots.append(
            f"integrations.paperclip.base_url {base_url!r} is neither loopback nor https — the "
            "Paperclip bearer token travels over the network in cleartext on every request")


def _load_paperclip_tickets_for(ctx) -> list | None:
    """If Paperclip is configured, pull open issues as tickets; else None (fall back to local .md)."""
    pc_cfg = (ctx.config.get("integrations", {}).get("paperclip", {}) or {})
    if not (pc_cfg.get("enabled") and pc_cfg.get("project_id") and pc_cfg.get("company_id")):
        return None
    resolved = _resolve_paperclip_token(pc_cfg, ctx.config)
    if not resolved.value:
        reason = resolved.blind_spot or "no token (env PAPERCLIP_TOKEN unset)"
        print(f"[agents-never-sleep] Paperclip enabled but token unresolved: {reason} — "
              "falling back to local tickets.", file=sys.stderr)
        return None
    _note_unsafe_paperclip_endpoint(ctx, pc_cfg)
    from .sources.paperclip import PaperclipClient, to_ticket
    ctx.paperclip = PaperclipClient(pc_cfg["base_url"], resolved.value, pc_cfg["company_id"],
                                    write_enabled=bool(pc_cfg.get("write_enabled")))
    try:
        issues = ctx.paperclip.list_open_issues(pc_cfg["project_id"])
    except Exception as exc:  # noqa: BLE001 - a source-fetch failure must not crash; degrade
        print(f"[agents-never-sleep] Paperclip fetch failed ({exc}); falling back to local "
              "tickets.", file=sys.stderr)
        ctx.paperclip = None
        return None
    tickets = [to_ticket(i) for i in issues]
    ctx.pcp_id_by_ticket = {t.id: t.meta.get("paperclip_id") for t in tickets}
    return tickets


def _init_paperclip_write_for(ctx) -> None:
    """Decouple Paperclip WRITE from SOURCE (c5994abe). When the tickets came from the local
    dir (Paperclip is NOT the source) but the config opts into `write_enabled` AND tickets carry
    a `paperclip_id`, build a write client + the id map so per-ticket status still syncs to the
    board. Reads/source stay independent; a missing token or no ids → silent degrade (no crash)."""
    if ctx.paperclip is not None:            # source already built the client — nothing to do
        return
    pc_cfg = (ctx.config.get("integrations", {}).get("paperclip", {}) or {})
    if not (pc_cfg.get("write_enabled") and pc_cfg.get("company_id") and pc_cfg.get("base_url")):
        return
    id_map = {t.id: t.meta.get("paperclip_id")
              for t in (ctx.tickets or []) if t.meta.get("paperclip_id")}
    if not id_map:
        return
    resolved = _resolve_paperclip_token(pc_cfg, ctx.config)
    if not resolved.value:
        print(f"[agents-never-sleep] Paperclip write_enabled but token unresolved "
              f"({resolved.blind_spot or 'no token'}) — per-ticket status-sync degraded to a "
              "blind spot (work still completes).", file=sys.stderr)
        return
    _note_unsafe_paperclip_endpoint(ctx, pc_cfg)
    from .sources.paperclip import PaperclipClient
    ctx.paperclip = PaperclipClient(pc_cfg["base_url"], resolved.value, pc_cfg["company_id"],
                                    write_enabled=bool(pc_cfg.get("write_enabled")))
    ctx.pcp_id_by_ticket = id_map


def _push_in_progress_one(ctx, ticket_id: str) -> None:
    """On hand-out (c5994abe): mark the ticket in_progress on the board, ONCE, before the agent
    works it — so a run checks issues off AS IT GOES, not batched at the end. Idempotent via the
    same paperclip-pushed.json marker (skip if any state was already pushed for this ticket, so a
    resume never regresses a terminal state back to in_progress). Never blocks the run."""
    if not ctx.paperclip or not ctx.pcp_id_by_ticket:
        return
    pid = ctx.pcp_id_by_ticket.get(ticket_id)
    if not pid:
        return
    marker_path = os.path.join(ctx.state_dir, "paperclip-pushed.json")
    pushed_state = {}
    if os.path.exists(marker_path):
        try:
            with open(marker_path, "r", encoding="utf-8") as fh:
                pushed_state = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pushed_state = {}
    if pushed_state.get(ticket_id):          # already pushed in_progress or a terminal state
        return
    try:
        action = ctx.paperclip.set_status(pid, "in_progress")
    except Exception as exc:  # noqa: BLE001 - status-sync must never crash the run
        print(f"[agents-never-sleep] Paperclip in_progress push failed for {ticket_id}: {exc}",
              file=sys.stderr)
        return
    if not action.dry_run:                   # only remember a state we actually wrote
        pushed_state[ticket_id] = "in_progress"
        try:
            with open(marker_path, "w", encoding="utf-8") as fh:
                json.dump(pushed_state, fh, indent=2, sort_keys=True)
        except OSError:
            pass


def _sentinel_path_ok(ctx: "_Context") -> bool:
    """The Stop-hook checks ${UE_RUN_INCOMPLETE:-$PWD/.unattended/run-incomplete}; the driver writes
    the same env-or-repo path. They agree automatically only when UE_RUN_INCOMPLETE is set OR the
    agent's CWD equals --repo. Otherwise never-stop silently breaks — so this is a HARD failure in
    unattended mode, not a warning.

    Compared by directory IDENTITY (realpath), not spelling: getcwd() is always the physical
    path, while --repo may arrive through a symlink — the macOS default, where TMPDIR lives
    under /var/folders, a symlink to /private/var/folders. A string compare would hard-fail a
    run whose CWD *is* the repo. (The Stop-hook's $PWD is physical too, so when this check
    passes the hook and driver agree on the same real file.) Path CONSTRUCTION elsewhere stays
    abspath on purpose — see cmd_note — only this identity check resolves symlinks."""
    if not ctx.unattended:
        return True
    if os.environ.get("UE_RUN_INCOMPLETE"):
        return True
    return os.path.realpath(os.getcwd()) == os.path.realpath(ctx.repo)


_TERMINAL = {"DRAINED", "HALTED", "LOW_YIELD", "STOPPED_CREDITS"}


def _push_paperclip(ctx) -> dict | None:
    """At a terminal signal, push each outcome back to its Paperclip issue (status + comment).

    IDEMPOTENT: comments are append-only, and this runs on every terminal `next` and on `report`, so
    a marker file records the last state pushed per ticket and we skip a ticket whose state is
    unchanged — otherwise a resume would re-spam every previously-completed issue. The marker is only
    advanced on a REAL write, so a dry-run preview never suppresses the eventual live push."""
    if not ctx.paperclip or not ctx.pcp_id_by_ticket:
        return None
    from .sources.paperclip import push_outcome
    marker_path = os.path.join(ctx.state_dir, "paperclip-pushed.json")
    pushed_state = {}
    if os.path.exists(marker_path):
        try:
            with open(marker_path, "r", encoding="utf-8") as fh:
                pushed_state = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pushed_state = {}

    actions, dry, skipped = 0, False, 0
    for outcome in ctx.store.all():
        pid = ctx.pcp_id_by_ticket.get(outcome.ticket_id)
        if not pid:
            continue
        if pushed_state.get(outcome.ticket_id) == outcome.state.value:
            skipped += 1
            continue  # already pushed this exact state — don't duplicate
        try:
            results = push_outcome(ctx.paperclip, pid, outcome)
        except Exception as exc:  # noqa: BLE001 - reporting back must never crash the wrap-up
            print(f"[agents-never-sleep] Paperclip push failed for {outcome.ticket_id}: {exc}",
                  file=sys.stderr)
            continue
        actions += len(results)
        is_dry = any(a.dry_run for a in results)
        dry = dry or is_dry
        if not is_dry:                       # only remember a state we actually wrote
            pushed_state[outcome.ticket_id] = outcome.state.value

    if pushed_state:
        try:
            with open(marker_path, "w", encoding="utf-8") as fh:
                json.dump(pushed_state, fh, indent=2, sort_keys=True)
        except OSError:
            pass
    return {"actions": actions, "skipped_already_pushed": skipped, "dry_run": dry,
            "note": ("DRY-RUN (set integrations.paperclip.write_enabled=true to apply)"
                     if dry else "applied to the board")}


def cmd_next(args) -> int:
    ctx = _Context(args)
    if not _sentinel_path_ok(ctx):
        return _emit({"status": "ERROR",
                      "error": ("unattended run with CWD != --repo and UE_RUN_INCOMPLETE unset: the "
                                "Stop-hook would not find the run-incomplete sentinel, silently "
                                "disabling never-stop. Run from the repo root (cd into --repo) OR "
                                f"export UE_RUN_INCOMPLETE={os.path.join(ctx.repo, '.unattended', 'run-incomplete')} "
                                "before launching.")}, code=2)
    if ctx.config.get("autonomy", {}).get("non_destructive_only") and ctx.unattended and \
            load_config(ctx.repo) is None:
        # Loud, machine-visible note: a real unattended run with no saved config is non-destructive.
        return _emit({"status": "NON_DESTRUCTIVE",
                      "message": "unattended + no saved config — run interactively once to "
                                 "configure before file-writing autonomy is allowed."})
    # Protect parked WIP before any snapshot/edit. Idempotent + resume-safe: a no-op after the
    # first `next` of the run, so calling it on every `next` is correct.
    if ctx.parked is not None:
        ctx.parked.protect()
    balance_eur = getattr(args, "balance_eur", None)
    result = ctx.driver.next_ticket(balance_eur=balance_eur)
    if result.get("status") in _TERMINAL:               # run finished -> report back to the board
        if ctx.parked is not None:                      # restore parked WIP after a terminal signal
            result["parked_restore"] = ctx.parked.restore()
        summary = _push_paperclip(ctx)
        if summary is not None:
            result["paperclip"] = summary
    elif result.get("status") == "PROCEED":             # c5994abe: mark it in_progress as we go
        _push_in_progress_one(ctx, (result.get("ticket") or {}).get("id") or "")
    # INT-1675 P2: echo the resolved absolute repo/tickets paths so a stray `cd` + `--repo .`
    # mis-target (empty backlog -> spurious DRAINED in the wrong dir) is visible at a glance.
    result.setdefault("repo_abs", ctx.repo)
    result.setdefault("tickets_abs", getattr(ctx, "tickets_dir", None))
    return _emit(result)


def cmd_complete(args) -> int:
    ctx = _Context(args)
    concerns = [s for s in (args.specialist_concerns or "").split(",") if s.strip()]
    http_status = getattr(args, "council_http_status", None)
    # ticket 03: optional machine-readable gateway verdict (x_council.verdict). Parsed
    # tolerantly — a bad/absent JSON is ignored (falls back to the --council-verdict
    # self-report), never crashes `complete`.
    structured = None
    raw_json = getattr(args, "council_verdict_json", None)
    if raw_json:
        try:
            import json as _json
            parsed = _json.loads(raw_json)
            if isinstance(parsed, dict):
                structured = parsed
        except (ValueError, TypeError):
            structured = None
    out = ctx.driver.complete_ticket(
        attempted=args.attempted, cannot_implement=args.cannot_implement,
        review_coverage=args.review_coverage, council_verdict=args.council_verdict,
        council_verdict_structured=structured,
        council_cost_eur=args.council_cost, council_http_status=http_status,
        specialist_concerns=concerns, specialist_cost_eur=args.specialist_cost)
    # INT-1675 P2: surface the resolved source so a `complete` run against the wrong --tickets dir
    # (the silent-revert foot-gun, now a refuse) is diagnosable from the result alone.
    if isinstance(out, dict):
        out.setdefault("repo_abs", ctx.repo)
        out.setdefault("tickets_abs", getattr(ctx, "tickets_dir", None))
    # c5994abe: push THIS ticket's terminal status (done/blocked) + outcome comment to the board
    # now, not batched at run-end — so issues are checked off as the run goes. Idempotent + degrades.
    pcp = _push_paperclip(ctx)
    if isinstance(out, dict) and pcp is not None:
        out["paperclip"] = pcp
    return _emit(out, code=_error_code(out))


def cmd_resolve_park(args) -> int:
    """F5 callback (Plan 1, requirement_meaning only): the agent reports the structured grounded-
    consensus verdict for a ticket `next` previously offered as PARK_CONSENSUS_ELIGIBLE. Symmetric
    to `complete`'s --council-verdict* flags, but a NEW verb — PARK never reaches `complete`."""
    ctx = _Context(args)
    from .f5 import F5Verdict
    verdict = F5Verdict(resolved=args.resolved, chosen_reading=args.chosen_reading or "",
                        evidence=args.evidence or "", dissent_count=args.dissent_count,
                        synthesis_text=args.synthesis_text or "",
                        defect_found=getattr(args, "defect_found", False))
    out = ctx.driver.resolve_park(args.ticket_id, args.attempt_id, verdict)
    if isinstance(out, dict):
        out.setdefault("repo_abs", ctx.repo)
        out.setdefault("tickets_abs", getattr(ctx, "tickets_dir", None))
    return _emit(out, code=_error_code(out))


def cmd_reset_attempts(args) -> int:
    # INT-1675 P3: first-class operator escape for the documented "kill+resume / tooling round-trip
    # inflated the attempt counter -> healthy ticket force-parked at the cap" case. Replaces the rough
    # "hand-edit ledger.json" workaround.
    ctx = _Context(args)
    prior = ctx.ledger.reset_attempts(args.ticket_id)
    return _emit({"status": "ATTEMPTS_RESET", "ticket_id": args.ticket_id,
                  "cleared_attempts": prior,
                  "note": ("attempt counter cleared" if prior else
                           "no attempts were recorded for this ticket")})


def cmd_reset_spend(args) -> int:
    # INT-1675 P4 follow-on: operator escape to zero the per-night spend accounting when a resume /
    # abnormal exit left the €-cap or council-call cap accounting wrong. Symmetric to reset-attempts.
    ctx = _Context(args)
    prior = ctx.driver.reset_spend()
    return _emit({"status": "SPEND_RESET",
                  "cleared_council_cost_eur": prior["council_cost_eur"],
                  "cleared_council_calls": prior["council_calls"],
                  "note": "per-night spend accounting zeroed; breaker counters (processed/bad) untouched"})


def _agent_hint_kwargs(ctx) -> dict:
    """F2-declarative inputs for the report: the run's active agent (launcher.default_agent — the
    best signal the harness has for 'what we're running as') and a ticket_id -> declared `agent:`
    map for tickets that carried the hint. Paperclip-sourced tickets simply have no hint (None)."""
    active = (ctx.config.get("launcher", {}) or {}).get("default_agent")
    hints = {t.id: t.declared_agent for t in ctx.tickets
             if getattr(t, "declared_agent", None)}
    return {"active_agent": active, "agent_hints": hints}


def cmd_report(args) -> int:
    ctx = _Context(args)
    from .vcs import Git, GitError
    try:
        backup_refs = Git(ctx.repo).list_backup_refs()
    except GitError:
        backup_refs = []
    report = build_report(ctx.store.all(), run_label="unattended run",
                          backup_refs=backup_refs, **_agent_hint_kwargs(ctx))
    with open(ctx.report_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    out = {"status": "REPORT_WRITTEN", "report_path": ctx.report_path}
    summary = _push_paperclip(ctx)
    if summary is not None:
        out["paperclip"] = summary
    return _emit(out)


def cmd_parked(args) -> int:
    """Manually protect/restore parked WIP (INT-1735) — the same helper next/report drive, exposed
    standalone so an operator can restore after a run that never reached a terminal signal."""
    ctx = _Context(args)
    if ctx.parked is None:
        return _emit({"status": "DISABLED",
                      "message": "autonomy.parked.enabled is false — nothing to protect/restore"})
    result = ctx.parked.protect() if args.action == "protect" else ctx.parked.restore()
    return _emit({"status": "OK", "action": args.action, "result": result})


def cmd_note(args) -> int:
    """Ticket 04: append a revert-surviving progress note for a ticket to
    <state-dir>/<ticket>.notes.md. The file lives under .unattended/ (gitignored + in the git
    protect set), so it survives a gate-fail/crash revert while the CODE rolls back to green; it
    is re-injected into the next PROCEED payload when autonomy.scratchpad.enabled is on, so a
    resumed agent CONTINUES its reasoning instead of re-deriving. Deliberately does NOT build a
    full run Context — a note write is a cheap, side-effect-free append the agent may call any
    time mid-ticket. Note text is redacted (same scrubber as the outcome store)."""
    from . import scratchpad
    from .redact import register_env_secrets
    # Populate the redaction registry exactly as _Context does — cmd_note skips _Context, but the
    # append below MUST still scrub a pattern-less env/creds key (else it lands verbatim on disk).
    register_env_secrets()
    _register_creds_file_key()
    # abspath (NOT realpath) to match _Context.state_dir, or a symlinked repo path would write the
    # note where _attach_scratchpad_hint/read_notes won't look → silent non-re-injection.
    state_dir = os.path.join(os.path.abspath(args.repo), args.state_dir)
    path = scratchpad.append_note(state_dir, args.ticket, args.text)
    return _emit({"status": "OK", "ticket": args.ticket, "notes_path": path})


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".", help="project working directory")
    p.add_argument("--tickets", default="tickets", help="dir of .md tickets")
    p.add_argument("--state-dir", default=".unattended/state")
    p.add_argument("--artifacts-dir", default=".unattended/artifacts")
    p.add_argument("--report", default="night-report.md")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Exposed (not inlined in main) so the surface-drift test can
    introspect the frozen Stable subcommand set without shelling out to --help."""
    ap = argparse.ArgumentParser(prog="agents_never_sleep.run")
    sub = ap.add_subparsers(dest="cmd")

    pn = sub.add_parser("next", help="get the next PROCEED ticket (or a terminal signal)")
    _add_common(pn)
    pn.add_argument("--balance-eur", type=float, default=None, dest="balance_eur",
                    help="current Tokonomix balance in € (from tokonomix_get_balance); "
                         "feeds the credits preflight and per-council budget gate")
    pn.set_defaults(func=cmd_next)

    pc = sub.add_parser("complete", help="record the ticket the agent just implemented")
    _add_common(pc)
    pc.add_argument("--attempted", default="", help="agent's short self-report of what it did")
    pc.add_argument("--cannot-implement", action="store_true",
                    help="the agent could not implement the ticket (records BLOCKED_ENV)")
    pc.add_argument("--review-coverage", default=None,
                    help="coverage tag: which proposers/councils ran (incl errors) + cost")
    pc.add_argument("--council-verdict", default=None, choices=["pass", "concerns", "error"],
                    help="council outcome on this change (omit if no council was run)")
    pc.add_argument("--council-verdict-json", default=None,
                    help="the gateway's machine-readable x_council.verdict JSON "
                         "({overall,issues[]}); honored DOWNGRADE-ONLY when "
                         "config.council.structured_verdict is on (ticket 03)")
    pc.add_argument("--council-cost", type=float, default=0.0,
                    help="real € charged for this ticket's council (feeds the per-night cost brake)")
    pc.add_argument("--specialist-concerns", default=None,
                    help="comma-separated specialist lenses that raised a material concern "
                         "(architect/security/tenant-safety force a daylight review)")
    pc.add_argument("--specialist-cost", type=float, default=0.0,
                    help="real € charged for this ticket's specialist reviews (feeds the spend brake)")
    pc.add_argument("--council-http-status", type=int, default=None, dest="council_http_status",
                    help="HTTP status code returned by the council gateway (402 = insufficient_balance "
                         "→ triggers the configured credits-exhaustion policy)")
    pc.set_defaults(func=cmd_complete)

    presolve = sub.add_parser("resolve-park",
                              help="report the agent's F5 grounded-consensus verdict for a "
                                   "PARK_CONSENSUS_ELIGIBLE ticket")
    _add_common(presolve)
    presolve.add_argument("--ticket-id", required=True, dest="ticket_id",
                          help="the ticket id `next` offered as PARK_CONSENSUS_ELIGIBLE")
    presolve.add_argument("--attempt-id", required=True, dest="attempt_id",
                          help="the attempt_id from the PARK_CONSENSUS_ELIGIBLE offer payload — "
                               "must match the outstanding ledger offer or the call is refused")
    resolved_grp = presolve.add_mutually_exclusive_group(required=True)
    resolved_grp.add_argument("--resolved", dest="resolved", action="store_true",
                              help="the grounded consensus reached a single-reading resolution")
    resolved_grp.add_argument("--not-resolved", dest="resolved", action="store_false",
                              help="the consensus did not resolve the ambiguity — stays PARK")
    presolve.add_argument("--chosen-reading", default="", dest="chosen_reading",
                          help="the reading the consensus picked (required for RESOLVE to succeed)")
    presolve.add_argument("--evidence", default="",
                          help="exact evidence cited from the repo/spec context")
    presolve.add_argument("--dissent-count", type=int, default=0, dest="dissent_count",
                          help="number of proposers disagreeing with the chosen reading")
    presolve.add_argument("--synthesis-text", default="", dest="synthesis_text",
                          help="the judge synthesis text (concern-language backstop)")
    presolve.add_argument("--defect-found", dest="defect_found", action="store_true",
                          help="(hard-category soundness path) the consensus found a concrete "
                               "defect — a deterministic veto that keeps the ticket parked")
    presolve.set_defaults(func=cmd_resolve_park)

    pr = sub.add_parser("report", help="(re)write the morning report from the store")
    _add_common(pr)
    pr.set_defaults(func=cmd_report)

    pra = sub.add_parser("reset-attempts",
                         help="clear a ticket's attempt counter (operator escape for cap inflation)")
    _add_common(pra)
    pra.add_argument("ticket_id", help="ticket id whose attempt counter to clear")
    pra.set_defaults(func=cmd_reset_attempts)

    prs = sub.add_parser("reset-spend",
                         help="zero the per-night council spend accounting (operator escape)")
    _add_common(prs)
    prs.set_defaults(func=cmd_reset_spend)

    pp = sub.add_parser("parked", help="protect/restore parked WIP (INT-1735)")
    _add_common(pp)
    pp.add_argument("action", choices=["protect", "restore"],
                    help="protect = stash parked WIP before a run; restore = bring it back after")
    pp.set_defaults(func=cmd_parked)

    pnote = sub.add_parser("note",
                           help="append a revert-surviving progress note for a ticket (ticket 04)")
    _add_common(pnote)
    pnote.add_argument("--ticket", required=True, help="ticket id the note belongs to")
    pnote.add_argument("--text", required=True,
                       help="the note text (progress/decisions); survives a gate-fail revert")
    pnote.set_defaults(func=cmd_note)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except RunResumeUnsafe as exc:
        # Loud HALT: a stale/stranger run branch cannot be safely resumed. Emit a structured signal
        # and exit non-zero. run-branch.json is left intact (we raised before any checkout) so the
        # operator can inspect; every re-invocation HALTs identically until they act.
        # Also drop a durable `resume-halt` marker so the fresh-session launcher STOPS instead of
        # respawning a fresh agent that would only HALT again (up to its cap). The driver clears the
        # marker once a fresh/safe run branch is (re)entered. Best-effort; a write failure just
        # falls back to the (bounded) respawn-cap behavior.
        try:
            repo = os.path.abspath(getattr(args, "repo", ".") or ".")
            sd = os.path.join(repo, getattr(args, "state_dir", None) or ".unattended/state")
            ensure_private_dir(sd)
            with open(os.path.join(sd, "resume-halt"), "w", encoding="utf-8") as fh:
                fh.write(str(exc) + "\n")
        except OSError:
            pass
        return _emit({"status": "HALT_RESUME_UNSAFE", "error": str(exc)}, code=3)


if __name__ == "__main__":
    raise SystemExit(main())
