# opencode enforcement adapter (dispatcher / JS plugin)

opencode (sst) has a `tool.execute.before` plugin hook that fires before a tool runs and blocks
it when the hook **throws**. ANS reuses the shared dispatcher: the plugin (`ans-enforce.js`)
shells to `python3 -m agents_never_sleep.enforce opencode pre_tool` and throws on a deny (exit 2
+ stderr reason). So opencode is a **dispatcher** platform whose hook happens to be JS.

## Capability matrix row

| Guarantee | Status | How |
|---|---|---|
| deny-irreversible | ✅ native\* | `throw` in `tool.execute.before` hard-blocks the tool; the command string is read from `output.args.command`. **\*CAVEAT:** subagent (`task`-tool) calls bypass this hook (upstream sst/opencode#5894) — deny covers PRIMARY-agent calls only. Recorded in the hook contract so the run report surfaces it. |
| never-stop | 🟡 soft-enforced | `session.idle` is observe-only — no stop veto. never-stop is the ANS driver relaunching around `opencode run` (the run-incomplete sentinel), reported as a BLIND SPOT. |
| never-ASK | 🟡 soft-enforced | enforced by the headless `opencode run` auto-approve flag (`--skip-permissions`/`--auto-approve` — verify the exact spelling on your version), not a pre-ask hook deny — same class as aider, so soft. |

## Install (opt-in, env-gated)

1. `agents_never_sleep` must be importable in the env that runs opencode
   (`python3 -m agents_never_sleep.enforce` resolves).
2. Drop `ans-enforce.js` in opencode's plugin dir — project `.opencode/plugin/` or global
   `~/.config/opencode/plugin/` (**verify the exact dir name** `plugin` vs `plugins` on your
   opencode version), or reference it via `opencode.json` `"plugin"`.
3. Launch unattended with `opencode run "<prompt>"` + the auto-approve flag, with `UE_UNATTENDED=1`
   set (the plugin is inert without it). For never-stop, the ANS driver relaunches on the sentinel.

## Verification status / verify-before-ship

Built to opencode's documented plugin contract (`tool.execute.before`, `input.tool`,
`output.args.command`, throw-to-deny — all from opencode.ai/docs/plugins) and proven hermetically
via the dispatcher test. **Not** in `LIVE_VERIFIED` (opencode is not installed here). Confirm on a
real install (all non-blocking, ~5 min each):
- the plugin dir name (`plugin` vs `plugins`);
- the exact `opencode run` auto-approve flag spelling;
- whether `@opencode-ai/plugin` exposes a returnable `stop` hook (would upgrade never-stop to native).

## Notes

- The plugin sends the **default dispatcher payload** `{"tool_name", "tool_input":{"command"}}`,
  so no opencode-specific `_normalize` is needed — it reuses the same path as Crush/Claude.
- Discoverability: opencode has a public npm plugin ecosystem (`@opencode-ai/plugin`) + the
  `awesome-opencode` list — an ANS opencode plugin is the Tier-2 listing target.
- **Subagent bypass (#5894)** is the one correctness caveat — disclosed loudly here + in the
  recorded hook contract.
