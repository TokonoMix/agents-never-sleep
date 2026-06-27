# Cross-platform enforcement — design

**Goal:** make the agents-never-sleep autonomy contract (never-ASK, deny-irreversible, never-stop)
enforceable on **all** supported agent platforms — Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub
Copilot CLI, Windsurf Cascade, Cursor — not just Claude. Chosen strategy (owner decision):
**best-effort + graceful degradation** — enforce natively where the platform's hook system allows it;
where it doesn't, fall back to the SKILL.md prose contract AND surface a loud blind-spot (startup note
+ morning-report entry) so a missing guarantee is never silent.

**Done-bar (owner decision):** hermetic + contract-correct. Adapters are built against each platform's
DOCUMENTED hook contract (researched 2026-06, sources below) and tested hermetically (correct stdin→
deny/block shape). A live smoke-test on each real tool is a documented manual follow-up — those tools
aren't installed in the build environment.

## The three guarantees and their per-platform hook mapping

| Guarantee | What it needs | Mechanism |
|---|---|---|
| **deny-irreversible** | a PRE-shell/command hook that can DENY | block destructive/outward commands |
| **never-stop** | an end-of-turn/stop hook that can BLOCK the stop | keep working until the backlog drains |
| **never-ASK** | a hook before the agent asks the user, that can DENY | convert ASK→PARK/PROCEED |

## Capability matrix (from researched contracts — see Sources)

| Platform | deny-irreversible | never-stop | never-ASK | config location |
|---|---|---|---|---|
| **Claude Code** | ✅ `PreToolUse`(Bash) deny | ✅ `Stop` `decision:block` | ✅ `PreToolUse`(AskUserQuestion) deny | `~/.claude/settings.json` |
| **Gemini CLI** | ✅ `BeforeTool`(run_shell_command) `decision:deny` | ✅ `AfterAgent` `decision:deny` (retry) | ⚠️ **degrade** — no ask-tool hook | `.gemini/settings.json` |
| **Codex CLI** | ✅ `PreToolUse`/`PermissionRequest`(Bash) deny | ✅ `Stop` `decision:block` | ⚠️ **degrade** — no ask-tool hook | `~/.codex/hooks.json` or `config.toml [hooks]` |
| **Copilot CLI** | ✅ `preToolUse`/`permissionRequest`(bash) `behavior:deny` | ✅ `agentStop` `decision:block` | ✅ `preToolUse`(`ask_user`) deny | `.github/hooks/*.json` |
| **Cursor** | ✅ `beforeShellExecution` `permission:deny` | ⚠️ **degrade** — `stop` can't block (only `followup_message`) | ⚠️ **degrade** — no ask hook | `.cursor/hooks.json` |
| **Windsurf** | ✅ `pre_run_command` (exit code **2**) | ⚠️ **degrade** — no blocking stop event | ⚠️ **degrade** — no ask hook | `~/.codeium/windsurf/hooks.json` |

Legend: ✅ native enforcement available · ⚠️ degrade to prose-contract + loud blind-spot.

**Reality:** deny-irreversible is enforceable everywhere. never-stop everywhere except Cursor/Windsurf.
never-ASK only on Claude + Copilot (only those expose an askable tool with a pre-hook). The degrade
cells are exactly why "best-effort + graceful degradation" was chosen — and why the status must be
reported, never assumed.

## Output contracts per platform (how to DENY / BLOCK)

