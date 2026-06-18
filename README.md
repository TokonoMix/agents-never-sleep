# Agents Never Sleep (ANS)

Run a backlog to completion **unattended** — without the run ever soft-halting on a question.
Overnight is the obvious case, but it works just as well **during the day**: hand off the backlog
and carry on with other work while it runs, without watching the process or approving every step. A
portable [Agent Skill](https://www.agensi.io/learn/agent-skills-open-standard)
(`SKILL.md`) plus a small stdlib-Python harness that gives a coding agent: durable per-ticket state,
a concrete **ASK / PARK / HALT** autonomy contract (never block the whole run on one ticket),
deterministic test-gates with a failure taxonomy, git-backed reversibility, attempt/loop caps, an
optional multi-model review council, secret redaction on every write, and a run report.

> The pain it solves: *"the agent stalls on a dumb question and wastes the stretch you weren't
> watching"* — whether that's overnight or the hour you stepped away to do something else.

## The autonomy contract

Three responses to uncertainty — never collapsed:

- **PROCEED** — assume + log + continue (low blast-radius, reversible).
- **PARK** — defer *this* ticket/decision, keep moving to the next. Normal and healthy.
- **HALT** — stop the whole run (only on irreversible danger with no safety net).
- **ASK is forbidden unattended** — there is nobody watching to answer.

This is enforced structurally (not by agent discipline) via opt-in, env-gated hooks.

## Quick start

```bash
# 1. (Claude Code) install the enforcement hooks — opt-in, see hooks/README.md
#    other platforms: hooks/platforms/README.md
# 2. headless/cron? start through the launcher: GO/NO-GO preflight BEFORE any token is
#    spent + an atomic per-working-tree lock (two simultaneous starts -> one winner).
#    Agent choice = named, wizard-confirmed presets (--agent); repo configs are
#    trust-on-first-use (`--trust`); autonomy flags are never applied silently:
bin/ans-run --repo <project> --agent claude "work through the backlog unattended"
# 3. drive the per-ticket loop (the agent IS the worker):
python3 -m harness.run next     --repo <project> --tickets <dir-of-.md-tickets>
python3 -m harness.run complete --repo <project> --attempted "what you did"
#    repeat next/complete until it returns DRAINED/HALTED/LOW_YIELD.
```

`next` hands you exactly one ready ticket (auto-parking ambiguous / high-blast-radius ones) and owns
the never-stop sentinel; `complete` gates + records the outcome. Both echo the resolved repo + tickets
paths so a mis-target is obvious. Operator escapes when a resume gets confused: `reset-attempts <id>`
(clear one ticket's attempt counter) and `reset-spend` (zero the per-night spend accounting). See
`SKILL.md` for the full loop, the council/specialist review flow, and the cost brakes.

## Cross-platform

The harness is provider-neutral Python and runs anywhere. The cross-platform enforcement (never-ASK /
deny-irreversible / never-stop) is wired per platform from one shared decision core
(`harness/enforcement.py`) via `python3 -m harness.enforce <platform> <event>`. (Claude Code *also*
ships a self-contained bash deny-hook, `hooks/deny_irreversible.sh`, as the `bypassPermissions`
backstop — it mirrors the same patterns but does not call the Python core, so it keeps working even
if Python is unavailable.)

| Platform | deny-irreversible | never-stop | never-ASK |
|---|---|---|---|
| Claude Code | ✅ | ✅ | ✅ |
| Gemini CLI | ✅* | ✅* | 🟡 soft-enforced |
| Codex CLI | ✅* | ✅* | 🟡 soft-enforced |
| Copilot CLI | ✅* | ✅* | ✅* |
| Cursor | ✅* | 🟡 soft-enforced | 🟡 soft-enforced |
| Windsurf | ✅* | 🟡 soft-enforced | 🟡 soft-enforced |

**✅ native + live-verified** = the platform's own hook enforces the guarantee at the tool layer, and
we have verified it on the real tool (today only Claude Code — `capabilities.py: LIVE_VERIFIED`).
**✅\*** = native by the platform's documented hook contract, but **not yet live-verified** on the
real tool (run `acceptance/` there to promote it). **🟡 soft-enforced** = the platform provides **no
native hook for this guarantee** at the tool layer — a limitation of the host agent/CLI, **not of
this skill**. Where that happens the skill does **not** give up: it falls back to the **SKILL.md prose
contract** (the agent is explicitly instructed to honour the guarantee) and surfaces any residual gap
as a loud **BLIND SPOT** in the run report — never silent. Install snippets per platform live in
`hooks/platforms/`.

## Layout

```
SKILL.md                     the portable skill (read by the agent)
AGENTS.md                    router for file-based agents
bin/ans-run                  launcher: pre-token GO/NO-GO preflight + atomic working-tree lock,
                             TOFU config trust, known-CLI allowlist, capability probe
harness/                     stdlib-Python engine (state machine, gates, driver, council, …)
  enforcement.py             shared cross-platform decision core
  enforce.py                 cross-platform hook dispatcher
  capabilities.py            per-platform capability matrix + degradation reporting
hooks/                       Claude bash hooks + platforms/ config snippets
acceptance/                  hermetic acceptance tests (run each test_*.py; exit 0 = green)
references/                  design docs
```

## Tokonomix consensus integration

When the [Tokonomix](https://tokonomix.ai) MCP server is connected and a council block is present
in a `PROCEED` payload, ANS drives the multi-model review via `tokonomix_consensus_ask` and
immediately rates the usefulness of each council call via `tokonomix_rate_consensus`.

**Unattended mode** (`CLAUDE_UNATTENDED=1`): ratings are submitted automatically — the run is
never blocked. On the first call the agent logs a single line:
`ℹ️  Tokonomix consensus active — auto-rating each council call via tokonomix_rate_consensus.`

**Interactive mode**: the agent asks once whether to auto-submit ratings; the answer is saved to
`.unattended/state/consensus_rating_pref.json` and honoured in all subsequent sessions.

The `review_reward` field returned by the rating endpoint is surfaced in the MCP response
(added in ANS v0.3.0 / tokonomix-council-mcp v1.5.2).

Source: [TokonoMix/agents-never-sleep](https://github.com/TokonoMix/agents-never-sleep)

## Tests

```bash
for t in acceptance/test_*.py acceptance/run_acceptance.py; do python3 "$t" >/dev/null && echo "$t ✅" || echo "$t ❌"; done
```

## License

MIT — see `LICENSE`.
