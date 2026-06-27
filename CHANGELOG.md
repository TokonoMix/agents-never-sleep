# Changelog — agents-never-sleep

All notable changes to the public API surface (see `ARCHITECTURE.md`) are documented here.
The versioning **policy** (Stable vs Experimental classification + the v1.0 roadmap) lives in
`SEMVER.md`.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

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
- **Packaging — `pyproject.toml` (ticket INT-1964).** `pip install agents-never-sleep` now works
  in a fresh venv with **zero runtime dependencies** (the harness is pure standard library).
  - Two console scripts: `ans-run` → `harness.launcher:main` (pre-token preflight launcher) and
    `ans` → `harness.run:main` (the `next`/`complete` loop). External users no longer clone the
    repo + hand-set `PYTHONPATH`.
  - Version is read dynamically from `harness/__init__.py` (`[tool.setuptools.dynamic]`) — single
    source of truth, no second place to bump.
  - Verified locally: wheel builds offline (`pip wheel . --no-deps`), installs into a throwaway
    venv, and `ans-run --check` runs from the installed entry point with no checkout on PATH.
  - **PyPI publish is a Mes action** (not done here): `twine upload` with the project's PyPI token.
- **`SEMVER.md` (DRAFT, ticket INT-1966).** Formal SemVer commitment: Stable-vs-Experimental
  classification of every public surface (loop CLI, launcher CLI, ticket format, gate/config,
  outcome states, import surface) + a deprecation policy + a checkable v1.0 roadmap. Draft only —
  1.0 is not declared; flags the `harness`→`agents_never_sleep` package-rename as a v1.0 blocker.
- **`PIP-INSTALL-PLAN.md` (ticket INT-1964).** Grounded plan for `pip install agents-never-sleep`:
  documents the (verified) install path, the two-track distribution (pip = harness CLI vs the
  Agent Skill = repo/hooks), and the remaining steps (name claim, build+twine, 3.9–3.12 smoke
  matrix, PyPI publish = Mes). Also published to `/internip/ans-pip-install-plan-2026-06-20.html`.
- **`COUNCIL-SETUP.md` (ticket INT-1963).** Reproducible record of how to make the Tokonomix
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
- **`acceptance/test_real_claude.py` (ticket INT-1965): now VERIFIED GREEN against a live
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
- **Version stays `0.3.1`.** Ticket INT-1966's "bump to 0.2.0" was written before the 0.2.0→0.3.x
  releases and is now a no-op (a downgrade); the next bump for these [Unreleased] changes is a
  deliberate **Mes-gated** action (per the policy at the top of this section), not part of this run.

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
  acceptance tests). It now mirrors the canonical `/home/claude/.claude/skills/agents-never-sleep/`
  source at v0.3.0 (28 harness modules). Changes versus the old monorepo copy:
  - **Added harness modules:** `agent_clis.py`, `f5.py` (F5 loop recovery), `parked.py`, `trust.py`
  - **Updated harness modules:** `capabilities.py`, `config.py` (includes `TOKONOMIX_API_KEY`
    fix + `max_tickets_per_run` cap, INT-1935), `council.py`, `decide.py`, `driver.py`,
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
  - **#3 (GitHub main) analysis:** GitHub was 1 commit behind canonical (#1 had INT-1935
    `max_tickets_per_run` + updated `TOKONOMIX_API_KEY` env-var name). No #3-only content
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
- `pip install agents-never-sleep` (PyPI publish — awaiting Mes decision on name + version)
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

## Roadmap to v1.0

A v1.0 release requires:

1. **Stable API surface** — all items in ARCHITECTURE.md verified through at least one real
   production run (done via nightly ANS runs on tokonomix.ai backlog since 2026-06).
2. **pip-installable** — `pip install agents-never-sleep` works in a fresh venv with no extras.
3. **Cross-platform enforcement adapters** — at least Codex and Gemini CLI hooks implemented and
   tested (Claude Code adapter already shipped).
4. **Public documentation site** — tokonomix.ai/agents-never-sleep or equivalent.
5. **Mes sign-off** — PyPI publish + GitHub release = explicit human decision, not autonomous.
