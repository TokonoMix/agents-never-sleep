# Watchdog — supervising the parent run (opt-in)

The Stop-hook prevents a premature *stop*, but it cannot see a *hang* (no progress is not a stop).
`agents_never_sleep/watchdog.py` is a standalone sidecar that runs the unattended command as a child, polls its
heartbeat file, and restarts it resumable when the heartbeat goes stale — then alerts once restarts
are exhausted.

## Why a sidecar and not a claude-run rewrite

`claude-run` is shared production infra; your crons depend on it. We do **not** modify
it. Instead the watchdog is invoked *around* a command, and you opt in by composing it — never by
editing the wrapper.

## Standalone use

```
python3 -m agents_never_sleep.watchdog \
  --heartbeat /path/project/.unattended/state/heartbeat.json \
  --stale 2400 --max-restarts 3 \
  --alert 'curl -s -X POST -H "Authorization: Bearer $PCP" .../issues -d @alert.json' \
  -- claude-run -p "work the backlog with the agents-never-sleep skill"
```

- `--stale 2400` — no heartbeat for this long = hung. **It MUST exceed the worst-case single-ticket
  work time.** The agent beats the heartbeat only at each `next`/`complete` boundary; in between it
  implements one ticket, which can take the full `per_ticket_timeout_s` (default 1800s). Set
  `--stale ≥ per_ticket_timeout_s + gate time + margin`, or the watchdog will false-restart a healthy
  run mid-ticket. (The child here is the AGENT loop — it drives `agents_never_sleep.run next`/`complete`
  itself; the agent IS the worker.)
- `--grace 300` — startup grace before staleness counts.
- `--max-restarts 3` — resumable restarts (the run skips DONE/parked tickets and resumes the rest)
  before giving up. `--alert` runs with a hard `--alert-timeout` (30s) so giving up can't itself hang.
- exit `75` on exhaustion — matches `claude-run`'s "gave up" convention so existing tooling can react.

The watchdog sets `CLAUDE_UNATTENDED=1` and `UE_HEARTBEAT` for the child automatically.

## Composing with claude-run (opt-in, no edits)

Wrap your existing `claude-run` invocation as the child command, so you keep claude-run's 529/overload
resilience AND gain hang-detection:

```
python3 -m agents_never_sleep.watchdog --heartbeat <hb> --stale 2400 -- \
  claude-run -p "work the backlog with the agents-never-sleep skill"
```

If you later want claude-run to set `CLAUDE_UNATTENDED=1` itself (so the hooks activate for ALL
claude-run invocations), add a single opt-in line to its env block — a one-line, reversible change
you make deliberately, not something this skill does for you:

```
export CLAUDE_UNATTENDED="${CLAUDE_UNATTENDED:-1}"   # opt-in: activate agents-never-sleep enforcement
```
