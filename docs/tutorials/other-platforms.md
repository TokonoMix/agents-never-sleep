# Tutorial — other platforms (Codex, Gemini CLI, Copilot, Cursor, Windsurf)

> ⚠️ **Honesty banner — read this first.** These adapters are built to each platform's
> **documented hook contract** (as researched 2026-06) and proven hermetically by
> `acceptance/test_enforce_platforms.py`. **Only Claude Code is live-verified on the real tool.**
> Everything below describes mechanism, not a promise of outcome: treat your **first unattended run
> on any of these platforms as a verification run** — watch it, confirm an irreversible command is
> actually blocked and (where native) a stop is blocked or an ask denied, and report findings. The
> run report itself states this verification status; a host changing its hook API is the one
> failure mode outside ANS's control.

ANS is platform-neutral by design: the harness (`next`/`complete`, gates, state, report) is a plain
Python CLI any coding agent can drive, and the enforcement layer is a shared dispatcher
(`python3 -m agents_never_sleep.enforce <platform> <event>`, launched via `hooks/enforce.sh`) that
each platform's hook system calls with its own payload shape. What differs per platform is (1) the
unattended invocation and what its autonomy flag grants, and (2) how much of the autonomy contract
the platform's hooks can enforce natively. Sources of truth: `agents_never_sleep/agent_clis.py`
(invocations), `agents_never_sleep/enforce.py` + `hooks/platforms/` (enforcement), and
[hooks/platforms/README.md](../../hooks/platforms/README.md) (capability matrix). Terms like
"autonomy contract" and "run report" are defined in the [glossary](../glossary.md).

## What "enforcement maps" means

Three guarantees are enforced at the hook layer on Claude Code: **deny-irreversible** (block
force-push, destructive SQL, etc. before they run), **never-stop** (block a premature end-of-turn
while the run-incomplete sentinel exists), and **never-ASK** (deny the ask-a-human tool; the
decision parks instead). On other platforms each guarantee is native where the host exposes a
blocking hook for it, and **soft-enforced** where it does not — soft-enforced means the SKILL.md
prose contract instructs the agent, and the run report emits a loud **BLIND SPOT** for that
guarantee (`agents_never_sleep/capabilities.py`), never a silent drop.

