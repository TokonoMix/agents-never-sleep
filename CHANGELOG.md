# Changelog — agents-never-sleep

All notable changes to the public API surface (see `ARCHITECTURE.md`) are documented here.
The versioning **policy** (Stable vs Experimental classification + the v1.0 roadmap) lives in
`SEMVER.md`.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [1.7.0] — 2026-07-15

Onboarding hardening: close the "init said ready, but enforcement isn't wired" gap and cut
re-trust friction. Derived from a cross-model consensus on an external reviewer's tips, verified
against source. Additive/behaviour-refining (no API, schema, or entry-point change), so MINOR —
**with one one-time migration** (see below).

### Added
- **`ans-run install-hooks` now actually wires the ANS deny-hooks** into the host settings file
  (Claude Code `~/.claude/settings.json`) with a unified diff + one confirmation (was an honest
  stub). Atomic write; idempotent; never clobbers a pre-existing `permissions` block or unrelated
  hooks; still requires an explicit `--harness` (no default), rejects unknown harnesses, and accepts
  enforcement-only targets like `cursor` (handoff). New `hooks_wired()` detector. On a pip/wheel
  install the hook scripts are not packaged, so it degrades **gracefully** with an actionable message
  (use the source checkout / installed skill) instead of crashing.
- **Non-blocking preflight NOTE:** the launcher now tells you at start when the selected harness's
  enforcement hooks are not wired (or reference a missing script — catches a moved/deleted install),
  and names the remedy (`ans-run install-hooks --harness <h>`). It is a **NOTE, never a NO-GO** — the
  run still proceeds on the prose-contract; the trusted-operator model is unchanged.
- **`ans-run init` can pre-authorize deny-list consent classes** during interactive onboarding
  (per-class, via a helper shared with the wizard — anti-drift). `--yes` writes **no** consent
  (skipping prompts never authorizes execution). `send_email` is called out as a prompt-injection
  exfiltration surface with no per-run gate once pre-authorized.

### Changed
- **TOFU trust digest now hashes canonical JSON** (sorted object keys, normalized separators) instead
  of raw config bytes, so a cosmetic re-save (whitespace / key reordering) no longer forces a
  re-`--trust`. Weakens nothing: any semantic difference (a changed command, autonomy flag, gate name,
  agent preset, array order — arrays stay ordered) still changes the digest; invalid JSON falls back to
  the raw-bytes hash. Field-level narrowing to a security-fields allowlist is deliberately deferred
  (it carries a maintenance-drift risk).

### Migration
- The canonical-digest change alters the stored digest of **every previously-trusted config**, so the
  first run after upgrading re-prompts for `--trust` **once** per repo (no auto-migration). Expected
  and harmless.

## [1.6.0] — 2026-07-15

First-touch onboarding: `ans-run init` takes a freshly cloned repo from "just cloned" to "a run
can start" — without ever writing host/global config. Additive and backward compatible (no change
to existing surfaces), so a MINOR bump.

### Added — `ans-run init` (new public surface)
- **`ans-run init`** scaffolds a safe per-project config: detects the installed agent CLI and the
  project's test/quality gates (now including Go `go test` and Rust `cargo test`), prints an honest
  enforcement-maturity notice (live-verified vs per-docs), writes the config via
  `config.default_config`, and drops inert example tickets under `.claude/ans-examples/` (never the
  discovered `tickets/` source, so they never auto-run).
- **TOFU trust policy:** the interactive path records trust in the ANS store only (never a harness
  config); a bare `--yes` does **not** auto-trust (skipping prompts is not authorizing execution),
  while `--yes --trust` is the explicit CI one-shot. `--force` overwrites an existing config.
- **`ans-run install-hooks`** ships as an honest stub: it prints the per-harness handoff and writes
  **nothing** to host/global config; the real installer (diff + confirm, schema-safe nested write)
  remains deferred. Enforcement-only platforms (e.g. `cursor`) are valid `install-hooks` targets
  even though `init` never selects them (ANS enforces on more platforms than it launches).

### Safety
- **Blast-radius invariant:** `init` never writes a harness's global config (`~/.claude`,
  `~/.gemini`, `~/.codex` are byte-unchanged), and `run_init` calls `scaffold_preset(confirmed=False)`
  on every path and never writes consent — so it **cannot** enable unattended autonomy or lower the
  v1.5 deny-list floor by construction. The v1.5 consent/consensus wizard behaviour is preserved
  (regression-guarded by `acceptance/test_init_v15_guard.py`).
- Launch-time-only: `init_cmd.py` is never imported by the re-invoked cycling path.

## [1.5.0] — 2026-07-14

The unattended safety model: a conservative deny-list **floor** plus a run-setup **consent
manifest** that lets a human pre-authorize specific classes for a run. Two controls, two jobs —
the floor blocks the known-irreversible commands by default (a backstop against an honest mistake,
NOT a boundary against evasion — it does not inspect script contents), and consent is the
operator's explicit, run-wide, all-targets escape hatch. See `SECURITY.md`.

### Changed — enforcement behaviour (deny-list floor)
This narrows/removes some live blocks AND adds new ones, so it is a behaviour change to shipped
enforcement, not additive hardening. `enforcement.decide()` gained an optional `consent=` kwarg;
the no-consent path is byte-identical to 1.4.0 (backward compatible).

