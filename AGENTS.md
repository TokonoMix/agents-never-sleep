# AGENTS.md — router

This directory is an [Agent Skill](https://www.agensi.io/learn/agent-skills-open-standard)
(`SKILL.md`), the open cross-tool standard read by Claude Code, OpenAI Codex CLI, Gemini CLI,
GitHub Copilot, Cursor and 30+ other tools. This file lets file-based agents discover and route
to it.

## Skill index

| Skill | Use it when | Entry |
|-------|-------------|-------|
| **agents-never-sleep** | Handing off a backlog of tickets / a milestone to run UNATTENDED (overnight, "while I'm away", "keep going till it's done"), or a long autonomous run keeps stalling on questions. | `SKILL.md` |

## How to run

```
python3 -m agents_never_sleep.run --repo <project> --tickets <dir-of-.md-tickets>
```

(Run from this skill directory so the `harness` package is importable, or add it to `PYTHONPATH`.)

## Platform notes

The harness (durable state machine, gates, park-semantics, reversibility, budgets) is
provider-neutral Python and runs anywhere. Enforcement (never-ASK / deny-irreversible / never-stop)
is wired per platform from ONE shared decision core (`agents_never_sleep/enforcement.py`) via the dispatcher
`python3 -m agents_never_sleep.enforce <platform> <event>`. All adapters are opt-in and env-gated
(`UE_UNATTENDED=1` / `CLAUDE_UNATTENDED=1`):

- **Claude Code** — three bash hooks in `hooks/` (Stop-guard, deny-irreversible, deny-ask); see
  `hooks/README.md`.
- **Gemini CLI / Codex CLI / Copilot CLI / Cursor / Windsurf** — config snippets + per-platform
  install in `hooks/platforms/` + the capability matrix. Strategy is **best-effort + graceful
  degradation**: each platform enforces what its hook system allows; a guarantee with no native hook
  falls back to the `SKILL.md` prose contract AND is reported as a morning-report BLIND SPOT
  (`agents_never_sleep/capabilities.py`) — never silent. `deny-irreversible` is native everywhere; `never-stop`
  everywhere except Cursor/Windsurf; `never-ASK` on Claude + Copilot only.

Set `UE_PLATFORM=<gemini|codex|copilot|cursor|windsurf>` on non-Claude runs so degradation reporting
names the right platform.

## Acceptance

`python3 acceptance/run_acceptance.py` must exit 0 (the 3-ticket demo). `python3
acceptance/test_resume.py` proves cross-resume loop detection.
