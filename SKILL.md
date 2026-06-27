---
name: agents-never-sleep
description: >-
  Run large, multi-step work UNATTENDED — a backlog of tickets or milestones — without stopping
  to ask the human trivial clarifying questions. Use this whenever the user hands off a big job to
  run overnight / "while I'm away" / "keep going till it's done", points you at a list of tickets or
  a milestone, or says "unattended", "don't stop and ask", "work through the backlog", or "finish
  this tonight". Also use when a long autonomous run keeps stalling on questions. It gives you a
  durable per-ticket state machine, an ASK/PARK/HALT autonomy contract (never block the run on a
  single ticket), deterministic-gate quality checks with a failure taxonomy, git-backed
  reversibility, attempt/loop caps, and a morning report — so the night never idles and nothing
  irreversible happens unsupervised. Make sure to reach for this even when the user does not say
  "unattended" but is clearly handing off long, autonomous, run-to-completion work.
---

# Agents Never Sleep (ANS)

Run a backlog to completion overnight without the run ever soft-halting on a question. The hard
part is not doing the work — it is **state durability + concrete park-vs-continue semantics +
anti-starvation**, so that is what this skill is built around. The reliability spine shipped first
(see `BUILD-PLAN.md`); the heavier multi-model review funnel was sequenced on top of it and is now
built (status in "What is deferred to Phase 2" below).

> Full rationale: `agents-never-sleep-design.md` (13 design threads + 3 council reviews).
> The MVP spine is implemented as a small Python harness under `harness/` and proven by
> `acceptance/run_acceptance.py` (the only test that maps to the "stops at 2am" pain).

## The autonomy contract — three distinct states, never collapsed

A run has exactly three responses to uncertainty. Keep them separate; collapsing them is how a run
inverts into the very stalling it exists to prevent.

- **ASK** — ask the human. **Forbidden while unattended.** Do not emit `AskUserQuestion`; there is
  nobody to answer at 2am, and a single question wastes the whole night. This is enforced
  structurally, not by discipline: the `deny_ask.sh` PreToolUse hook (matcher `AskUserQuestion`,
  env-gated `CLAUDE_UNATTENDED=1`) DENIES the tool and steers you back into PARK/PROCEED — the mirror
  of the never-stop guarantee.
- **PARK** — defer *this decision or ticket* and **keep the run moving** to the next independent
  ticket. Parking is normal and healthy, not a stop.
- **HALT** — stop the *whole run*. Only on genuinely irreversible danger that the hook layer would
  block anyway, or when there is no reversibility safety net at all (read-only fs, no VCS and none
  creatable).

When unattended you only ever choose PROCEED, PARK, or HALT. Never ASK.

## Decide PROCEED vs PARK by blast radius (so "unsure" is rare)

Make the tiering concrete — guessing on a high-blast-radius item and being confidently wrong all
night is the worst outcome, worse than parking.

