# Changelog — agents-never-sleep

All notable changes to the public API surface (see `ARCHITECTURE.md`) are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
