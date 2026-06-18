# Public changelog — agents-never-sleep

User-facing changes only. No ticket numbers, no internal flags, no internal names.

---

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
