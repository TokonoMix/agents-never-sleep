# Hermes enforcement adapter (in-process plugin)

Hermes is the maintainer's own in-process Python orchestrator (a self-hosted in-process agent
orchestrator). Unlike the
out-of-process dispatcher platforms (gemini/codex/copilot/cursor/windsurf), Hermes has a
**native `pre_tool_call` plugin hook** that fires *before* the clarify/inline special-cases on
every dispatch path — so ANS enforces here by registering a hook that calls the shared
`decide()` core **in-process**. This is ANS's first in-process adapter.

## What it enforces (capability matrix row)

| Guarantee | Status | How |
|---|---|---|
| deny-irreversible | ✅ native | the hook blocks any command-bearing tool whose command matches `decide()`'s irreversible patterns |
| never-ASK | ✅ native | the hook denies the `clarify` tool **before** Hermes's 120s clarify-timeout — preempting the fail-OPEN "use your best judgement and proceed" (invented consent) at `cli.py:8655` |
| never-stop | 🟡 soft-enforced | Hermes has no stop hook that can veto end-of-turn (`on_session_end` is observer-only). Continuation falls back to the SKILL.md prose contract + a morning-report BLIND SPOT. Upgradeable to native via an in-tree patch (re-inject at the no-tool-calls branch when the run-incomplete sentinel exists) — staged separately. |

Hermes is the **2nd platform after Claude with native never-ASK**, because `clarify` is a real
askable tool sitting behind a real pre-hook.

## Install (opt-in, env-gated)

1. Make `agents_never_sleep` importable in **Hermes's** Python env (v1.1.0 is the release
   target — until that tag is cut, install from the branch or the local checkout):
   ```
   pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.1.0   # once released
   # interim: pip install /path/to/public/skills/agents-never-sleep
   ```
2. Copy this directory to `~/.hermes/plugins/agents-never-sleep/` (so it contains
   `plugin.yaml` + `__init__.py`).
3. **Enable it** — user plugins under `~/.hermes/plugins/` are opt-in: add `agents-never-sleep`
   to `config.yaml` `plugins.enabled` (else it is silently skipped, `plugins.py:740`).
4. The hook is **inert unless `UE_UNATTENDED=1`** (or `CLAUDE_UNATTENDED=1`) — set it (the
   ANS driver does) only for real unattended runs; normal interactive Hermes is untouched.

## Verify after install (do this once)

- Start an unattended Hermes session with `UE_UNATTENDED=1` and confirm a deliberately
  irreversible command (e.g. `git push --force`) is blocked, and that a `clarify` call is
  denied with the ANS PARK steer instead of timing out into "agent will decide".
- Until that live smoke-test passes, Hermes is **not** in `LIVE_VERIFIED` — the run report
  says "built to the documented contract, NOT yet live-verified". `systemctl --user` is not
  reachable from an unattended Claude shell, so this step is maintainer-side.

## Open items (carry these)

- **never-ASK on the gateway is config-dependent.** `clarify` is excluded from the
  `openai-api-server`/`editor` tool distributions (`toolsets.py:305,324`); the gateway resolves
  tools per session config. The CLI fail-open is always reachable (the value story holds there).
  Confirm the actual unattended Hermes config exposes `clarify` for full gateway coverage.
- **never-stop** is soft-enforced until the in-tree no-tool-calls patch lands.
