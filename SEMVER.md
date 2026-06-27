# SemVer policy — agents-never-sleep

> **STATUS: BINDING from v1.0.0 (2026-06-27).** The stability guarantee below is in force. A
> breaking change to any surface classified **Stable** in §2 requires a MAJOR bump, announced in
> the CHANGELOG with a migration note.

This file complements two existing documents — read them together:

- **`ARCHITECTURE.md`** — enumerates the public API surface (CLI flags, ticket format, gate
  interface, outcome states, config schema). That doc is the *contract*; this doc is the *policy*
  for how the contract may change.
- **`CHANGELOG.md`** — the per-version record of what changed; it already carries a "Roadmap to
  v1.0" list, which §4 here makes concrete and checkable.

The current published version is **`1.0.0`** (source of truth: `agents_never_sleep/__init__.py` `__version__`).

---

## 1. What SemVer means for this package

We follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). For a version
`MAJOR.MINOR.PATCH`, **once 1.0 is tagged**:

- **MAJOR** — a breaking change to anything classified **Stable** in §2 (a removed CLI flag, a
  changed JSON output key, a removed/renamed config key, a changed/removed outcome-state value, a
  renamed importable symbol). Breaking changes are announced in the CHANGELOG with a migration note.
- **MINOR** — backward-compatible additions: a new optional CLI flag, a new optional config key, a
  new JSON output field, a new outcome state, a new (additive) integration. Existing callers keep
  working unchanged.
- **PATCH** — bug fixes and internal changes that do not alter any Stable surface (e.g. the redact
  hardening in 0.3.1).

**Decision D5 — the guarantee scope, and where it deliberately stops.** The commitment above covers
the **ANS API**: the loop/launcher CLIs, the config + ticket-file schema, the outcome states, and the
console entry points (§2). It does **not** extend to the *behaviour* of a cross-platform enforcement
adapter on a third-party host, because that depends on the host's hook contract — which can change
outside ANS's control. Per-platform adapter behaviour is therefore **best-effort, validated against
the hook-contract version recorded for each platform** (`agents_never_sleep/capabilities.py`
`_HOOK_CONTRACT`, surfaced in the run report; only Claude Code is live-verified). If a host changes
its hook API and a deny stops registering, that is a host change to adapt to — not a MAJOR break of
the ANS API. This keeps the stability promise honest: strong where ANS owns the surface, explicit
about the one seam it doesn't.


## 2. Public API surface — Stable vs Experimental

"Stable" = covered by the §1 guarantee at 1.0. "Experimental" = may change in a MINOR before
1.0-equivalent stabilization; do not build load-bearing automation on it without pinning.

### 2.1 CLI — the loop (`python3 -m agents_never_sleep.run` / `ans`)

| Surface | Class | Notes |
|---|---|---|
| `next` subcommand + `--repo`, `--tickets` flags | **Stable** | Primary agent-driven entry. |
| `complete` subcommand + `--attempted`, `--cannot-implement` | **Stable** | Core loop. |
| `complete` council/specialist flags (`--council-verdict`, `--council-cost`, `--review-coverage`, `--specialist-concerns`, `--specialist-cost`, `--council-http-status`) | **Experimental** | Tied to the review-council feature, which is still evolving. |
| `report` subcommand | **Stable** | Re-emits the morning report. |
| `reset-attempts`, `reset-spend` | **Stable** | Operator escapes. |
| `parked protect|restore` | **Experimental** | WIP-protection surface (INT-1735). |
| JSON output shapes in ARCHITECTURE.md §1 | **Stable** | Documented keys only; undocumented keys may appear/disappear. |

### 2.2 CLI — the launcher (`bin/ans-run` / `ans-run`)

| Surface | Class | Notes |
|---|---|---|
| `ans-run [--repo] [--agent] [--fg] [--check] [--trust] PROMPT...` | **Stable** | Flags + exit codes (0 / 64 NO-GO / 65 busy). |
| `.claude/agents-never-sleep.json` `launcher` section (presets, `target_user`, `checks`, `credentials_paths`, `min_disk_mb`, `fresh_session_every`) | **Stable** | Additive keys = MINOR. |
| `ANS_RUN_NO_LOCK`, `ANS_TRUST_STORE` env vars | **Experimental** | Escape hatches / test hooks — **kept Experimental at 1.0 (decision D4)**. `ANS_TRUST_STORE` is honored only under `ANS_TEST_MODE=1` (a test fixture, not a runtime knob); `ANS_RUN_NO_LOCK=1` bypasses the working-tree lock (an operator escape). Neither is a behaviour a 1.0 should freeze. |

### 2.3 Ticket format (ARCHITECTURE.md §2)

| Surface | Class |
|---|---|
| `.md` + YAML front-matter (`id`, `title`, `blast_radius`, `expected_outcome`); harness never mutates ticket files | **Stable** |

### 2.4 Gate interface & config (ARCHITECTURE.md §3, §5)

| Surface | Class | Notes |
|---|---|---|
| `gate.command` / `gate.timeout_s` / `gate.cwd`; green=exit 0 contract | **Stable** | |
| Top-level config keys `gate`, `budget`, `integrations`, `launcher` | **Stable** | |
| `council`, `specialists` config sections | **Experimental** | Feature still maturing. |
| `integrations.paperclip`, `integrations.tokonomix` | **Experimental** | External-service shaped. |

