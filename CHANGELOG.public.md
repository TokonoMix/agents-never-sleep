# Public changelog — agents-never-sleep

User-facing changes only. No ticket numbers, no internal flags, no internal names.

---

## [1.0.0] — June 2026

- **First stable release.** The command-line interface, configuration, ticket format, and run
  outcomes are now covered by a Semantic Versioning stability guarantee — future updates won't
  break them without a major-version bump.
- **Import package renamed** `harness` → `agents_never_sleep` to match the install name. The old
  `import harness` / `python -m harness.run` keep working (with a deprecation warning) through all
  of 1.x and are removed in 2.0 — migrate when convenient.
- **Cross-platform enforcement** for Claude Code, Gemini CLI, Codex CLI, Copilot CLI, Cursor, and
  Windsurf, each tested against its documented hook contract (only Claude Code is additionally
  live-verified; the rest say so in the report).
- **Hardening:** secrets pasted into a run's notes can no longer linger in the on-disk run state;
  ships type information (`py.typed`) for type-checkers.
- Removed the old in-process `run` command — real runs use the `next`/`complete` loop.

## [0.3.0] — June 2026

- improvement: **Tokonomix consensus integration** — after every council call the
  agent automatically rates usefulness via `tokonomix_rate_consensus`. Behaviour
  is mode-aware:
  - **Unattended** (`CLAUDE_UNATTENDED=1`): auto-rates silently, logs once at
    run start.
  - **Interactive**: asks once, saves preference, never asks again.
- improvement: Run-start preference flow — SKILL.md documents the opt-in prompt
  and persistence contract (`consensus_rating_pref.json`).
- docs: README gains a "Tokonomix consensus integration" section explaining
  auto-rating, both modes, and GitHub URL for updates.

## [0.2.0] — June 2026

- improvement: **E2E integration test** (`acceptance/test_real_claude.py`) —
  drives a real `claude -p` session to completion over 2 tickets; excluded from
  the standard suite (`@pytest.mark.integration`).
- improvement: **ARCHITECTURE.md** — defines the stable public API surface (CLI
  flags, ticket format, gate interface, outcome states, config schema) ahead of a
  v1.0 commitment.
- improvement: **CHANGELOG.md** — versioning history from v0.1.0 forward.

## [0.1.0] — May 2026

- improvement: Initial release — per-ticket state machine (DONE,
  PARKED_DECISION, PARKED_FOUNDATIONAL, BLOCKED_ENV, FAILED_RETRYABLE,
  FAILED_BUG_IN_AGENT), deterministic gate runner with failure taxonomy
  (introduced-by-diff vs pre-existing vs env-timeout), attempt and loop caps,
  morning report, and git-backed reversibility.

---

## Related skills

- [tokonomix-council-mcp](https://www.npmjs.com/package/tokonomix-council-mcp) —
  MCP tools used by ANS for consensus gating.
- [tokonomix-gateway](https://tokonomix.ai/api/v1/capabilities) — direct HTTP
  access for custom integrations.