- **Now DENY/PARK (new):** `redis-cli flush(all|db)` (Redis is as often a session/queue/lock store
  as a cache), `docker volume rm` (a named volume *is* the data), secret-path `vault kv put` /
  `vault write <mount>/` (an overwrite; on KV v1 there is no version history), release-tag pushes
  (`git push … vX.Y.Z / --tags / --follow-tags`), `find … -delete/-exec rm` on root/home,
  `kubectl delete` of stateful/bulk kinds, publishing (`npm/pnpm/yarn/poetry/cargo/gem/flit
  publish`, `gh release create`), infra teardown (`terraform destroy`, `aws s3 rb`, `aws s3 rm
  --recursive`), `systemctl mask`, `crontab -r`, `docker system prune`, `docker compose down -v`,
  power-state changes, recursive `chmod/chown` on root/home.
- **⚠️ CONCESSION 4 — single-message + mass email now DENYs by default** (`sendmail`/`mailx`/`mail
  -s`, `postqueue`/`postsuper`/`postfix flush|reload|stop`, `exim -M`, `sendmail -q`). Failure mode
  it guards: "a test script mails 4,000 customers at 3am." **This can break an unattended run that
  legitimately sends a notification.** The "notify me" need is served instead by the morning report
  and, for a real unattended send, by pre-authorizing `send_email` in the consent manifest at setup.
- **No longer blanket-DENY (removed over-blocks):** blunt `systemctl stop/disable` (replaced by
  `systemctl mask` + prune/compose-down-v), `docker rm <container>` (only `docker volume rm` — the
  data — is on the floor), bare `flushall/flushdb` outside `redis-cli`, `db.X.drop(` outside a
  client context. Non-secret `vault write` (auth config, policies, leases) stays ALLOW.

### Added — run-setup consent manifest (opt-in escape hatch)
- **Interactive wizard "actions" section** pre-authorizes deny-list classes for a run. Consent is
  stored OUT-OF-REPO (`~/.config/agents-never-sleep/consent/…`, TOFU-style) so the unattended agent
  structurally cannot author its own consent, frozen at launch into the `UE_CONSENT` env var, and
  upgrades PARK→ALLOW only for a **single clean shell statement** matching **exactly one** consented
  class (chaining, substitution, redirection, newline, interpreter `-c` wrappers all void it).
- **Honest limits (not oversold):** consent is granted per-class, once, at setup, for the WHOLE run
  across EVERY reachable target — there is no per-invocation or per-target check. Pre-authorize a
  high-blast class (`redis_flush`, `send_email`) only for runs scoped to non-critical environments.
  Structured target-scoping (host/URL/recipient allowlists) is the deferred v2 feature.
- **Consent audit + report provenance:** an out-of-repo append-only trail records every command
  that ran under consent; the morning report lists what was pre-authorized and what rode it.

### Fixed
- **`gate_cache` gitignored-file false-green (t06):** `git status --porcelain` omits ignored files,
  so an agent adding a gitignored `sitecustomize.py`/`.env`/`conftest.py`/`*.pth` kept the tree id
  stable while runtime behaviour changed — a stale green baseline could be reused on the only hard
  gate. The cache key now folds a content digest of execution-relevant ignored files (benign
  ignored files like `.unattended/`, `__pycache__/`, build artifacts do not disable the cache).
- **Never-stop counter reset is now regression-locked (t07):** the cross-platform stop-block cap
  already reset on ticket progress (not only on a drained backlog) via `driver._bump_progress`; that
  invariant was untested and is now covered, so a non-Claude run can't silently stop with a full
  backlog if the reset ever regresses.

## [1.4.0] — 2026-07-12

First release published to PyPI: `pip install agents-never-sleep` now resolves the package
directly, so users no longer need the `git+https://…@tag` form (that form keeps working). No
runtime dependencies; the wheel is pure standard library.

### Added — consensus-grounding routing knobs
- **Additive council config keys** in `default_config()`: `consensus_context_cap_tokens`,
  `consensus_context_route_margin`, and `consensus_context_upload` — routing knobs for the
  consensus-grounding gate. Additive only: no existing key, value, or signature changed.

### Changed — onboarding
- **First-run onboarding gains an explicit confirmation prompt** before it acts, and the
  `report` command is now side-effect-free (it only reads and prints run state).

### Security — public-surface hardening
- **Self-contained redaction gate in CI** (`scripts/redact_lint.sh` + the `surface-parity`
  workflow) fails the build if an unambiguously-internal token reaches a tracked file.
- **Deny-hook enforcement bypasses closed.** The irreversible-command deny matcher missed
  several quoted / brace-expanded / equivalent-spelling forms of already-blocked operations
  (force-push refspecs, `${HOME}`-style brace variables, `vault kv metadata delete`, quoted
  `dd of=`); each is now matched. The deny hook also no longer fails open, and the state
  directory is created with owner-only permissions. Regression tests in
  `acceptance/test_enforcement.py`.

### Added — run-branch/worktree isolation safety

- **Auto-worktree isolation.** An unattended run now isolates itself in a dedicated external git
  worktree instead of operating on the caller's working tree, so a detached run never mutates a
  live shared checkout. The run report and logs are kept on the primary tree.
- **Live-tree detection gate.** The launcher now detects when it would run un-isolated inside a
  live shared working tree and gates on that condition, rather than proceeding silently. The
  harness's own `.unattended/` directory is not counted as human-authored dirt.

### Changed — run-branch resume safety

