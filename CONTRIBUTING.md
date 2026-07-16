# Contributing to agents-never-sleep

Thanks for your interest! This is a platform-neutral skill + Python harness that
runs a coding-agent backlog to completion unattended — PROCEED / PARK / HALT,
never ASK, never irreversible unsupervised.

## Layout

- `SKILL.md` — the agent-facing contract (start here).
- `agents_never_sleep/` — deterministic stdlib engine (state machine, gates, council
  routing, budget gate, ledger, report). No third-party deps; no network/LLM calls — the
  agent makes those. (The old import name `harness` still works via a back-compat shim,
  removed in 2.0.)
- `hooks/` — per-platform enforcement (Claude Code / Cursor / Gemini / Codex / …).
- `acceptance/` — the test suite. `python3 acceptance/run_acceptance.py` must end
  `RESULT: ✅ GREEN`.

## Development

```bash
python3 acceptance/run_acceptance.py     # full acceptance — must stay GREEN
python3 acceptance/test_budget.py        # a single suite
```

Pure standard library, Python 3.9+. No build step.

### Pre-commit redaction check

A committed hook mirrors CI's public-surface redaction gate so an internal identifier is caught
before it is committed, not after push. Install it once, per clone:

```bash
bash scripts/install-git-hooks.sh
```

This symlinks the tracked `scripts/hooks/pre-commit` into your local `.git/hooks/` (idempotent,
worktree-aware; pass `--force` to replace an existing hook). The hook runs `bash
scripts/redact_lint.sh` and blocks the commit if the gate fails. Because git hooks are never
distributed by `git clone` and `--no-verify` bypasses them, this local hook is the early catch —
**CI remains the authoritative, non-bypassable gate.** `acceptance/test_precommit_redaction.py`
proves the hook actually blocks a leak.

## Pull requests

- Keep the two structural guarantees intact: **never ASK in unattended mode**,
  **never act irreversibly unsupervised**. New code paths must respect them. The one sanctioned
  exception is the consent manifest: a human's run-setup, out-of-repo pre-authorization of a specific
  deny-list class (see [glossary](docs/glossary.md#security)) — not a code path that lets the
  agent decide for itself.
- Add/extend acceptance tests for behaviour changes; the suite must stay GREEN.
- Never commit secrets, runtime state (`.unattended/`, `state/`, reports), or real
  credentials. Test fixtures must be obviously fake.
- Surgical, focused changes; match the existing style.
- Adding a new irreversible-command pattern? It goes in `enforcement.py::_IRREVERSIBLE` only (the sole
  copy every platform shares) with a stable, never-renamed slug — that slug is a consent-manifest key.
  Don't duplicate the pattern in SKILL.md, a hook file, or platform-specific code.

## Surface parity (public knowledge)

A change that introduces or alters **user-visible mechanism knowledge** must land with a
matching entry in `site-src/content/mechanisms/` (the content SSOT that generates the public
`/en/mechanisms/` page and `llms.txt`). CI's `surface-parity` gate fires when a
`### Added`/`### Changed` CHANGELOG line or a `knowledge-affecting` PR label is present
without an SSOT update.

The obligation fires at **publish-readiness (flag-flip / GA)**, not at initial merge — a
flag-gated feature does not need a public entry until it is meant to be public.

If a gate-fire is a false positive (e.g. an internal-only refactor that tripped a label),
add ONE line to the PR body: `surface-waiver: <reason>`. This is the authorized, logged
escape — do NOT use `git commit --no-verify`. Waivers are visible in the PR and reviewable.

SSOT entries are **public-safe by construction**: no tenant/account IDs, DB names, Vault
paths, cost figures, internal hostnames, or private endpoints (enforced by
`site-src/tools/redact_lint.py`, whose org-specific vocabulary lives in a gitignored
`redact-vocab.local.json`), and no absolute "never/always" outcome claims.

## Code of Conduct

This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.

## Security

Report vulnerabilities privately — see [SECURITY.md](./SECURITY.md).

Contributions are licensed under the repo's [MIT license](./LICENSE).
