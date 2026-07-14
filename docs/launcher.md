# ANS Launcher (`bin/ans-run`)

> **30-second version.** The launcher is a **pre-token GO/NO-GO gate** that runs *before* the coding-agent
> CLI boots — so a doomed unattended run never spends its first token. It checks that the repo's config is
> trusted, the run isn't owned by root, the chosen agent CLI exists and is autonomy-confirmed, credentials
> resolve, the repo is healthy, and the working tree isn't already locked by another run. Exit `0` = GO,
> `64` = NO-GO, `65` = tree busy. Use it for any headless/cron launch. See [watchdog](watchdog.md),
> [security](security.md), [secrets](secrets.md), [glossary](glossary.md).

## Why a pre-token gate

`preflight.py` measures capabilities *after* the agent session boots — by which point the first tokens are
already spent and a doomed run has already cost money. The launcher (`launcher.py`, shipped as
`bin/ans-run`) moves the go/no-go decision *before* the agent CLI starts. It is deterministic: every check
either passes or contributes a blocking failure, and the aggregate decides GO vs NO-GO. Nothing
site-specific is hardcoded — host checks come entirely from the trusted config.

```
ans-run [--repo DIR] [--agent NAME] [--fg] [--check] [--trust] [--] PROMPT...
```

Exit codes (`launcher.py`): **`0`** started / GO (with `--fg`, the agent's own return code is propagated);
**`64`** (`EX_NOGO`) a blocking check failed; **`65`** (`EX_BUSY`) the working tree is already locked by a
running ANS instance.

## The checks (in order)

1. **TOFU config-trust.** `.claude/agents-never-sleep.json` travels with the repo and describes commands
   the launcher will execute (agent argv, host checks). A new or changed config must be trusted **once per
   user** — interactively at the prompt, or explicitly via `ans-run --trust` after review. Trust is keyed
   on the config's SHA-256 and recorded *outside* the repo (`~/.config/agents-never-sleep/trusted.json`),
   because the repo cannot vouch for itself. **Headless + untrusted = NO-GO.** Any config change
   invalidates the trust (`trust.py`). *Never run `ans-run` in a repo you trust less than its `make install`.*
   If the same interactive wizard session pre-authorized any deny-list classes (see
   [glossary](glossary.md#security)), the launcher reads them from the out-of-repo consent store
   and freezes them into the child process as `UE_CONSENT` (plus an audit-trail path `UE_CONSENT_AUDIT`)
   immediately after this trust check — never re-read from the repo mid-run, so the unattended agent
   cannot author or widen its own consent.
2. **Identity / root-guard.** Started as root with a `launcher.target_user` configured → **re-exec as that
   user** (credentials, settings, and repo ownership live in the user's HOME). Started as root with **no**
   target user → **NO-GO** (an unattended run must never own the machine as root). The re-exec keeps the
   launch one command; where it is genuinely needed, use a *command-scoped* sudoers rule, never
   `NOPASSWD: ALL`.
3. **Agent selection.** Named presets under `launcher.agents`, picked by `--agent` or
   `launcher.default_agent`. No platform auto-detection happens at launch (session env markers are
   spoofable and gone under cron). Four sub-gates per preset:
   - `argv[0]` must be a known agent CLI (`claude` / `codex` / `gemini` / `copilot`), unless the trusted
     config sets `launcher.allow_custom_agent`;
   - the binary must pass a **5-second `--version` capability probe** (catches CLI flag drift before tokens
     are spent — probe == spawn rule);
   - **`autonomy_confirmed` must be true** — a preset without it refuses to launch detached (a deliberate
     NO-GO instead of a silent stall at the first approval prompt). See the autonomy table below.
   - **the resolved argv must carry a non-interactive permission flag** for a *detached* launch — verified
     from the argv itself, not just the `autonomy_confirmed` boolean (which a hand-edited config can desync
     from the cmd). A detached preset that would still hit the CLI's approval prompt is a NO-GO; `--fg`
     (a human is attached) is allowed. A preset may also declare a `capabilities` list (e.g.
     `--strict-mcp-config --mcp-config <file>`) to load only the MCP servers / tools a run needs.
4. **Credentials.** `launcher.credentials_paths` is **blocking** when configured, **warn-only** when not
   (keychain / API-key setups have no credentials file; the default probe is `~/.claude/.credentials.json`).
   Token-ref credentials (`env:` / `vault:`) are resolved into the child env *before* the probe — a failed
   resolution is a blocking NO-GO with a clear message, never a silent empty value. See [secrets](secrets.md).
5. **Repo health.** Git usable (catches dubious-ownership), repo writable, disk space ≥ `min_disk_mb`
   (default 1000). A dirty/staged tree is surfaced as a *warning*, not a block.
6. **Host checks.** Service/DB probes come **exclusively** from `launcher.checks`
   (`[{"name", "command", "blocking"}]`) — nothing site-specific is baked into the launcher.

## Autonomy flags are an explicit human decision

A detached run with the CLI's permission system fully on stalls at its first approval prompt (stdin is
closed, nobody is watching); the flag that prevents that grants real power. The launcher refuses to launch
a preset that has not recorded `autonomy_confirmed: true`, and the first-run wizard shows what the flag
grants *before* asking the human to confirm:

| CLI | unattended invocation | the flag grants |
|---|---|---|
| Claude Code | `claude -p --permission-mode acceptEdits` | file edits auto-approved; shell/network stay gated |
| Codex | `codex exec --sandbox workspace-write` | edits/commands inside the workspace sandbox |
| Gemini | `gemini --yolo -p` | EVERYTHING — run in a container/VM or throwaway checkout |
| Copilot | `copilot --allow-all-tools -p` | everything (required for programmatic `-p`) |

The map (`agent_clis.py`) is the single source for both the wizard and the launcher, so the two never drift.
For a detached launch the launcher does **not** trust the `autonomy_confirmed` boolean alone: it inspects
the resolved argv and refuses (NO-GO) if it carries no non-interactive permission flag — catching a
hand-edited preset that would otherwise hang silently at the first tool prompt. (`--permission-mode
acceptEdits` clears the hang but only auto-approves *edits*; shell/network stay gated, so a bash-heavy
detached run makes little progress — full autonomy is `--dangerously-skip-permissions` / the CLI equivalent.)

## Atomic, pidfile-free mutual exclusion

The launcher takes a **non-blocking `flock(2)`** on `<repo>/.unattended/ans-run.lock` *before* the
expensive probes, and hands the open FD to the long-lived agent process. The kernel holds the lock exactly
as long as the run lives and releases it on any crash or kill — no stale pidfile state (pidfiles were
rejected in review as TOCTOU-racy). The lock is **repo-local**, so only principals who can already write
the repo can touch it. Two simultaneous starts yield exactly one winner; `--check` only probes the lock and
never blocks a launch. Opt-out for intentionally disjoint worktrees: `ANS_RUN_NO_LOCK=1`.

## Managed (Tokonomix-delegated) routing

A preset's `env` map can point the spawned CLI at an OpenAI-compatible gateway (`OPENAI_BASE_URL`,
`OPENAI_API_KEY=env:TOKONOMIX_KEY`), so model choice, budget caps, EU-residency, and central billing are
configured **once** on a Tokonomix token instead of per machine. The key is always a **token-ref, never a
literal** (a pasted-key-shaped literal is loudly flagged at launch). This is the *governance* tier; the DIY
path with your own provider keys stays fully functional. See [secrets](secrets.md).

## Boundary

The launcher governs *whether and how an unattended run starts* — it does not choose models for you
(that's Routing / the managed gateway), generate code, or verify it. See the [glossary](glossary.md)
ecosystem table.

## Limitations

The launcher gates the *start* of a run; it cannot prevent a problem that only emerges mid-run (that is the
[watchdog](watchdog.md)'s job). The `--version` probe catches flag drift but not every CLI behaviour
change. Trust is per-user and per-config-hash: a legitimately updated config must be re-trusted, by design.

---

*Verified against `agents_never_sleep/` (v1.0.0): `launcher.py` (`EX_NOGO=64`, `EX_BUSY=65`, check order,
`min_disk_mb=1000`, `DEFAULT_CREDENTIAL_PROBES`, root-guard re-exec, flock), `trust.py` (TOFU,
`config_digest`), `agent_clis.py` (autonomy map), `keysource.py` (token-refs), SKILL.md.*
