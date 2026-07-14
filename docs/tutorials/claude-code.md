# Tutorial — Claude Code end-to-end (the live-verified platform)

> **30-second version.** Claude Code is the one platform where the full ANS enforcement chain is
> **live-verified** (the others are built to their documented hook contracts — see
> [other platforms](other-platforms.md)). This page walks one complete cycle: install → first
> interactive run (the wizard) → trust the config → write a small backlog → launch detached with
> `bin/ans-run` → read the run report. Every command below runs as written against v1.0.0.

## 1. Install

```bash
pip install agents-never-sleep
```

or, pinned to a tagged GitHub release:

```bash
pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.0.0
```

or, hacking on a checkout:

```bash
git clone https://github.com/TokonoMix/agents-never-sleep
cd agents-never-sleep && pip install -e .
```

Verify:

```bash
python3 -c "import agents_never_sleep; print(agents_never_sleep.__version__)"
```

## 2. First interactive run — let the wizard write the config

Config is **per project**, at `<repo>/.claude/agents-never-sleep.json`. It records the choices a
run must never ask about at 2am: gates, budgets, autonomy, integrations, launcher presets. The
wizard is interactive-only by design — the skill never schedules itself, so a human is always
present before the first unattended run can exist.

From your project repo, ask the harness for work once, interactively:

```bash
python3 -m agents_never_sleep.run next --repo . --tickets tickets
```

No config yet → the wizard runs and writes conservative defaults you then confirm or edit. The
two decisions worth attention:

- **Gates** — the deterministic command(s) that decide DONE (e.g. your test suite). The gate is
  the *only* hard arbiter of a ticket; pick something that actually fails when the work is wrong.
- **The agent preset** — for Claude Code the unattended invocation is:

  ```json
  "launcher": {
    "default_agent": "claude",
    "agents": {
      "claude": { "cmd": ["claude", "-p", "--permission-mode", "acceptEdits"],
                  "autonomy_confirmed": true }
    }
  }
  ```

  `acceptEdits` auto-approves **file edits only** — shell/network tools stay under your normal
  permission rules. Setting `autonomy_confirmed: true` is the explicit human decision the launcher
  requires; an unconfirmed preset refuses to launch detached (a deliberate NO-GO instead of a
  silent stall).

**Pre-authorize deny-list classes (optional).** The wizard then asks, once per deny-list class,
whether to pre-authorize it for this run (default no). Consent applies to the WHOLE run across
every reachable target of that class — there's no per-call check — so only say yes for a
high-blast-radius class (`redis_flush`, `send_email`, …) on a run confined to a non-critical
environment. Consent is stored out-of-repo and frozen into the run; see
[glossary](../glossary.md#security).

## 3. Trust the config (TOFU)

`.claude/agents-never-sleep.json` travels with the repo and describes commands the launcher will
execute — so a new or changed config must be trusted once per user:

```bash
bin/ans-run --repo . --trust
```

Trust is recorded **outside the repo** (`~/.config/agents-never-sleep/trusted.json`, keyed on the
config's SHA-256): the repo cannot vouch for itself. Headless + untrusted = NO-GO.

## 4. Write a small backlog

A ticket is one Markdown file in a directory, with minimal frontmatter and a body that says what
to build and how green is judged:

```markdown
---
id: fix-csv-header
title: CSV export writes the header row twice
blast_radius: local
---
Repro: tests/test_export.py::test_header (currently red).
Fix the duplicate header in export/csv.py. Green = that test passes and the suite stays green.
```

Optional keys: `agent:` (a preferred CLI, metadata only) and `paperclip:` (a board issue id —
with `integrations.paperclip.write_enabled` the harness then syncs per-ticket status to the board
as the run goes). Shaping guidance: [workflow patterns](workflow-patterns.md).

## 5. Launch detached

```bash
bin/ans-run --repo /abs/path/to/repo "Drive the backlog in ./tickets to completion."
```

The launcher is a deterministic GO/NO-GO gate **before** the first token is spent: config trust,
identity/root-guard, preset + 5s `--version` capability probe, credentials, repo health, disk,
your configured host checks. Exit codes: `0` started/GO · `64` NO-GO · `65` working tree busy
(another run holds the `flock` on `.unattended/ans-run.lock`). Useful flags: `--check` (probe
only), `--fg` (foreground, exit code propagates — cron-friendly), `--agent <preset>`.

Two environment notes for scheduled launches (details: [unattended scheduling](unattended-scheduling.md)):

- `CLAUDE_UNATTENDED=1` arms the enforcement hooks (below); set it in your cron/systemd wrapper.
- If the agent's CWD can differ from `--repo`, export
  `UE_RUN_INCOMPLETE=<repo>/.unattended/run-incomplete` so the Stop-hook and driver agree on the
  sentinel path — `next` hard-fails rather than let never-stop silently break.

## 6. What the hooks enforce (this is the live-verified part)

Three thin hooks under `hooks/`, all **env-gated to `CLAUDE_UNATTENDED=1`** (inert in your normal
interactive sessions), registered via Claude Code's hooks config:

| hook | event | what it does |
|---|---|---|
| `deny_ask.sh` | PreToolUse (`AskUserQuestion`) | DENIES asking the human — steers the agent back into PARK/PROCEED; nobody answers at 2am |
| `deny_irreversible.sh` | PreToolUse (`Bash`) | DENIES the irreversible class: force-push, remote deletes, destructive SQL, `rm -rf /`, … |
| `stop_guard.sh` | Stop | BLOCKS a premature stop while `.unattended/run-incomplete` exists — the never-stop guarantee |

The `deny_irreversible.sh` row's list is representative, not exhaustive: it also denies sending real
mail, Vault secret-path writes, mass Redis flush, docker volume deletes, package/release publishing,
and infra teardown. Conversely, `systemctl stop`/`restart`, a bare `docker rm`, `docker push`, and
`redis-cli get`/`set` are ALLOWED — they aren't in the enumerated class. This deny-list floor can also
be selectively lifted per class via the run-setup consent step (§2) — see
[glossary](../glossary.md#security) — so don't read this table as absolute.

The agent drives the loop itself: `next` → implement → `complete` → `next` … until a terminal
status. The harness owns snapshot-before-edit, gate-after-edit, revert-on-red, attempt caps, loop
detection and the low-yield breaker.

## 7. Read the run report

A terminal `next` (DRAINED/HALTED/LOW_YIELD) writes the report (`report.local_path`, default
`night-report.md` — the artifact name; runs happen at any hour). It groups every ticket by
[outcome state](../state-machine.md#the-seven-outcome-states) — DONE, DONE_LOW_CONFIDENCE (done
but flagged for daylight review), PARKED_DECISION / PARKED_FOUNDATIONAL (with the recorded *why*
and the exact human next-action), BLOCKED_ENV, FAILED_*. Work lands on a dedicated run branch
(`ans/run-<timestamp>-<pid>`) with `pre:`/`done:` commits per ticket and **nothing is ever
pushed** — review the branch, then merge on your own judgment.

That is the whole contract: you shape the backlog and review the morning after; the run never
stalls on a question in between.