- **Safer run-branch resume.** Resuming a stale run-branch now HALTs instead of performing a silent
  destructive checkout, and any recoverable work-in-progress from the prior run is surfaced as a
  backup reference so it can be recovered rather than lost.

### Fixed — 2026-07-08 E2E follow-up findings (third pass)
- **A read-only repo with a REAL project no longer tracebacks.** With a committed config and at
  least one ticket — so `next` gets past the early NON_DESTRUCTIVE exit and into the driver —
  two unprotected writes still crashed with a raw `PermissionError` (exit 1):
  `Git._ensure_gitignore`'s bare `open(gi, "a")` (vcs.py) and `_set_sentinel`'s bare
  `os.makedirs` (driver.py; the sentinel is pinned to the Stop-hook's repo-relative path, so the
  state-dir redirect never covered it). Now: a failed `.gitignore` update raises `GitError` and
  `ensure_safety_net` reports **no safety net** (the protection against snapshots sweeping
  harness state cannot be established), so classify HALTs cleanly — `status: HALTED`, exit 0;
  the sentinel write/remove degrade to blind-spot notes (never-stop is inactive either way,
  since the Stop-hook reads a path the harness cannot write). `_resolve_writable_state_dirs`
  additionally probes an ACTUAL write: `makedirs(exist_ok=True)` succeeds on an existing
  read-only dir, so the "ran while writable, then locked" case previously skipped the redirect
  and died on the first state write.
- **Crash-recovery revert is lineage-guarded — the stale-resume HALT's own recovery
  instruction no longer corrupts the fresh run.** After `HALT_RESUME_UNSAFE`, removing
  `run-branch.json` and calling `next` forked a fresh run branch correctly, but the separate
  pending-checkpoint recovery then ran `git reset --hard` to the OLD run's `pending.snapshot`
  with no ancestry check — silently grafting the fresh branch onto the prior run's disconnected
  lineage (exit 0, `PROCEED`); the corruption only surfaced on the NEXT call as a second,
  seemingly unrelated HALT. The recovery now resets to `pending.snapshot` only when it is an
  ancestor of the current checkout; a stale checkpoint is instead discarded — carried-over
  partial edits are set aside via the current-tip revert (recoverable `refs/ans-backup/*` ref),
  a blind-spot note records it, and the ticket is re-scheduled under its attempt cap.
- New regressions: `test_halt_readonly_repo_realistic_project` (fresh + warm, via the installed
  CLI path) in `acceptance/test_hardening.py`;
  `test_recovery_after_stale_halt_does_not_graft_old_lineage` in
  `acceptance/test_run_branch_freshness.py`. Both confirmed RED against the pre-fix sources
  (`git stash`), GREEN after; full suite GREEN on stock py3.9.6 and py3.12.

### Fixed — 2026-07-08 E2E review findings
- **Sentinel path guard compares directory identity, not spelling.** `next`'s unattended
  CWD-vs-`--repo` hard-fail used a string compare (`abspath`), so a `--repo` passed through a
  symlink — the macOS default, where TMPDIR lives under `/var/folders`, a symlink to
  `/private/var/folders` — spuriously exited 2 even when CWD *was* the repo. Now compared via
  `realpath` (identity); path construction elsewhere intentionally stays `abspath`.
- **A terminal signal survives an unwritable repo root.** `driver._terminate` crashed with an
  unhandled `PermissionError` writing the night report in a read-only repo — exactly the
  flagship no-safety-net HALT case — breaking the agent-facing JSON contract. The report write
  now degrades: fallback to the state dir, else `report_path: null` + a `report_error` note;
  the HALTED/DRAINED JSON is always emitted.
- **Leaked-process reaping now works on macOS/BSD.** `reap.py` was `/proc`-only, so on Darwin
  `descendants()` silently returned `[]` (no-op reaper, permanently-red `test_reap`). Where
  `/proc` is absent the same pid→ppid table now comes from one `ps -axo pid=,ppid=` snapshot —
  still pure parent-chain lineage, never a name match.
- **One ERROR exit-code convention.** `complete` and `resolve-park` returned exit 0 with
  `status: "ERROR"` (e.g. an attempt-id mismatch); they now exit 2 like `next`'s sentinel
  hard-fail, so scripts can trust the exit code as well as the JSON.
- **py3.9 really is supported by the test suite.** Four acceptance suites used PEP 604
  annotations evaluated at def time and crashed on import under Python 3.9
  (`requires-python >= 3.9`); they now carry `from __future__ import annotations`.
- **`run_all.sh` no longer runs the pytest-marked integration test.** `test_real_claude.py`
  (real `claude -p`, needs pytest + credentials; out of the hermetic suite by its own header)
  is skipped with a pointer to `pytest -m integration`.
- **Docs: `complete` examples carry `--tickets`.** README/SKILL showed `complete` without
  `--tickets`, which errors whenever the tickets dir is not `<repo>/tickets`. README version
  pointers refreshed 1.0.0 → 1.3.0.

## [1.3.0] — 2026-07-08

### Added — keyless first-run Tokonomix onboarding offer (MINOR, additive)
- On an INTERACTIVE first-run with no Tokonomix credential, the wizard now offers a one-time 3-way
  choice — create a free account (keyless onboard), paste an existing key, or skip (default) — instead
  of silently running with multi-model review OFF. Unattended first-runs are unchanged (never prompted).
- The chosen path actually activates review: Paste flips review on immediately; Create records a
  `pending_onboard` marker that `ensure_config` re-probes on the next launch and enables review once the
  key is present — re-recording TOFU trust so a detached run does not bounce on the changed config bytes.
