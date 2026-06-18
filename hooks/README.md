# Claude Code enforcement adapter (opt-in)

Three hooks make the autonomy contract real at the code layer instead of relying on the agent's
judgment (which is exactly what fails at 2am). All are **env-gated to `CLAUDE_UNATTENDED=1`**, so
once wired they stay completely inert during your normal interactive sessions.

- `stop_guard.sh` — **Stop hook.** Blocks a premature end-of-turn while a run-incomplete sentinel
  (`.unattended/run-incomplete`) exists, so the agent keeps working the backlog instead of
  soft-halting. Honours `stop_hook_active` → can never loop.
- `deny_irreversible.sh` — **PreToolUse hook (matcher `Bash`).** Blocks genuinely irreversible /
  outward actions (force-push, remote branch/tag delete, destructive SQL, disk-destructive
  commands, Vault secret delete/rotate, sending real email, service/volume teardown). It
  deliberately does **not** block the harness's own local `git reset --hard` / `git clean` revert
  mechanism.
- `deny_ask.sh` — **PreToolUse hook (matcher `AskUserQuestion`).** Makes "never ASK" structural,
  the mirror of never-stop: there is nobody to answer at 2am, so a single question wastes the night.
  It DENIES `AskUserQuestion` and returns a reason that re-routes the agent into the contract — PARK
  this decision (record why, candidate interpretations, the exact human next-action, contamination
  scope) and PROCEED to the next ticket; HALT only on irreversible danger. Fails open (allows) on a
  malformed payload so it can never wedge an unrelated tool.

## Why this is not auto-installed

These hooks live in your **global** `~/.claude/settings.json` and therefore affect *every* Claude
Code session. Enabling them is a deliberate, reversible step you take when you are ready to test —
not something to flip while you are away. They are env-gated, so even once wired they do nothing
unless `CLAUDE_UNATTENDED=1` is set (which `claude-run`/cron sets for real unattended runs).

## Install (when you are ready to test)

1. Make the hooks executable:
   ```
   chmod +x ~/.claude/skills/agents-never-sleep/hooks/*.sh
   ```
2. Merge `hooks/settings-snippet.json` into the `"hooks"` block of `~/.claude/settings.json`
   (it is currently `{}`). Keep any existing hooks.
3. Restart Claude Code so the hooks load.
4. Verify they are inert normally (no `CLAUDE_UNATTENDED`) and active under an unattended run.

## Uninstall

Remove the two entries from the `"hooks"` block and restart. Nothing else persists.
