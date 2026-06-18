# Cross-platform enforcement adapters (opt-in)

The autonomy contract — **never-ASK**, **deny-irreversible**, **never-stop** — is enforced at the code
layer so it does not rely on the agent's 2am judgment. The Claude adapter is the three bash hooks one
level up (`../*.sh`). This folder wires the **same decisions** into the other supported platforms via
one shared dispatcher: `python3 -m harness.enforce <platform> <event>` (launched by `../enforce.sh`).

**Strategy: best-effort + graceful degradation.** Each platform enforces what its hook system allows.
Where the host agent/CLI **provides no native hook for a guarantee and no alternative mechanism**
(a limitation of *that* platform, not of this skill), the skill does not give up: it catches the
guarantee as well as it can by falling back to the **SKILL.md prose contract** (the agent is
instructed to honour it) AND emits a loud **BLIND SPOT** in the morning report (via
`harness/capabilities.py`). A 🟡 soft-enforced guarantee is never silently dropped.

## Capability matrix (researched 2026-06 — see `../../references/cross-platform-enforcement-design.md`)

| Platform | deny-irreversible | never-stop | never-ASK |
|---|---|---|---|
| Claude Code | ✅ native | ✅ native | ✅ native |
| Gemini CLI | ✅ native | ✅ native | 🟡 soft-enforced (no pre-ask hook) |
| Codex CLI | ✅ native | ✅ native | 🟡 soft-enforced (no pre-ask hook) |
| Copilot CLI | ✅ native | ✅ native | ✅ native (`ask_user` tool) |
| Cursor | ✅ native | 🟡 soft-enforced (stop can't block) | 🟡 soft-enforced (no pre-ask hook) |
| Windsurf | ✅ native | 🟡 soft-enforced (no stop event) | 🟡 soft-enforced (no pre-ask hook) |

`deny-irreversible` works on every platform. The 🟡 soft-enforced cells are reported as blind spots at run end.

## Install (per platform — all opt-in, all env-gated)

1. `chmod +x <SKILL_DIR>/hooks/enforce.sh` (once).
2. Copy the matching config into that platform's hooks location and **replace `<SKILL_DIR>`** with the
   absolute path to this skill:
   - **Gemini CLI** → merge `gemini/settings.json` into `~/.gemini/settings.json`
   - **Codex CLI** → `codex/hooks.json` → `~/.codex/hooks.json` (or `config.toml [hooks]`)
   - **Copilot CLI** → `copilot/agents-never-sleep.json` → `<repo>/.github/hooks/`
   - **Cursor** → `cursor/hooks.json` → `<project>/.cursor/hooks.json`
   - **Windsurf** → `windsurf/hooks.json` → `~/.codeium/windsurf/hooks.json`
3. The dispatcher is inert unless `UE_UNATTENDED=1` (or `CLAUDE_UNATTENDED=1`) is set — so once wired,
   it does nothing during your normal interactive sessions. Set it (and optionally `UE_PLATFORM=<id>`
   for accurate degradation reporting) only for real unattended runs.
4. For `never-stop` to work, the platform must see the run-incomplete sentinel — export
   `UE_RUN_INCOMPLETE=<repo>/.unattended/run-incomplete` (the driver writes that path).

## Verification status

These adapters are built to each platform's **documented** hook contract and proven hermetically by
`acceptance/test_enforce_platforms.py` (correct stdin→deny/block shape per platform). A **live
smoke-test on each real tool** is the remaining manual step — run an unattended job on the platform,
confirm an irreversible command is blocked and (where native) a stop is blocked / an ask is denied.