- The onboarding directive copy now names the beta-gate (`accept_beta_terms`, set only after the human
  confirms https://tokonomix.ai/beta) and that the key auto-lands in `~/.tokonomix/credentials.json`.
- No config-schema break; the harness never calls the gateway or accepts beta terms — the agent + human
  do. `needs_onboarding`/the run-time directive are unchanged (never-nag preserved).

## [1.2.0] — 2026-07-07

### Fixed — SKILL.md install: `description` under the 1024-char limit
- The packaged Agent Skill's `SKILL.md` YAML `description` field had grown to 1030 characters, over
  the 1024-character limit skill installers enforce — the YAML parsed fine, so it looked healthy, but
  installation was rejected for new users. Trimmed to 919 characters with every trigger phrase and the
  mechanism list preserved. No public API-surface change; `name` and the SKILL.md body are unchanged.

### Added — F5 Plan 2: consensus-assisted hard-PARK categories + explicit setup wizard (MINOR, additive)
- A project may now opt individual hard-PARK categories (`db_schema_or_migration`, `api_contract`,
  `security_or_tenant`, `money_or_billing`, `cross_ticket_interface`) into a consensus-assisted
  resolution attempt before parking, via the new `classify.consensus_assisted_categories` config key
  (empty by default — existing projects behave identically). A ticket may override the project
  default with an optional `consensus_assisted: true|false` frontmatter field; an explicit `false`
  disables F5 even for `requirement_meaning`. The set is validated fail-fast at load (an unknown or
  misspelled category, or a `requirement_meaning` entry, is a hard error — a safety toggle must never
  silently no-op).
- `f5.eligible()` gains a `consensus_assisted_categories` parameter and consults it for `category_ok`
  (replacing the old `consensus_resolvable`/`not-foundational` pair — four of five hard categories
  are foundational). `interpret_verdict()` — the evidence/dissent/hedge trust gate — is unchanged for
  every category: the opt-in controls only whether F5 is *tried*, never how strictly it judges.
- For a hard category the consensus asks a grounded **soundness** question (new `build_soundness_prompt`:
  is this already-decided change reversible / correctly scoped / free of data-loss, contract-break, or
  security holes, citing evidence?), never disambiguation and never "should I proceed". A found defect
  is a **deterministic veto**: the new `F5Verdict.defect_found` (agent flag `resolve-park --defect-found`)
  routes through `interpret_soundness_verdict` to `KEEP_PARKED` in the harness, before the shared gate,
  so a confidently-reported "resolved: here is the security hole" can never apply the hole unattended.
  With no defect it delegates to the byte-for-byte-unchanged `interpret_verdict` (evidence + zero-dissent
  + no-hedge). `requirement_meaning` keeps the disambiguation prompt and gate.
- Safety compensation: an F5 resolution of any category other than `requirement_meaning` is forced to
  `DONE_LOW_CONFIDENCE` with a `NEEDS DAYLIGHT REVIEW` note regardless of gate/council result — ANS
  may apply the change unattended (git is the reverse button), but the human always gets the
  after-the-fact look a hard-PARK used to guarantee. Carried by a new `ProceedToken.force_daylight_review`
  that round-trips through the persisted pending token from `resolve-park` to `complete`.
- Anti-TOCTOU: the effective opt-in set is snapshotted onto the durable offer record at offer time;
  `resolve-park` re-checks eligibility against the RECORDED set, never fresh config.
- The first-run wizard now asks explicitly (when a Tokonomix key is present) whether to enable council
  and specialist review, and — per hard-PARK category — whether to allow a consensus-assisted
  resolution (default no), instead of silently defaulting off credential presence. Unattended
  first-runs keep the conservative empty opt-in.
- SKILL.md documents ticket-authoring guidance: ask once per batch, write `consensus_assisted:` only
  when it differs from the project default; ticket prose is never a trusted opt-in channel.
- New acceptance suites `acceptance/test_consensus_scope.py` and `acceptance/test_f5_plan2_wiring.py`;
  `test_f5.py`/`test_config.py`/`test_tickets.py` extended. No change to any Stable CLI verb, outcome
  state, or config key — purely additive.

## [1.1.0] — 2026-07-07

### Added — F5 wiring: grounded-consensus PARK resolution for `requirement_meaning` (MINOR, additive)
- Activates the previously dormant, unit-tested `agents_never_sleep/f5.py` at runtime, narrowly for
  the single eligible category (`requirement_meaning`: FILE-scoped, non-foundational, reversible,
  one-shot per ticket). `next` now returns a new non-terminal status, `PARK_CONSENSUS_ELIGIBLE`,
  instead of parking immediately, when `f5.eligible()` holds; the agent runs a grounded tokonomix
  consensus (never a free-text "should I proceed?") and reports the structured verdict — plus the
  offer's `attempt_id` — via the new `resolve-park --ticket-id --attempt-id --resolved/--not-resolved
  [--chosen-reading] [--evidence] [--dissent-count] [--synthesis-text]` subcommand (Experimental),
  symmetric to `complete`'s `--council-verdict*` flags. `--resolved`/`--not-resolved` is a required
  mutually-exclusive group. Delegates to `StepDriver.resolve_park`. The deterministic
  `f5.interpret_verdict()` gate is the only arbiter: RESOLVE routes into the existing PROCEED path
  (`begin_proceed`); KEEP_PARKED writes a park outcome with a durable audit trail (full verdict +
  category + the fact consensus was attempted) distinguishable from a cold park. A new verb, not a
  `complete` flag — PARK never reaches `complete`.
- Durable, crash-safe, idempotent, attempt-id-keyed bookkeeping: `AttemptLedger.open_f5_offer`
  records an immutable offer (attempt_id + the category/foundational/safety-net snapshot captured AT
  OFFER TIME) OPTIMISTICALLY before the agent runs consensus, so a crash between the offer and
  `resolve-park` (or the agent simply calling `next` again) falls through to a normal park on the
  next scheduling pass, never re-offering the same ticket. `resolve-park` validates the callback's
  `--attempt-id` against this durable record and consumes it on return: a forged, stale, or duplicate
  callback is refused (`ERROR`/`ALREADY_RESOLVED`), and a ticket that already reached a terminal
  outcome is never re-opened.
- A deterministic per-run F5 call ceiling (separate from the council's `€`/call caps — F5 makes its
  own, cheaper single-call tokonomix requests and must never silently borrow the council's budget)
  throttles a PARK-heavy backlog.
- The morning report gets a new visibility line for tickets whose F5 attempt was tried and declined.
- Hard-category PARKs (db-schema/security/money/cross-ticket/foundational) remain structurally
  unreachable by F5 — re-validated defensively even at `resolve-park` time against the RECORDED
  offer (never a fresh re-classification of the ticket text, which would reopen a TOCTOU category-
  drift hole).
- New acceptance suite `acceptance/test_f5_wiring.py` proves the wiring end-to-end (both the RESOLVE
  and KEEP_PARKED branches, the CLI round-trip, resume/crash safety, and the budget ceiling);
  `acceptance/test_f5.py` continues to prove the pure `f5.py` core in isolation.
- SKILL.md documents the agent-facing protocol: how to recognize `PARK_CONSENSUS_ELIGIBLE`, run the
  grounded consensus, and report the verdict via `resolve-park`.
- No change to any existing Stable CLI verb, outcome state, or config key — purely additive.
- Fix: `resolve-park`'s RESOLVE→PROCEED payload now reaches full parity with
  `next_ticket`'s — council/specialist/onboarding/scratchpad hints and credits STOP/DEGRADE gating
  are attached via a new shared `_hand_out_proceed` helper instead of being built inline and omitted.

### Added — leaked-process reaping + opt-in capability restriction (MINOR, additive)
- The watchdog now reaps a run's OWN child tree by PARENT-CHAIN lineage (new
  `agents_never_sleep/reap.py`) — on restart, on a graceful SIGTERM/SIGINT, and via a rolling
  snapshot on a spontaneous agent crash — so leaked MCP servers (context7, tokonomix-mcp, npm/sh)
  no longer accumulate toward OOM (observed ~13 GB, 2026-06-17). NEVER a name-match `pkill`: every
  reap is rooted at the agent's own pid, so it can only reach that subtree (verified against a live
  other-user run), and `reap_pids` refuses pid <= 1. A SIGKILL'd watchdog cannot self-reap — that
  residual leak is reduced, not eliminated.
- Opt-in per-RUN capability restriction: a preset's `capabilities` list (e.g.
  `["--strict-mcp-config", "--mcp-config", "…"]`) is appended to the agent argv to shrink the loaded
  MCP/tool set; absent = today's full set (no-op). Per-RUN, not per-ticket (ANS launches once).

### Added — revert-surviving per-ticket scratchpad + do-not-repeat digest (MINOR, additive)
- New `note --ticket --text` subcommand (Experimental) appends a redacted, timestamped progress
  note to `<state-dir>/<ticket>.notes.md`. The file lives under `.unattended/` (gitignored + in
  the git protect set), so it **survives a gate-fail/crash revert** while the code correctly rolls
  back to green. Gated by `autonomy.scratchpad.enabled` (default off): when on, the ticket's notes
  and a compact `do_not_repeat` digest of dead ends tried this run are re-injected into the PROCEED
  payload so a resumed/fresh agent CONTINUES its reasoning instead of re-deriving. With the flag
  off the PROCEED payload is byte-identical. New module `agents_never_sleep/scratchpad.py`.

### Added — four new enforcement platforms (MINOR, additive)
- **Hermes** (a self-hosted in-process agent orchestrator) — ANS's first **in-process** adapter. A native plugin
  (`hooks/platforms/hermes/`, logic in `agents_never_sleep.hermes_plugin.ans_pre_tool`) registers
  on Hermes's `pre_tool_call` hook and calls the shared `decide()` core directly. Matrix:
  **deny-irreversible NATIVE / never-stop soft-enforced / never-ASK NATIVE**. Denying the `clarify`
  tool preempts Hermes's fail-open clarify-timeout (invented consent) → explicit PARK. Opt-in
  (`plugins.enabled`) + env-gated (`UE_UNATTENDED=1`); not live-verified (maintainer smoke-test).
- **Aider** (0.86.2) — ANS's first **wrapper** adapter (no hook API). Hardened launch preset
  (`agents_never_sleep.aider_launcher.build_aider_argv`) + git-reversibility + prose. All three
  guarantees soft-enforced; the first platform where **deny-irreversible is not native** (breaks the
  old "deny works everywhere" invariant). never-stop/never-ASK are soft-but-structurally-strong.
- **Crush** (charmbracelet) — dispatcher platform. `PreToolUse` shell hook in `crush.json` runs
  before the permission check (beats `crush run --yolo`), forwards the stdin payload to the shared
  dispatcher (`hooks/platforms/crush/`). Matrix **deny NATIVE / never-stop soft / never-ASK soft**.
- **opencode** (sst) — dispatcher platform. `tool.execute.before` JS plugin (`hooks/platforms/opencode/`)
  shells to the dispatcher and throws on deny. Matrix **deny NATIVE\* / never-stop soft / never-ASK
  soft**. \*Caveat (recorded in the hook contract): subagent (`task`-tool) calls bypass the hook
  (upstream sst/opencode#5894) — deny covers primary-agent calls only.
- `agents_never_sleep.enforce`: crush + opencode deny via exit 2 + stderr (reusing the default
  `tool_input.command` normalize — no new parser).
- `agents_never_sleep.capabilities`: adapter-shape distinction (`dispatcher`/`in_process`/`wrapper`,
  `adapter_shape()`, `dispatcher_platforms()`); `_ASK_TOOLS` now includes `clarify`.
- Hermetic tests `acceptance/test_enforce_hermes.py` + `acceptance/test_aider_launcher.py`.
- Smoke-tested (2026-06-28): the Hermes deny+never-ASK flow end-to-end through Hermes's real
  `get_pre_tool_call_block_message`; the aider headless preset on real `aider 0.86.2` — which
  found onboarding/network hang paths `stdin=/dev/null` does not close, driving the
  `--no-show-model-warnings` flag + a documented mandatory wall-clock timeout
  (`RECOMMENDED_TIMEOUT_SECONDS`) + key pre-flight for never-stop.
- No change to any Stable API surface — purely additive (new platforms + capability metadata).

## [1.0.0] — 2026-06-27

> First stable release. Distributed as the public GitHub repo
> (`github.com/TokonoMix/agents-never-sleep`, `pip install git+…@v1.0.0`); a PyPI mirror is an
> optional later add. The SemVer guarantee in `SEMVER.md` is binding from this release.

### Stability commitment
- **First SemVer-stable release.** From the 1.0.0 tag, the surfaces classified **Stable** in
  `SEMVER.md` §2 (loop CLI subcommands + core flags, launcher CLI + exit codes, ticket format,
  gate/config schema, the 7 outcome states, `agents_never_sleep.__version__` + the console entry
  points) are guaranteed: a breaking change to any of them requires a MAJOR bump. **Per-platform
  adapter *behaviour* is best-effort** against each platform's recorded hook-contract version
  (SEMVER §D5) — a host changing its hook API is the one failure mode outside ANS's control.
  Machine-guarded by `acceptance/test_surface_drift.py`.

### Changed
- **Package renamed `harness` → `agents_never_sleep`** so the public import path matches the
  distribution name (a generic `harness` is collision-prone once pip-installed). A back-compat
  `harness` shim (emits one `DeprecationWarning`) keeps `import harness`, `from harness.launcher
  import main`, and `python3 -m harness.run` / `-m harness.enforce` working through **all of 1.x**;
  it is **removed in 2.0**. Console entry points + the dynamic-version attr now resolve
  `agents_never_sleep`. **Migration:** swap `harness` → `agents_never_sleep` in your imports/recipes
  at your convenience before 2.0.

### Added
- `py.typed` (PEP 561) so type-checkers consume the package's inline annotations.
- `acceptance/test_surface_drift.py` — guards the Stable surface (subcommands, core flags, the 7
  outcome states) against drift vs `SEMVER.md` §2.
- Per-platform **tested hook-contract version** (`capabilities._HOOK_CONTRACT`, surfaced in the run
  report) + a hook-contract-coverage assertion over all 6 enforcement adapters' negative tests.

### Removed
- The Experimental in-process `run` CLI subcommand (+ its unwired `NullWorker`). Real runs use
  `next`/`complete`; `Orchestrator.run()` stays as the internal demo/reference loop.

### Security
- The persisted outcome store (`.unattended/state/*.json`) is now scrubbed by the same shape-anchored
  redactor as the other outward surfaces, so a credential pasted into an agent's free-text
  (`attempted`/`exact_blocker`) can't linger verbatim on disk.

### Deprecated
- The `harness` import name — use `agents_never_sleep`; the shim is removed in 2.0.

---

_Earlier 1.0-line groundwork (packaging, SemVer draft, docs) — detail:_

### Added (groundwork)
- **Packaging — `pyproject.toml`.** `pip install agents-never-sleep` now works
  in a fresh venv with **zero runtime dependencies** (the harness is pure standard library).
  - Two console scripts: `ans-run` → `harness.launcher:main` (pre-token preflight launcher) and
    `ans` → `harness.run:main` (the `next`/`complete` loop). External users no longer clone the
    repo + hand-set `PYTHONPATH`.
  - Version is read dynamically from `harness/__init__.py` (`[tool.setuptools.dynamic]`) — single
    source of truth, no second place to bump.
  - Verified locally: wheel builds offline (`pip wheel . --no-deps`), installs into a throwaway
    venv, and `ans-run --check` runs from the installed entry point with no checkout on PATH.
  - **PyPI publish is a maintainer action** (not done here): `twine upload` with the project's PyPI token.
- **`SEMVER.md` (DRAFT).** Formal SemVer commitment: Stable-vs-Experimental
  classification of every public surface (loop CLI, launcher CLI, ticket format, gate/config,
  outcome states, import surface) + a deprecation policy + a checkable v1.0 roadmap. Draft only —
  1.0 is not declared; flags the `harness`→`agents_never_sleep` package-rename as a v1.0 blocker.
- **`PIP-INSTALL-PLAN.md`.** Grounded plan for `pip install agents-never-sleep`:
  documents the (verified) install path, the two-track distribution (pip = harness CLI vs the
  Agent Skill = repo/hooks), and the remaining steps (name claim, build+twine, 3.9–3.12 smoke
  matrix, PyPI publish = maintainer).
- **`COUNCIL-SETUP.md`.** Reproducible record of how to make the Tokonomix
  council fire (`cncl>0`): the two gates (`council.enabled` + `integrations.tokonomix.enabled`)
  and the credential resolution order (`token_ref` → `~/.tokonomix/credentials.json`). The
  per-project config that enables it lives in `.claude/agents-never-sleep.json` (gitignored).
- **Default-suite exclusion for integration tests (`pyproject.toml`).** Added
  `[tool.pytest.ini_options] addopts = "-m 'not integration'"` so a plain `pytest` stays fast and
  offline; `pytest -m integration` still selects the live-`claude` test (a CLI `-m` overrides the
  default). Complements the marker already registered in `acceptance/conftest.py`.

### Changed
- **Launcher refactored into `harness/launcher.py` (enables the `ans-run` console entry point).**
  The body of `bin/ans-run` moved into the importable module `harness.launcher` with `main()`;
  `bin/ans-run` is now a thin shim that delegates to it. Behaviour is unchanged — the launcher
  acceptance suite (`test_launcher.py`) stays GREEN. (A console entry point must point at
  `module:callable`, which the old script-only `bin/ans-run` could not provide.)
- **`acceptance/test_real_claude.py`: now VERIFIED GREEN against a live
  `claude -p` session** (it had never actually run before). Four real defects fixed so it passes:
  (1) the child no longer inherits the parent run's `UE_RUN_INCOMPLETE` (which made a nested run's
  Stop-hook guard the *parent* sentinel → a 300s hang) — the parent run-env vars are scrubbed and
  the sentinel is re-pinned to the child's own repo; (2) launches with
  `--dangerously-skip-permissions` (mirroring the real ANS launcher) so the headless child can run
  the harness shell commands; (3) reads the done-count from the durable `OutcomeStore`, not
  `run-progress.json` (which is reset to 0 at the DRAINED terminal by design); (4) resolves the
  persistent `ans/run-*` branch via `git for-each-ref` and verifies `hello.txt` via `git show` +
  counts `done:` commits there (the operator branch is checked back out at the terminal, so the
  work is NOT in the working tree). The in-test gate config was corrected to the `gates` (list)
  shape. Still `@pytest.mark.integration`; now formally excluded from the default suite (above).

### Notes
- `.gitignore`: added Python packaging artifacts (`build/`, `dist/`, `*.egg-info/`, `*.whl`).
- **Version stays `0.3.1`** (historical note — superseded by the released **1.0.0** above). The earlier "bump to 0.2.0" note was written before the 0.2.0→0.3.x
  releases and is now a no-op (a downgrade); the next bump for these [Unreleased] changes is a
  deliberate **maintainer-gated** action (per the policy at the top of this section), not part of this run.

---

## [0.3.1] — 2026-06-18 (redact hardening)

### Security
- **`redact.py`: tok_live_ shape-pattern hardened + TOKONOMIX key-name canonicalized.**
  - Lowered `tok_live_` body minimum from 20 to 8 characters — short sandbox/onboarding keys were
    slipping through (ticket 2d05782a).
  - Added `TOKONOMIX_API_KEY=<value>` and `TOKONOMIX_KEY=<value>` label patterns — catches raw
    key values that do not carry the `tok_live_` shape prefix (e.g. rotated or test-issuance keys).
  - Extended `test_patterns` in `acceptance/test_redact.py` with `tokonomix-live` and
    `tokonomix-label` cases; suite remains GREEN.

---

## [0.3.0] — 2026-06-18 (monorepo sync)

### Fixed
- **Monorepo copy (#2) reconciled with canonical installed copy (#1).**
  The `public/skills/agents-never-sleep/` tree was at 0.1.0-slice1 (24 harness modules, stale
  acceptance tests). It now mirrors the canonical `/ABSOLUTE/PATH/TO/agents-never-sleep/`
  source at v0.3.0 (28 harness modules). Changes versus the old monorepo copy:
  - **Added harness modules:** `agent_clis.py`, `f5.py` (F5 loop recovery), `parked.py`, `trust.py`
  - **Updated harness modules:** `capabilities.py`, `config.py` (includes `TOKONOMIX_API_KEY`
    fix + `max_tickets_per_run` cap), `council.py`, `decide.py`, `driver.py`,
    `enforcement.py`, `onboarding.py`, `orchestrator.py`, `preflight.py`, `redact.py`,
    `report.py`, `run.py`, `tickets.py`, `vcs.py`
  - **Added acceptance tests:** `test_agent_clis.py`, `test_classify_narrowing.py`,
    `test_classify_override_wiring.py`, `test_f5.py`, `test_fresh_session.py`, `test_launcher.py`,
    `test_managed_env.py`, `test_parked.py`, `test_real_claude.py`, `test_revert_backup.py`,
    `test_run_branch_isolation.py`, `test_tickets.py`; `conftest.py`, `run_all.sh`
  - **Removed stale #2-only tests:** `test_install_hooks.py`, `test_review.py`,
    `test_structured_verdict.py` (functionality covered by newer canonical tests)
  - **Removed:** `hooks/install.sh` (stale), `.tar.gz`/`.zip` archives, `BUILD-PLAN.md`,
    `BUGFIX-PLAN-*.md`
  - **Added:** `ARCHITECTURE.md`, `CHANGELOG.public.md`, `bin/ans-run`, `.github/` templates
  - **#3 (GitHub main) analysis:** GitHub was 1 commit behind canonical (#1 had the
    `max_tickets_per_run` fix + updated `TOKONOMIX_API_KEY` env-var name). No #3-only content
    to port — all #3 differences were superseded by #1.

## [0.3.0] — 2026-06-17

### Added
- **`tokonomix_rate_consensus` integration**: after every council call the agent automatically
  rates usefulness via `tokonomix_rate_consensus`. Behaviour is mode-aware:
  - **Unattended** (`CLAUDE_UNATTENDED=1`): auto-rate, no user prompt, one log line at run start.
  - **Interactive**: ask once, save preference to `.unattended/state/consensus_rating_pref.json`,
    never ask again.
- **Run-start preference flow**: SKILL.md step documents the opt-in prompt + persistence contract.
- **README: "Tokonomix consensus integration" section** — explains auto-rating, both modes, and
  GitHub URL for updates.

### Planned (pushed to future release)
- `pip install agents-never-sleep` (PyPI publish — awaiting maintainer decision on name + version)
- Entry-point console scripts: `ans` / `ans-run`
- `pyproject.toml` with zero declared dependencies

---

## [0.2.0] — 2026-06-20

### Added
- **E2E integration test** (`acceptance/test_real_claude.py`): drives real `claude -p` session
  to completion over 2 tickets; marked `@pytest.mark.integration`, excluded from standard suite.
- `acceptance/conftest.py`: registers `integration` pytest marker to prevent unknown-mark warnings.
- **ARCHITECTURE.md**: defines the stable public API surface (CLI flags, ticket format, gate
  interface, outcome states, config schema) ahead of v1.0 commitment.
- **CHANGELOG.md** (this file): versioning history starting from v0.1.0.

### Changed
- Version bump: `harness/__init__.py` `__version__` from `0.1.0-slice1` to `0.2.0`.

---

## [0.1.0-slice1] — 2026-05-xx (initial harness spine)

### Added (MVP)
- `harness/run.py` CLI with `next` / `complete` / `report` / `run` subcommands.
- Per-ticket state machine: DONE, DONE_LOW_CONFIDENCE, PARKED_DECISION, PARKED_FOUNDATIONAL,
  BLOCKED_ENV, FAILED_RETRYABLE, FAILED_BUG_IN_AGENT.
- `harness/gates.py`: deterministic gate runner with failure taxonomy (introduced-by-diff vs
  pre-existing vs env-timeout).
- `harness/ledger.py`: per-ticket attempt cap + loop detection.
- `harness/vcs.py`: git-backed snapshot-before-edit + revert-on-red.
- `harness/report.py`: morning report writer (Markdown).
- `harness/state.py`: atomic durable outcome store (JSONL).
- `harness/orchestrator.py`: main coordination loop.
- `harness/driver.py`: `next`/`complete` agent-facing API layer.
- `harness/preflight.py`: capability measurement before any tokens are spent.
- `harness/council.py`: multi-model review council (advisory, via Tokonomix gateway).
- `harness/specialists.py`: per-lens specialist reviewers (architect, security, tenant-safety).
- `harness/redact.py`: secret redaction on every write path.
- `harness/keysource.py`: `env:VAR` / `vault:<mount>/<path>` token-ref resolution.
- `harness/watchdog.py`: heartbeat-based sidecar restart (proven in acceptance tests).
- `harness/sources/paperclip.py`: Paperclip issue tracker as ticket source.
- `SKILL.md`: portable agent skill (Claude Code, Gemini CLI, Codex, Copilot, Cursor, Windsurf).
- `bin/ans-run`: launcher with TOFU config trust, GO/NO-GO preflight, flock-based mutex.
- `hooks/`: Claude Code enforcement hooks (stop-guard, deny-irreversible, deny-ask) — opt-in.
- `acceptance/run_acceptance.py`: hermetic acceptance demo (DemoWorker, no live model).
- `acceptance/test_*.py`: 40+ unit tests covering all harness modules.

---

## Roadmap to v1.0 — ✅ reached (v1.0.0 tagged 2026-06-27)

> Historical record. v1.0.0 is released; `SEMVER.md` §4 is the live, machine-checkable version of
> this list. Kept here for the SEMVER cross-reference.

A v1.0 release required:

1. **Stable API surface** — all items in ARCHITECTURE.md verified through at least one real
   production run (done via nightly ANS runs on tokonomix.ai backlog since 2026-06).
2. **pip-installable** — `pip install agents-never-sleep` works in a fresh venv with no extras.
3. **Cross-platform enforcement adapters** — at least Codex and Gemini CLI hooks implemented and
   tested (Claude Code adapter already shipped).
4. **Public documentation site** — tokonomix.ai/agents-never-sleep or equivalent.
5. **Maintainer sign-off** — PyPI publish + GitHub release = explicit human decision, not autonomous.