### 2.5 Outcome states (ARCHITECTURE.md §4)

All seven values (`DONE`, `DONE_LOW_CONFIDENCE`, `PARKED_DECISION`, `PARKED_FOUNDATIONAL`,
`BLOCKED_ENV`, `FAILED_RETRYABLE`, `FAILED_BUG_IN_AGENT`) are **Stable** — they are how a consumer
interprets a run. New states may be ADDED (MINOR); existing ones are not renamed/removed without a
MAJOR bump.

### 2.6 Python import surface (`import agents_never_sleep`)

| Surface | Class | Notes |
|---|---|---|
| `agents_never_sleep.__version__` | **Stable** | Canonical version symbol. |
| `agents_never_sleep.launcher:main`, `agents_never_sleep.run:main` (the console entry points) | **Stable** | Their *callability* is the contract, not their internal signature. |
| Everything else under `agents_never_sleep.*` (e.g. `.driver`, `.orchestrator`, `.council`) | **Experimental / internal** | Importable but NOT a supported API. Use the CLI. |
| `harness` (and `harness.*`, `harness.__version__`) | **Deprecated shim** | Back-compat for the pre-1.0 import name; re-exports `agents_never_sleep` and emits one `DeprecationWarning`. Works through all of 1.x; **removed in 2.0**. |

> ✅ **v1.0 blocker resolved (decision D1).** The package was renamed `harness` →
> `agents_never_sleep` so the public import path matches the distribution name (a generic
> `harness` is collision-prone once pip-installed; after 1.0 this rename would be a MAJOR bump).
> A thin `harness` shim preserves every pre-1.0 recipe (`import harness`, `from harness.launcher
> import main`, `python -m harness.run` / `-m harness.enforce`) through 1.x and is removed in 2.0.
> Verified from a fresh-venv wheel install; guarded by `acceptance/test_shim.py`.

## 3. Deprecation policy (applies from 1.0)

- A Stable surface marked **deprecated** keeps working for at least one MINOR release, emits a
  warning where practical, and is documented in the CHANGELOG under a "Deprecated" heading.
- Removal of a deprecated surface happens only in a MAJOR release.
- Experimental surfaces may change or be removed in a MINOR with a CHANGELOG note, no deprecation
  window.

## 4. Roadmap to v1.0 — what must be true to tag it

This refines the existing "Roadmap to v1.0" in `CHANGELOG.md` into a checkable list. **Tagging 1.0,
publishing to PyPI, and cutting the GitHub release are explicit Mes decisions — never autonomous.**

| # | Criterion | Status |
|---|---|---|
| 1 | **Stable surface frozen & reviewed** — §2 classifications signed off; "Experimental" items either stabilized or explicitly kept experimental past 1.0. | ◐ drafted + machine-guarded by `acceptance/test_surface_drift.py` (subcommands, core flags, 7 outcome states); ☐ pending Mes sign-off |
| 2 | **`pip install agents-never-sleep` works** in a fresh venv, both console scripts on PATH. | ✅ built + verified locally (wheel installs offline, `ans-run`/`ans` run from a venv); **PyPI publish is a Mes action** |
| 3 | **Real-agent E2E proof** — `acceptance/test_real_claude.py` drives real `claude -p` to DRAINED, asserts done≥2 **and** committed work on the run branch. | ✅ test present + collects clean; live run is Mes/credential-gated |
| 4 | **Package rename** `harness` → `agents_never_sleep` (§2.6 blocker) decided + executed, or explicitly waived. | ✅ done — renamed with a back-compat `harness` shim (decision D1); verified from a fresh-venv wheel install; guarded by `acceptance/test_shim.py` |
| 5 | **Cross-platform enforcement adapters** — the 6 scaffolded adapters (Claude/Gemini/Codex/Copilot/Cursor/Windsurf) hermetically tested + a recorded hook-contract version each (Aider + Hermes → 1.1). | ◐ hermetic negative tests + hook-version record done; per-platform LIVE-smoke = Mes/credential-gated |
| 6 | **Public docs** — doc CONTENT consistent (README/SKILL/ARCHITECTURE/SEMVER); the public site at tokonomix.ai/agents-never-sleep (styled HTML + nav + indexed publish) = Mes-gated. | ◐ content ready; ☐ public publish |
| 7 | **CHANGELOG complete** with a `1.0.0` entry + the SemVer commitment statement made final (DRAFT banner removed). | ✅ done — CHANGELOG 1.0.0 dated, DRAFT banner removed, guarantee binding |
| 8 | **Mes sign-off** — GitHub release + git tag `v1.0.0` (PyPI = optional, deferred). | ✅ GitHub release `v1.0.0` cut (2026-06-27); **PyPI deferred** (GitHub is the distribution channel; PyPI is an optional later add when a token exists) |

**v1.0.0 was tagged 2026-06-27** — the guarantee above is binding from that commit. Distribution is
the public GitHub repo (`github.com/TokonoMix/agents-never-sleep`, `pip install git+…@v1.0.0`); a
PyPI mirror is an optional later convenience, not a maintenance commitment.
