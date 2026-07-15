# Crush enforcement adapter (dispatcher / shell hook)

Crush (charmbracelet) has a `PreToolUse` hook in `crush.json` that runs as a **shell command
before the permission check** — so a deny blocks the tool even under `crush run --yolo`. That
makes it a standard ANS **dispatcher** platform: the hook calls `enforce.sh crush pre_tool`,
which forwards Crush's stdin JSON to the shared dispatcher, which DENIES via exit 2 + a stderr
reason.

## Capability matrix row

| Guarantee | Status | How |
|---|---|---|
| deny-irreversible | ✅ native | `PreToolUse` runs before permissions; an irreversible command (matched on `tool_input.command`) → exit 2 → blocked, even under `--yolo`. |
| never-stop | 🟡 soft-enforced | Crush has only a `PreToolUse` hook — no Stop/end-of-turn veto (its `halt` does the *opposite*). never-stop is the ANS driver relaunching around `crush run` (the run-incomplete sentinel), reported as a BLIND SPOT. |
| never-ASK | 🟡 soft-enforced | enforced by the headless `crush run --yolo` launch flag (no permission prompts), not a pre-ask hook deny — same class as aider, so soft. |

## Install (opt-in, env-gated)

1. `agents_never_sleep` must be importable (`python3 -m agents_never_sleep.enforce` resolves). No
   `chmod +x` needed — the wired command is `bash <SKILL_DIR>/hooks/enforce.sh crush pre_tool`,
   invoked explicitly via `bash`.
2. Merge `crush.json`'s `hooks` block into your Crush config; replace `<SKILL_DIR>` with the
   absolute path to this skill.
3. Launch unattended with `crush run --yolo "<prompt>"` and `UE_UNATTENDED=1` set (the dispatcher
   is inert without it, so interactive Crush is untouched). For never-stop, the ANS driver must
   relaunch on the run-incomplete sentinel.

## Verification status

Built to Crush's documented hook contract (`docs/hooks/README.md` + `internal/hooks`) and proven
hermetically (`acceptance/test_enforce_platforms.py`: the crush payload denies an irreversible
command with exit 2, allows a benign one). **Not** in `LIVE_VERIFIED` — a live smoke-test on the
real `crush` CLI is maintainer-side (Crush is not installed here).

## Notes

- Deny payload reuses the default dispatcher shape: Crush's stdin JSON is
  `{"tool_name":"bash","tool_input":{"command":"…"}}` — the dispatcher reads `tool_input.command`.
- The hook deliberately has **no `matcher`** so it fires for every tool — a deny can't silently
  no-op on a shell tool whose exact name we didn't anticipate (commandless tools are allowed, so
  screening all is safe). If you add a `matcher` for performance, **confirm Crush's shell tool's
  exact name is included** (verify against your Crush version) or the guard won't fire on it.
- License: Crush is **FSL-1.1-MIT** (source-available, converts to MIT over time) — note for any
  OSS-strict consumer.
- Do NOT make the hook emit exit 49 / `halt:true` — that halts the whole turn (the opposite of
  never-stop).
