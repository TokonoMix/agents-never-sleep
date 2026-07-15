// ANS enforcement plugin for opencode (sst).
//
// opencode's `tool.execute.before` hook fires before a tool runs and blocks it when the hook
// THROWS. This plugin reuses the shared dispatcher instead of re-implementing decide() in JS:
// it hands (tool_name, command) to `python3 -m agents_never_sleep.enforce opencode pre_tool` on
// stdin and throws when the dispatcher denies (exit 2 + stderr reason).
//
// Install: drop this file in opencode's plugin dir (project `.opencode/plugin/` or global
// `~/.config/opencode/plugin/` — verify the exact name against your opencode version), OR
// reference it via `opencode.json` "plugin". Inert unless UE_UNATTENDED=1 / CLAUDE_UNATTENDED=1,
// so interactive opencode is untouched.
//
// CAVEAT (upstream sst/opencode#5894): `tool.execute.before` does NOT intercept tool calls made
// by SUBAGENTS spawned via the `task` tool — this deny covers PRIMARY-agent tool calls only.
// Run reported as a BLIND SPOT via capabilities.py's recorded hook contract.

export const AnsEnforce = async ({ $ }) => {
  return {
    "tool.execute.before": async (input, output) => {
      // Inert outside an unattended run (also avoids spawning python on every interactive call).
      if (process.env.UE_UNATTENDED !== "1" && process.env.CLAUDE_UNATTENDED !== "1") return;

      // Build the default dispatcher payload: only bash carries a shell command to screen.
      const payload = JSON.stringify({
        tool_name: input?.tool,
        tool_input: { command: output?.args?.command ?? "" },
      });

      // Shell to the shared dispatcher; exit 2 = deny (stderr = reason), exit 0 = allow.
      const res = await $`python3 -m agents_never_sleep.enforce opencode pre_tool`
        .stdin(payload)
        .nothrow()
        .quiet();

      if (res.exitCode === 2) {
        const reason = (res.stderr?.toString?.() || "").trim();
        throw new Error(reason || "agents-never-sleep: blocked an irreversible/outward action");
      }
      // exit 0 (or any non-2 / crash) → allow (fail OPEN — enforcement must never wedge a call).
    },
  };
};