- **Claude** — `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}`; Stop: `{"decision":"block","reason":"…"}`. (Already implemented as 3 bash hooks — kept as-is.)
- **Gemini** — stdin has `tool_name`,`tool_input`; deny: `{"decision":"deny","reason":"…"}` (or exit 2 + stderr). End-of-turn event `AfterAgent`, same deny shape = retry.
- **Codex** — stdin `tool_name`,`tool_input.command`; deny: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}` (Claude-compatible) or legacy `{"decision":"block","reason":"…"}`+exit 2. Stop: `{"decision":"block","reason":"…"}`.
- **Copilot** — stdin camelCase `toolName`,`toolArgs`; deny: `{"permissionDecision":"deny","permissionDecisionReason":"…"}`; `permissionRequest`: `{"behavior":"deny","message":"…","interrupt":true}`; `agentStop`: `{"decision":"block","reason":"…"}`.
- **Cursor** — stdin `command`,`cwd`; `beforeShellExecution` out: `{"permission":"deny","agent_message":"…","user_message":"…"}` (or exit 2). `stop` cannot block (only `followup_message`).
- **Windsurf** — stdin `tool_info.command_line`; `pre_run_command` blocks via **exit code 2** (stderr → reason). No blocking stop event.

## Architecture

Three units, each one purpose, testable in isolation:

1. **`agents_never_sleep/enforcement.py` — the decision CORE (platform-neutral, pure).** Single source of truth
   for: the irreversible-command patterns, the set of ask-tool names (`AskUserQuestion`, `ask_user`),
   the canonical deny/block reason strings, and pure functions `is_irreversible(cmd)->(bool,kind)`,
   `is_ask_tool(name)->bool`, and `decide(event, tool_name, command, sentinel_present)->Decision`.
   No platform I/O. Tested once, exhaustively.

2. **`agents_never_sleep/enforce.py` — the cross-platform DISPATCHER (CLI).** `python3 -m agents_never_sleep.enforce
   <platform> <event>` reads the platform's stdin JSON, NORMALISES it to (tool_name, command),
   calls the core `decide()`, and emits THAT platform's deny/block shape (or exit code). Env-gated to
   `CLAUDE_UNATTENDED=1` (rename-agnostic: also honours `UE_UNATTENDED=1`). One dispatcher, one place
   to test each platform's I/O translation. The proven Claude bash hooks stay as-is (no risky refactor
   of working security code); enforce.py serves the NEW platforms. The irreversible patterns live in
   enforcement.py as canonical; deny_irreversible.sh's copy is documented as a known duplicate to
   converge later.

3. **`agents_never_sleep/capabilities.py` — capability detection + degradation reporting.** The matrix above as
   data: `guarantees(platform) -> {deny_irreversible, never_stop, never_ask: native|degraded}`, plus
   `degradation_notes(platform) -> [blind-spot strings]` for the guarantees that fall back to prose.
   The driver/report surface these as startup notes + morning-report BLIND SPOTs (reusing the
   `build_report(notes=…)` channel from the secret-redaction slice).

**Install:** per-platform config snippets under `hooks/platforms/<platform>/` + a generalized
`AGENTS.md` router and install docs. All opt-in, env-gated — inert until the user wires them.

## Testing (hermetic, the achievable bar)

- `test_enforcement.py` — the core: irreversible patterns (force-push, rm -rf /, destructive SQL, …),
  ask-tool recognition, benign-allow, reason-string content.
- `test_enforce_platforms.py` — for EACH platform: feed its DOCUMENTED stdin JSON → assert the dispatcher
  emits that platform's correct deny/block shape (or exit 2) for (a) an irreversible command, (b) an ask
  tool where supported, (c) a stop with the sentinel present where supported; and ALLOWS a benign command;
  and is inert when not unattended.
- `test_capabilities.py` — matrix correctness + degradation notes for the ⚠️ cells.

## Sources (researched 2026-06)
- Gemini CLI hooks: https://geminicli.com/docs/hooks/reference/
- Codex CLI hooks: https://developers.openai.com/codex/hooks
- Copilot CLI hooks: https://docs.github.com/en/copilot/reference/hooks-configuration
- Cursor hooks: https://cursor.com/docs/hooks.md
- Windsurf Cascade hooks: https://docs.windsurf.com/windsurf/cascade/hooks (→ docs.devin.ai/desktop/cascade/hooks)