| Platform | deny-irreversible | never-stop | never-ASK |
|---|---|---|---|
| Claude Code | ✅ native | ✅ native | ✅ native |
| Codex CLI | ✅ native | ✅ native | 🟡 soft-enforced (no pre-ask hook) |
| Gemini CLI | ✅ native | ✅ native | 🟡 soft-enforced (no pre-ask hook) |
| Copilot CLI | ✅ native | ✅ native | ✅ native (`ask_user` tool) |
| Cursor | ✅ native | 🟡 soft-enforced (stop hook can't block) | 🟡 soft-enforced (no pre-ask hook) |
| Windsurf | ✅ native | 🟡 soft-enforced (no stop event) | 🟡 soft-enforced (no pre-ask hook) |

The consent manifest (see [glossary](../glossary.md#security)) is platform-neutral: it's read by
`enforce.py`'s shared `decide()` call regardless of which platform's dispatcher invoked it, so a
pre-authorized class applies identically whether the enforcement hook fired from Claude Code, Codex,
Gemini, Copilot, Cursor, or Windsurf.

All adapters are **env-gated**: the dispatcher is inert unless `UE_UNATTENDED=1` (or
`CLAUDE_UNATTENDED=1`) is set, so wiring the hooks changes nothing in your normal interactive
sessions. For never-stop to work, the platform must see the sentinel — export
`UE_RUN_INCOMPLETE=<repo>/.unattended/run-incomplete`. Set `UE_PLATFORM=<id>` so the degradation
reporting knows which capability set applies. The dispatcher **fails open** (allows) on anything
unexpected — enforcement must never wedge a tool call.

## Codex CLI

**Unattended invocation** (from `agent_clis.py` — the wizard shows this and a human must confirm
`autonomy_confirmed` before a preset launches):

```
codex exec --sandbox workspace-write
```

The safe variant is `codex exec` (approvals fully on — a detached run can stall on the first
prompt). **What the flag grants:** auto-approves edits/commands *inside the workspace sandbox*;
network and out-of-tree writes stay blocked.

**Enforcement:** copy `hooks/platforms/codex/hooks.json` to `~/.codex/hooks.json` (or fold into
`config.toml [hooks]`), replacing `<SKILL_DIR>`. Native: deny-irreversible (`PreToolUse` on Bash) +
never-stop (`Stop`). **Not covered:** never-ASK — Codex has no pre-ask hook, so it is soft-enforced
and reported as a blind spot.

## Gemini CLI

**Unattended invocation:**

```
gemini --yolo -p
```

The safe variant is `gemini -p`. **What the flag grants — read this twice:** `--yolo` auto-approves
**ALL** tool calls — file writes, shell **and network**. There is no edits-only middle tier for
`-p`. Per the recorded guidance in `agent_clis.py`: **run it in a container/VM or on a throwaway
checkout.**

**Enforcement:** merge `hooks/platforms/gemini/settings.json` into `~/.gemini/settings.json`.
Native: deny-irreversible (`BeforeTool` on `run_shell_command`) + never-stop (`AfterAgent`).
**Not covered:** never-ASK — no pre-ask hook; soft-enforced, reported as a blind spot.

## Copilot CLI

**Unattended invocation:**

```
copilot --allow-all-tools -p
```

The safe variant is `copilot -p`, but `-p` *requires* `--allow-all-tools` to run programmatically.
**What the flag grants:** auto-approves **ALL** tool calls — treat it like a full permission
bypass.

**Enforcement:** copy `hooks/platforms/copilot/agents-never-sleep.json` to `<repo>/.github/hooks/`.
Native: all three — deny-irreversible and never-ASK via `preToolUse` (the matcher covers `bash`,
`powershell` *and* the `ask_user` tool, so never-ASK **is** hook-enforced here), plus never-stop
via `agentStop`.

## Cursor

Cursor is an IDE agent, not a headless CLI: **`agent_clis.py` records no unattended invocation and
no autonomy flag for it**, and the launcher's allowlist (`claude`, `codex`, `gemini`, `copilot`)
does not include it — so `ans-run` will not spawn it without the explicit `allow_custom_agent`
opt-out, and how you start an unattended Cursor agent is between you and Cursor's own docs. What
ANS provides is the enforcement adapter and the harness loop.

**Enforcement:** copy `hooks/platforms/cursor/hooks.json` to `<project>/.cursor/hooks.json` (or
`~/.cursor/hooks.json`). Native: deny-irreversible (`beforeShellExecution`). **Not covered:**
never-stop — Cursor's stop hook cannot block, only nudge a follow-up — and never-ASK (no pre-ask
hook); both fall back to the prose contract and are reported as blind spots.

## Windsurf

Same status as Cursor: an IDE agent with **no entry in `agent_clis.py`** (no recorded unattended
argv or autonomy flag, not on the launcher allowlist). ANS provides the enforcement adapter and the
harness loop.

**Enforcement:** copy `hooks/platforms/windsurf/hooks.json` to `~/.codeium/windsurf/hooks.json`
(Desktop) or `<workspace>/.windsurf/hooks.json`. Native: deny-irreversible (`pre_run_command`
blocks via exit code 2). **Not covered:** never-stop (no blocking stop event) and never-ASK (no
pre-ask hook) — prose contract + blind-spot report.

## What is NOT covered, on any platform

- **No Hermes or Aider adapters.** Directories exist under `hooks/platforms/` as scaffolding, but
  these are v1.1 roadmap items — do not wire or rely on them today.
- **No outcome guarantees.** A native hook blocks what its matcher sees; a soft-enforced guarantee
  is an instruction, not a mechanism. The capability matrix cell, not this tutorial's prose, is the
  claim.
- **No live verification beyond Claude Code.** The five other adapters are
  documented-contract-built and hermetically tested; the live smoke-test on each real tool is the
  remaining manual step. When you run one, you are that verification — please report what you find.

Install: `pip install agents-never-sleep` (live on PyPI since v1.4.0), or pinned from GitHub
(`pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.0.0`).

---

*Verified against `agents_never_sleep/` (v1.0.0): `agent_clis.py` (`AGENT_CLIS` cmd_safe/
cmd_unattended argv + `grants` text, `ALLOWLIST`, `NONINTERACTIVE_MARKERS`), `enforce.py`
(dispatcher events, `UE_UNATTENDED`/`CLAUDE_UNATTENDED` gating, fail-open, sentinel path),
`hooks/platforms/README.md` + the five per-platform config snippets (hook event names, install
paths, native-vs-degraded notes), and `hooks/enforce.sh`.*