**Hard-PARK (never guess):** DB schema / migration direction, public or shared API contract,
security / auth / tenant-isolation boundary, money / billing / pricing, a cross-ticket interface
others build on, and **requirement meaning** (you don't actually know *what* to build) unless it is
both locally reversible and isolated — then build it reversibly behind a flag / draft change AND
park the decision (hybrid).

**Proceed (assume + log + continue):** naming, internal structure, log / comment / error wording,
test fixtures, a choice between two equivalent local implementations, trivially-toggled defaults.

**When genuinely unclassifiable → PARK.** A wrongly-parked small item costs a 5-second morning
decision; a wrongly-assumed big one costs a night of wrong work.

Commit each PROCEED assumption so it can be reverted; if the snapshot commit cannot be made
(git lock / timeout / read-only object store), treat the ticket as `BLOCKED_ENV` rather than editing
unrevertibly. Every PARK must record: why, the candidate interpretations, the exact human
next-action, and its contamination scope.

## The per-ticket loop (what the harness enforces)

1. **Preflight measures capabilities** (`agents_never_sleep/preflight.py`) — VCS/reversibility, platform,
   gates, execution mode, tokonomix/Vault/Paperclip. Missing capability never stops the run; it
   lowers expected yield and raises conservatism. No VCS → establish a safety net (git init /
   timestamped backup) before any risky edit; if impossible → non-destructive only.
2. **Decide** ASK/PARK/HALT for the ticket (above). Unattended: ASK→PARK.
3. **Implement** the PROCEED ticket — *you, the agent, are the worker*: read the ticket the harness
   hands you and edit files. You drive this one ticket at a time via the two subcommands below; the
   harness owns scheduling, parking, snapshot/revert and the never-stop sentinel.
4. **Gate** deterministically (`agents_never_sleep/gates.py`) — the BACKBONE. Classify failures:
   introduced-by-the-diff → hard-block (revert to last green + park/fail); pre-existing / flaky /
   env → downgrade confidence, continue or park (never report as "the ticket failed"); timeout/env
   → BLOCKED_ENV. Every gate runs with a per-step timeout and a non-interactive environment so it
   can never hang on a TTY prompt. **Never delete or skip a failing test to go green** — that is a
   blocking blind-spot.
5. **Record** exactly one durable outcome (`agents_never_sleep/state.py`): DONE, DONE_LOW_CONFIDENCE,
   PARKED_DECISION, PARKED_FOUNDATIONAL, BLOCKED_ENV, FAILED_RETRYABLE, FAILED_BUG_IN_AGENT — with
   the required fields. Atomic writes; resume-safe.
6. **Next ticket.** Attempt + loop caps (`agents_never_sleep/ledger.py`) force-park a ticket that exceeds its
   cross-resume attempt cap or is provably looping, so the night is never burned on one cursed item.
   A low-yield circuit breaker stops and alerts if most work is parked/blocked.

## Running it — the agent IS the worker (drive this loop)

**Before starting from an interactive session, ask the user ONCE, in chat:** run the backlog
*here in this session* (foreground — it stops if they close the terminal), or launch a *detached*
run via `bin/ans-run` that survives logout? This is a setup/routing decision, not a mid-run
question — it does not violate the never-ASK contract, and it is the only question you ask.

**If tokonomix consensus mode is configured** (`council` block in PROCEED payloads, or a
`tokonomix_*` MCP tool is available): at run start:

- **Unattended (`CLAUDE_UNATTENDED=1`):** emit ONE log line and proceed immediately — never
  block or ask:
  `ℹ️  Tokonomix consensus active — auto-rating each council call via tokonomix_rate_consensus.`
- **Interactive (no `CLAUDE_UNATTENDED`):** if the account has no saved rating preference yet
  (i.e. `review_feedback_opt_in` not yet set — detectable because `feedback_invite` is absent
  from the first consensus response), ask the user ONCE in chat:
  > "Tokonomix consensus is active. After each council call I can auto-submit a usefulness
  > rating (score + findings) to improve model scores. Enable this? (yes/no)"
  Store the answer in `.unattended/state/consensus_rating_pref.json` so this is asked only
  once across sessions. If yes: proceed with auto-rating. If no: skip rating calls entirely.

If `consensus_rating_endpoint_enabled` returns 404 on the first rating attempt, log it once as
a blind-spot and skip all subsequent rating calls for this run.

The harness cannot call you from inside a Python loop, so you drive it. Alternate these two
subcommands until the backlog drains. Each prints one JSON object to stdout.

```
# 1. Ask the harness for the next thing to do. It auto-parks ambiguous/high-blast-radius tickets
#    and only ever hands you ONE ready-to-implement ticket, or a terminal signal.
python3 -m agents_never_sleep.run next --repo <project> --tickets <dir-of-.md-tickets>
```

Read the JSON `status`:
- **`PROCEED`** → implement ONLY `ticket.body` by editing files in the repo (do NOT touch other
  tickets, do NOT stop, do NOT ask). Then call `complete`:
  ```
  python3 -m agents_never_sleep.run complete --repo <project> --attempted "one-line summary of what you did"
  ```
  Use `--cannot-implement` (with `--attempted` explaining why) if you genuinely cannot do it — the
  harness reverts your partial edits and records BLOCKED_ENV. After `complete`, call `next` again.

  **If the `PROCEED` payload carried a `council` or `specialists` block, you ran reviews — you MUST
  feed the results back on `complete`, or the advisory trust-gating silently never fires:**
  `... complete --attempted "…" [--council-verdict pass|concerns|error --council-cost <€>] \
  [--specialist-concerns architect,security,… --specialist-cost <€>] [--review-coverage "<who ran>"]`.
  Omit a flag only when you ran no such review. (Details: the Council and Specialist sections below.)
- **`DRAINED` / `HALTED` / `LOW_YIELD`** → the run is over; the morning report is written. Stop.
- **`NON_DESTRUCTIVE`** → unattended with no saved config; do a configuring interactive run first.

**Never invent your own loop or stop early.** `next` owns the `.unattended/run-incomplete` sentinel
that the Stop-hook uses to block a premature 2am stop; keep calling `next` until it returns a
terminal status. The harness handles snapshot-before-edit, gate-after-edit, revert-on-red, the
attempt cap, loop detection and the low-yield breaker — your only job is the edits.

> **Sentinel path contract (load-bearing, enforced).** The Stop-hook checks
> `${UE_RUN_INCOMPLETE:-$PWD/.unattended/run-incomplete}` and the driver writes the same path. They
> agree automatically **only when the agent runs from the repo root** (so `$PWD == --repo`; use
> `--repo .`). For an unattended run where CWD may differ from `--repo` (cron / `claude-run`),
> export `UE_RUN_INCOMPLETE=<repo>/.unattended/run-incomplete` at launch. To stop never-stop from
> breaking silently, `next` **hard-fails** (`status:"ERROR"`, exit 2) when unattended + CWD ≠
> `--repo` + `UE_RUN_INCOMPLETE` unset — fix the launch (cd into the repo, or set the env var).

First interactive run triggers a minimal per-project wizard (`.claude/agents-never-sleep.json`).
The skill never schedules itself — running unattended (cron / `claude-run`) is always the user's
explicit act, so the wizard always gets to run first. Unattended with no config → non-destructive
only + a loud note in the report.

> Legacy `python3 -m agents_never_sleep.run run` drives the loop in-process with a deterministic Worker; it is
> only for the hermetic acceptance demo. Real runs use the `next`/`complete` flow above.

## Launching a run — `bin/ans-run` (pre-token preflight + working-tree lock)

`agents_never_sleep/preflight.py` measures capabilities AFTER the agent session boots — by then the first
tokens are already spent and a doomed run has already cost money. For headless/cron launches,
start through the launcher instead:

```
bin/ans-run [--repo <project>] [--agent <preset>] [--fg] [--check] [--trust] "<prompt>"
```

It is a deterministic GO/NO-GO gate that runs BEFORE the agent CLI boots:

- **config trust (TOFU)** — `.claude/agents-never-sleep.json` travels with the repo and describes
  commands the launcher executes (agent argv, host checks). A new or changed config must be
  trusted once per user — interactively at the prompt, or explicitly via `ans-run --trust` after
  review. Headless + untrusted = NO-GO. Trust is recorded outside the repo
  (`~/.config/agents-never-sleep/trusted.json`, keyed on the config's SHA-256) — the repo cannot
  vouch for itself. **Never run ans-run in a repo you trust less than its `make install`.**
- **identity** — configurable `launcher.target_user` + root-guard: started as root with a target
  user configured → re-exec as that user; as root with none configured → NO-GO (an unattended run
  must never own the night as root),
- **agent selection** — named presets under `launcher.agents`, picked by `--agent` or
  `launcher.default_agent`. NO platform detection happens at launch time (session env markers are
  spoofable and gone under cron); the wizard is where detection — as a prefill hint — and the
  human decision live. Three gates per preset: argv[0] must be a known agent CLI
  (`claude`/`codex`/`gemini`/`copilot`; override only via `launcher.allow_custom_agent` in a
  trusted config), the binary must pass a 5s `--version` **capability probe** (catches flag drift
  before tokens are spent), and `autonomy_confirmed` must be true (see below),
- **credentials** — `launcher.credentials_paths` is blocking when configured, warn-only when not
  (keychain and API-key setups have no credentials file),
- **repo health** — git usable (catches dubious-ownership), repo writable, disk space
  (`launcher.min_disk_mb`), dirty/staged tree surfaced as warnings,
- **host checks** — services/DB probes come EXCLUSIVELY from `launcher.checks`
  (`[{"name", "command", "blocking"}]`); nothing site-specific is hardcoded in the launcher.

Exit codes: `0` started/GO · `64` NO-GO · `65` working tree busy.

### Autonomy flags are an explicit human decision — never a default

A detached run with the CLI's permission system fully on stalls at its first approval prompt
(stdin is closed, nobody is watching); the flag that prevents that grants real power. The shipped
map (`agents_never_sleep/agent_clis.py`, single source for wizard + launcher) keeps both variants apart and
the wizard shows what the flag grants before asking you to confirm — only then does the preset
record `autonomy_confirmed` and become launchable:

| CLI | unattended invocation | the flag grants |
|---|---|---|
| Claude Code | `claude -p --permission-mode acceptEdits` | file edits auto-approved; shell/network stay gated |
| Codex | `codex exec --sandbox workspace-write` | edits/commands inside the workspace sandbox |
| Gemini | `gemini --yolo -p` | EVERYTHING — run in a container/VM or throwaway checkout |
| Copilot | `copilot --allow-all-tools -p` | everything (required for programmatic `-p`) |

A preset without `autonomy_confirmed` refuses to launch detached — a deliberate NO-GO instead of
a silent stall-and-burn, and instead of nudging users to google a bypass flag without guidance.

### Tokonomix-delegated routing — the managed tier (token-refs, never literal keys)

A preset's `env` map can point the spawned CLI at an OpenAI-compatible gateway, so model choice,
budget caps, EU-residency and central billing are configured ONCE on a Tokonomix token instead of
per machine. The config scaffold ships this documented shape:

```jsonc
"managed": {
  "cmd": ["codex", "exec", "--sandbox", "workspace-write"],
  "env": { "OPENAI_BASE_URL": "https://gateway.tokonomix.ai/v1",
           "OPENAI_API_KEY": "env:TOKONOMIX_KEY" },
  "autonomy_confirmed": false
}
```

The key is a **token-ref, NEVER a literal**: `env:VAR` resolves from the launcher's own
environment at spawn; `vault:<mount>/<path>[#field]` resolves via the existing keysource
(`agents_never_sleep/keysource.py`, gated on `integrations.vault`). Resolution happens into the child env
BEFORE the capability probe (probe == spawn rule) and a failed resolution — missing env var,
unreadable vault path, disabled vault integration — is a blocking NO-GO with a clear message,
never a silent empty value. Resolved values are registered with the redaction layer and never
printed. A literal value still passes through unchanged, but one that LOOKS like a pasted key
(long, token-shaped, high-entropy) is loudly flagged at launch. Trust scope: an `env:` ref lets
the config choose WHICH launcher env vars enter the child env — that choice is part of what you
vouch for at `--trust` time, like the argv and host checks the config already carries. Honest
free/paid line: this is the managed **governance** tier — the gateway owns routing, caps and
billing centrally; the DIY path with your own provider keys stays fully functional. **Residual
surface (by design):** the spawned agent legitimately holds the resolved key, and its raw background
run-log (`.unattended/logs/`) is the agent's own stream — NOT redaction-scoped — so the launcher
creates that log `0600` (owner-only, never world-readable) rather than pretending to scrub it; for an
untrusted agent binary use `--fg` or a restricted `launcher.log_dir`.

### Mutual exclusion is atomic and pidfile-free

The launcher takes a non-blocking `flock(2)` on `<repo>/.unattended/ans-run.lock` — BEFORE the
expensive probes — and hands the open FD to the long-lived agent process: the kernel holds the
lock exactly as long as the run lives and releases it on any crash or kill. Pidfile schemes were
rejected in review — they are TOCTOU-racy and leave stale state after a crash. The lock is
repo-local (not shared `/tmp`), so only principals who can already write the repo can touch it.
Two simultaneous starts always yield exactly one winner (proven by
`acceptance/test_launcher.py`); `--check` only probes the lock and never blocks a launch.
Opt-out for intentionally disjoint worktrees: `ANS_RUN_NO_LOCK=1`.

Background mode (the default) writes agent output to `.unattended/logs/` (override:
`launcher.log_dir`) and prints the PID plus a watch hint; `--fg` execs the agent so its exit code
propagates unchanged (cron-friendly).

### Running unattended as the right user — prefer no sudo at all

Run the cron/systemd job AS the target user (`User=` in the unit, or the user's own crontab) —
then no sudo rule is needed. Where the root-guard re-exec is genuinely required, use a
**command-scoped** sudoers rule pointing at a root-owned, non-writable path:

```
ops ALL=(ansrunner) NOPASSWD: /usr/local/bin/ans-run
```

Never `NOPASSWD: ALL` — that hands an autonomous shell-executing agent a passwordless
privilege-escalation primitive.

## Context strategy for long backlogs

A single agent session that drives a long backlog **degrades as it accumulates** — empirically
around ticket ~19 the session starts deferring large/live-facing work and losing earlier design
constraints. Each `next`/`complete` is already a fresh subprocess that rejoins the run branch, so
the *harness state* never degrades; what degrades is the **one long agent context** driving the loop.

Consensus chose a hybrid (Option D):

- **Short / coupled backlogs** → keep one session. The agent CLI's built-in **auto-compact** (~95%
  of the window) handles it; don't intervene.
- **Long / independent backlogs** → a **fresh agent session per N tickets**. A fresh session starts
  with a clean, un-degraded context and re-reads the durable per-ticket state, so ticket 40 gets the
  same quality as ticket 1.
- **NEVER a mid-task %-trigger** (e.g. "compact at 50%"). It is wrong on three counts: it summarizes
  lossily mid-thought, it busts the prompt cache (every later token re-billed), and it silently drops
  design constraints the agent was holding. Auto-compact near the ceiling is fine; a low %-trigger is not.

### The `fresh_session_every` knob (opt-in, default OFF)

`launcher.fresh_session_every` (int ≥ 0, **default `0` = off**) turns on the fresh-session loop in
`bin/ans-run`:

```json
{ "launcher": { "fresh_session_every": 8 } }
```

- **`0` (default)** — byte-identical legacy behaviour: one background `claude -p` spawn drives the
  whole loop; the Stop-hook blocks any stop while the run-incomplete sentinel exists.
- **`N > 0`** — the launcher supervises a bounded loop (cap 500): it spawns an agent with
  `UE_SESSION_TICKET_BUDGET=N`, waits for it to exit, then checks the run-incomplete sentinel. Sentinel
  **absent** → the run reached a terminal state (DRAINED/HALTED/…) → stop. **Present** → reset the
  per-session counter + marker and spawn a **fresh** agent for the next N tickets.

How the early stop is coordinated without weakening never-stop: while a budget is set, the driver
counts RECORDED completions for the current session into `.unattended/state/session-ticket-count`;
at N it writes `.unattended/state/session-budget-reached` **and** changes the `complete` response's
`next` hint from "call `next`" to "STOP now — the launcher will resume you fresh". Since the agent's
loop is complete→next, a `complete` that says STOP is the deterministic brake (the agent simply
doesn't call `next`). The Stop-hook then **allows** the stop because `UE_SESSION_TICKET_BUDGET` is set
AND the marker exists — even with the run-incomplete sentinel still present — so the launcher resumes
in a fresh session. When the budget env var is **unset** (the default), the driver writes nothing, the
`next` hint is unchanged, and the hook keeps blocking while the sentinel exists: the never-stop
guarantee is fully intact. The marker path is pinned via `UE_SESSION_BUDGET_MARKER` (like
`UE_RUN_INCOMPLETE`) so hook and driver agree even when CWD ≠ repo.

Tested in `acceptance/test_fresh_session.py`: default-off is a single budget-free spawn and a real
default-off `complete` writes no counter/marker and keeps the "call next" hint; the hook blocks while
the sentinel exists and the budget is unset; the hook allows an early stop when budget + marker are
present; a sub-budget completion keeps the "call next" hint while the at-budget completion flips to
STOP; the counter resets per session; and the loop respawns until the sentinel clears.

## Council review (multi-model, advisory) — when configured

If the project config enables `council` (needs the tokonomix gateway), each `PROCEED` ticket carries a
`council` block. **You run the council via the tokonomix MCP** (the harness can't call LLMs) — it is
ADVISORY and never blocks the run; it only decides whether finished high-risk work is auto-trusted.

> **Epistemics — what the council is and isn't.** A multi-model council is a **recall amplifier that
> feeds judgment, not a truth oracle**: with several vendors you only need one to catch the missed
> edge case, and it gets surfaced — but agreement is *not* correctness (frontier models share training
> data, so they can be confidently+uniformly wrong on a shared blind spot). It works here because of
> the design, not the headcount: proposers answer **parallel + blind** (dissent is preserved, no
> debate-capitulation), the **judge is independent** (disjoint from the proposers, never scoring its
> own answer, cross-family), and selection favours **decorrelation over raw score**. The biggest lever
> against shared hallucination is **grounding** (feed the real diff/logs/spec), not more models. This is
> exactly why ANS treats the council as advisory and never auto-trusts: a HEAVY-risk diff whose council
> raised concerns / errored / never ran is recorded `DONE_LOW_CONFIDENCE` + **NEEDS DAYLIGHT REVIEW** —
> the deterministic gate stays the only HARD gate. (Full rationale: the `tokonomix-consensus` skill's
> "How the council is built" + "What it does — and doesn't — give you".)

Per ticket, when a `council` block is present:
1. **Pre-work:** read `council.hint_tier` (a non-binding hint). Plan to convene a council on your
   implementation. LIGHT = a de-anchoring pass ("what am I not seeing?"). HEAVY (schema/migration,
   security/auth/tenant, money/billing, public API, cross-service) = review the plan AND the diff.
2. **Pull FRESH model slugs** from `tokonomix_list_models` (the configured slugs drift). Prefer the
   `proposers`/`judges` in the block but swap any that 404 / are slow. Call
   `tokonomix_consensus_ask(models=[...], judge_models=[...], mode="consensus")`.
3. **ALWAYS show** the pre-council summary (`council.summary`: task · proposers · judges · mode · est
   €) before the call, and the post-call summary + the REAL charged cost + which proposers
   errored/timed-out afterward. Unattended: show and continue. Interactive: show and get approval.
4. **Graceful degradation:** if the council errors / times out / has no key, log it as a blind-spot
   and PROCEED — never stop. Report it as `--council-verdict error`.
5. **Rate the council call** via `tokonomix_rate_consensus` immediately after acting on the result.
   Submit: `score` (1–10, how useful was the council for THIS ticket decision), `helped_model` (the
   proposer whose view was most decisive, if any), and `outcome` + `findings` real/false counts per
   severity bucket (once you know whether the concerns were real). This feeds the agent-utility
   reputation scores. **Only when `consensus_rating_endpoint_enabled` is ON** — skip silently if
   the call returns 404. Interactive: show the rating you plan to submit and get approval first.
   Unattended: auto-submit based on your outcome assessment, log the rating in the ticket summary.
6. **Feed the result back** on `complete`:
   `... complete --council-verdict pass|concerns|error --council-cost <€charged> --review-coverage
   "<proposers that ran, who errored, € cost>"`. Omit `--council-verdict` only if you ran no council.

**Cost safety (unattended runs spend real money).** The harness enforces a per-night brake from
`--council-cost`: once `budget.per_night_euro_cap` or `budget.max_council_calls_per_night` is reached,
`next` returns `council: {disabled: ...}` — stop convening councils (high-risk diffs are still flagged
for daylight review). Also check `tokonomix_get_balance` before convening; if balance is below
`budget.balance_threshold_euro`, stop convening councils, log a blind-spot, and proceed. Always report
the real charged cost via `--council-cost` so the brake is accurate.

The harness then re-routes from the ACTUAL diff (overriding the hint) and decides disposition: a
HEAVY-risk diff whose council raised **concerns**, **errored**, or **was never run** is recorded
`DONE_LOW_CONFIDENCE` and flagged **NEEDS DAYLIGHT REVIEW** in the morning report — done, but not
trusted to merge blind. Deterministic gates remain the only HARD gate; the council never reverts.

### Specialist reviewers (when `specialists` is enabled)

A finer-grained companion to the whole-diff council: distinct review **lenses**. A `PROCEED` payload
carries a `specialists` hint — `architect` + `security` always, plus `tenant-safety` / `mobile` /
`ux` / `i18n` / `performance` / `seo` only when the diff (or, for the hint, the ticket text) touches
that domain. **You run each lens as a focused tokonomix review** of your plan/diff; it is ADVISORY
and never blocks. On `complete`, report any lens that raised a **material concern** via
`--specialist-concerns architect,security,…` and the real € via `--specialist-cost`. The harness
records which lenses applied (from the actual diff) as coverage, and folds a concern in a
**high-blast-radius** lens (`architect` / `security` / `tenant-safety`) into the SAME daylight-review
disposition as the council — `DONE_LOW_CONFIDENCE` + **NEEDS DAYLIGHT REVIEW**, composed alongside any
council reason, never clobbering it. Specialist € shares the council's per-night spend cap (it does
not consume a council-call slot). If `budget_exhausted` trips, `specialists` is returned `disabled` —
stop convening lenses; high-risk diffs are still flagged.

**Recommended council cadence:** run a council at ticket **start** (plan review), **mid** (after a
significant implementation choice), and **end** (diff review) — approximately 3 calls per ticket.
Running a council on every individual change (per-call) is possible but only recommended when the
user explicitly requests it, given the per-night call cap and credit cost. The default caps
(`max_council_calls_per_night`, `per_night_euro_cap`) are sized for the 3-calls-per-ticket cadence.

Mode detection is layered: `CLAUDE_UNATTENDED=1` (set by `claude-run`/cron) or a keyword →
unattended (no prompts, ever); otherwise interactive.

## Portability

This is the open `SKILL.md` standard — it runs in Claude Code, Codex, Gemini CLI, Copilot, Cursor
and 30+ other tools. The body above is written by ROLE, not by Claude-specific tool names. Enforcement
(Stop-hook to prevent premature stop, deny-hooks for irreversible actions and for asking the human)
is a thin per-platform adapter; the Claude adapter lives in `hooks/` and is opt-in (env-gated to
`CLAUDE_UNATTENDED=1`).
An `AGENTS.md` router lets file-based tools discover this skill.

## What is deferred to Phase 2

The agent-as-worker bridge, the multi-model council (diff-routed light/heavy review, advisory
trust-gating), the never-ASK enforcement hook, the conditional specialist reviewers
(architect/security/tenant-safety daylight-fold, diff-routed lenses), and shape-anchored secret
redaction (scrubs credentials from the report, gate artifacts, Paperclip comments and emitted JSON;
a literal-value registry the Vault slice will feed) are now built. Full Paperclip integration is
also built (`harness/sources/paperclip.py` + `run.py` wiring, `acceptance/test_paperclip.py`): pull
open issues from a single configured project as the work-source, push per-ticket status transitions
(todo→in_progress→done/blocked) and parked/daylight comments back, with the board token resolved via
the keysource (`env:`/`vault:`) and graceful degrade-to-local + blind-spot when it can't be read.
The cross-platform enforcement adapters (Gemini / Codex / Copilot / Cursor / Windsurf via
`agents_never_sleep/enforce.py` + `capabilities.py`) are now built too — **live-verified on Claude Code, built
to each platform's documented hook contract elsewhere** (see the README capability matrix). The spine
shipped first; the quality machinery layered on top.

The heartbeat watchdog is built and proven (`agents_never_sleep/watchdog.py`, `acceptance/test_watchdog.py`): a
sidecar that runs the unattended command as a child, restarts it resumable when the heartbeat goes
stale (the hang the Stop-hook can't see), and on exhausted restarts alerts + exits 75. Integrating it
with `claude-run` is **opt-in composition** (wrap the call; see `WATCHDOG.md`) — never a rewrite of
that shared wrapper.

Vault as a key source is built: a `token_ref` of `env:VAR` or `vault:<mount>/<path>[#field]` (the
form the wizard already writes) resolves the Paperclip token and tokonomix key from env or the configured
Vault (AppRole / `VAULT_TOKEN`, KV-v2), registers the resolved value with the redaction layer, and
DEGRADES to a morning-report blind spot — never a hard stop — when a configured source can't be read.

The tokonomix onboarding gate is built: when a credential consumer (council/specialists) is enabled
but no tokonomix credential is found, a `PROCEED` payload carries an `onboarding` directive — run the
keyless `tokonomix_onboard` → `tokonomix_onboard_verify` handshake (interactive), or, unattended, the
night proceeds with review disabled and the morning report carries a **BLIND SPOT** note. The MCP
calls are yours; the harness owns only the gate.


## Related skills

- **`tokonomix-gateway`** — direct HTTP access to the Tokonomix gateway for
  apps and agent frameworks not using MCP. If you are building a custom
  integration instead of running ANS, see the gateway skill.
  Get it: `npx tokonomix-council-mcp` includes the skill under
  `node_modules/tokonomix-council-mcp/skill/tokonomix-gateway/`.
- **`tokonomix-council-mcp`** — MCP tools for interactive consensus
  (`tokonomix_consensus_ask`, `tokonomix_single_ask`, etc.) used by ANS
  for the review gate. The same npm package also bundles the gateway skill.
