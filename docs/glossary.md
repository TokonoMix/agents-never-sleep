# ANS Glossary — terminology source-of-truth

> **What this is.** The single, authoritative definition of every term used across the Agents Never
> Sleep (ANS) documentation. When the README, ARCHITECTURE, or any other doc uses a capitalised term
> or a piece of jargon, this file is what it means — verified against the `agents_never_sleep/` source
> for ANS v1.0.0.
>
> **The 30-second version.** ANS is **Autonomous Execution Governance**: the layer *above* a coding
> agent that decides how the agent should behave when it runs unattended and hits something it is
> unsure about — **PROCEED** (assume + log + keep going), **PARK** (defer this one decision, keep the
> run moving), or **HALT** (stop the whole run, only on irreversible danger) — while keeping every
> change reversible and never letting one unanswerable question stall the rest of the backlog. ANS
> owns **execution only**; verification, model choice, and code generation belong to other building
> blocks (see *Tokonomix ecosystem* below).

Terms are grouped by concept; within a group the most load-bearing term comes first. Where a term maps
to a concrete module, the module is named — open it to see the exact behaviour.

---

## The autonomy contract

**Autonomy contract** — The rule that an unattended run has exactly three responses to uncertainty —
ASK, PARK, HALT — and that ASK is forbidden while unattended. This is the heart of ANS: a coding agent
left alone treats *every* unknown as "stop and ask", which means one unanswerable question wastes the
whole run. The contract gives the agent a structured alternative so the run never idles. Defined in
`decide.py`; enforced by the deny-hooks in `hooks/`.

**ASK** — Asking the human a question. **Forbidden while unattended** — there is nobody to answer at
mid-run, and a single blocking question wastes the entire run. Enforced structurally, not by discipline:
the `deny_ask.sh` PreToolUse hook (env-gated on `CLAUDE_UNATTENDED=1`) denies the `AskUserQuestion`
tool and steers the agent back into PARK or PROCEED. The one exception is a single setup question asked
*once, in chat, before the run starts* (foreground vs detached) — that is routing, not a mid-run stall.

**PARK** — Defer *this one decision or ticket* and keep the run moving to the next independent ticket.
Parking is normal and healthy — it is the opposite of stopping. A park always records why, the candidate
interpretations, the exact human next-action, and its contamination scope, so reviewing it afterward is a
few-second decision. Recorded as `PARKED_DECISION` or `PARKED_FOUNDATIONAL` (see *Outcome states*).

**HALT** — Stop the *whole run*. Reserved for genuinely irreversible danger that the hook layer would
block anyway, or for the case where there is no reversibility safety net at all (read-only filesystem,
no version control and none creatable). HALT is rare by design; almost every uncertainty is a PARK.

**PROCEED** — Assume a reasonable answer, log the assumption, and continue. Chosen only for
low-blast-radius, reversible decisions (naming, internal structure, log/comment wording, test fixtures,
a choice between two equivalent local implementations). Every PROCEED assumption is committed so it can
be reverted if it turns out wrong. Decided in `decide.py`.

---

## Deciding PROCEED vs PARK

**Blast radius** — How far a wrong decision can spread. ANS tiers decisions by blast radius so "unsure"
is rare and a confidently-wrong high-impact guess never happens: low blast-radius + reversible → PROCEED;
high blast-radius → Hard-PARK. The tiering is made concrete (enumerated categories) rather than left to
judgement. Lives in `decide.py`.

**Hard-PARK** — A category that is *never* guessed, regardless of how reversible it looks. The enumerated
categories (`HARD_PARK_CATEGORIES` in `decide.py`) are: database schema / migration direction, public or
shared API contract, security / auth / tenant-isolation boundary, money / billing / pricing, and any
cross-ticket interface other tickets build on. Requirement-meaning ambiguity (you don't actually know
*what* to build) also parks unless it is both locally reversible and isolated — then ANS builds it
reversibly behind a flag *and* parks the decision (a hybrid).

**Contamination scope** — How far a parked or failed ticket's risk can spread: `MODULE` < `PACKAGE` <
`SERVICE`. The scheduler may only hand the agent a "next" ticket whose scope does not intersect a parked
ticket's scope — so it never builds on top of an unresolved foundational decision. Defined in `decide.py`
/ `state.py`.

**Requirement-meaning ambiguity** — The case where the agent does not know *what* the ticket is asking
it to build (as opposed to *how*). Detected from signal words ("which", "unclear", "TBD", a trailing
"?") in `decide.py`. It parks the decision unless the work is reversible and isolated enough to build
behind a flag and park only the choice.

---

## The per-ticket loop

**Ticket** — One unit of work the agent implements in a single PROCEED. ANS hands the agent exactly one
ready-to-implement ticket at a time. Tickets come from a directory of Markdown files (`--tickets <dir>`)
or, when configured, from a Paperclip project (`sources/paperclip.py`). Loaded by `tickets.py`.

**Preflight** — The capability scan that runs after the agent session boots: it measures version
control / reversibility, platform, gates, execution mode, and optional integrations (Tokonomix, Vault,
Paperclip). A missing capability never stops the run — it lowers expected yield and raises conservatism.
Lives in `preflight.py`. (Compare the *launcher*, which runs its checks *before* any tokens are spent.)

**Deterministic gate** — The project's own non-interactive check (typically the test suite or a type
check like `tsc --noEmit`) run after each diff. This is the **backbone** and the *only hard gate* — the
one thing that can hard-block a ticket. It runs with a per-step timeout and a non-interactive environment
so it can never hang on a TTY prompt. A failure introduced by the diff reverts to last-green and
parks/fails; a pre-existing / flaky / environment failure downgrades confidence but never reports the
ticket as failed. **Never delete or skip a failing test to go green** — that is a blocking blind-spot.
Lives in `gates.py`.

**Snapshot / revert** — Before editing, ANS takes a git snapshot of the working tree; if the gate fails
on the diff, it reverts to that last-green snapshot. This is the reversibility safety net that makes a
wrong PROCEED a cheap revert instead of a catastrophe. If the snapshot commit cannot be made
(git lock, timeout, read-only object store) the ticket is recorded `BLOCKED_ENV` rather than edited
unrevertibly. Lives in `vcs.py`.

---

## Outcome states

**Outcome state** — The single durable record written for every ticket (`state.py`, `OutcomeState`).
There are exactly seven, and they are never collapsed:

- **DONE** — completed, gates green.
- **DONE_LOW_CONFIDENCE** — completed and gates green, but the *delegated* review coverage was degraded
  (a high-risk diff whose council raised concerns, errored, or never ran). Flagged **NEEDS DAYLIGHT
  REVIEW** in the run report — done, but not trusted to merge blind.
- **PARKED_DECISION** — a single decision deferred; the run kept moving.
- **PARKED_FOUNDATIONAL** — a foundational ambiguity; the ticket is parked *and* its dependents are
  quarantined (see *contamination scope*).
- **BLOCKED_ENV** — the environment or tooling blocked progress (not the agent's fault); e.g. a git lock
  or an un-runnable gate.
- **FAILED_RETRYABLE** — a gate failed on the diff; the edit was reverted; the ticket can be retried.
- **FAILED_BUG_IN_AGENT** — the harness or agent did something wrong; needs a human look.

**Morning report** — The end-of-run summary (`report.py`): what got done, what parked and why, what
needs daylight review, spend, and blind spots. The artefact a human reads when the run ends — named for
the classic overnight case — to turn a run of autonomous work into a few quick decisions.

---

## Anti-starvation (never burn the run on one ticket)

**Attempt cap** — The maximum number of times a single ticket is attempted across resumes (`ledger.py`,
`AttemptLedger`). A ticket that crashes and restarts forever would otherwise burn the whole run; at the
cap it is force-parked instead. The cap survives resumes because the ledger is durable.

**Loop detection** — Detecting a ticket that loops *fast* under budget, failing the same way each time.
The ledger records a stable **failure signature** (`failure_signature()` extracts the stable identifiers
so a varying timestamp/path doesn't defeat detection); when the same signature recurs past a threshold,
the ticket is force-parked. Lives in `ledger.py`.

**Low-yield breaker** — A circuit breaker that stops the run and alerts when most work is parked, blocked,
or failing — so a systematically broken environment doesn't grind on for the whole run producing nothing. It only
trips on a non-trivial backlog (≥ 8 processed) once the bad ratio (parked+blocked+failed / processed)
reaches 0.75. Lives in `orchestrator.py` (`LowYieldBreaker`); surfaces as the `LOW_YIELD` terminal status.

**`reset-attempts` / `reset-spend`** — Operator escape hatches (run subcommands). `reset-attempts <ticket>`
clears an inflated attempt counter for the documented "a kill+resume round-trip pushed a healthy ticket
to the cap" case; `reset-spend` zeroes the per-run spend accounting (the breaker's processed/bad
counters are untouched). Defined in `run.py` / `ledger.py`.

---

## Running it

**Run loop subcommands** — The agent drives the run by alternating two subcommands until the backlog
drains; each prints one JSON object to stdout. `next` asks the harness for the next ready ticket (it
auto-parks ambiguous / high-blast-radius tickets and owns the run-incomplete sentinel); `complete`
records the outcome of the ticket just implemented. The full set in `run.py` is **`next`, `complete`,
`report`, `reset-attempts`, `reset-spend`, `parked`**. There is **no `run` subcommand** — the old
in-process `run` was removed; real runs always use the `next`/`complete` flow. (`parked` lists what
parked and why.)

**Run-incomplete sentinel** — The file (`${UE_RUN_INCOMPLETE:-<repo>/.unattended/run-incomplete}`) that
`next` owns and the Stop-hook checks: while it exists, a premature stop is blocked, guaranteeing
the run keeps going until `next` returns a terminal status. The "never-stop" guarantee in one file.

**The agent is the worker** — A deliberate division of labour: the *harness* (stdlib Python) owns
scheduling, parking, snapshot/revert, the attempt cap, loop detection, and the never-stop sentinel; the
*agent* (Claude Code, Codex, …) does the actual file edits, one ticket at a time. The harness cannot call
an LLM, so it cannot do the work itself — and it cannot run from inside a Python loop, so the agent drives
the `next`/`complete` loop.

**Fresh-session-every** — An opt-in launcher knob (`launcher.fresh_session_every`, default `0` = off) for
long backlogs. A single agent context degrades as it accumulates (empirically around ticket ~19 it starts
deferring large work and losing earlier constraints). With `N > 0`, the launcher spawns a *fresh* agent
session every N tickets; because per-ticket state is durable, ticket 40 gets the same quality as ticket 1.
A mid-task percentage-trigger compaction is explicitly *not* used (it summarises lossily and busts the
prompt cache). Lives in `launcher.py` / `driver.py`.

---

## The launcher (pre-token GO/NO-GO)

**Launcher (`bin/ans-run`)** — The deterministic GO/NO-GO gate that runs *before* the agent CLI boots, so
a doomed run never spends its first token. It checks config trust, identity, agent selection, credentials,
repo health, and host checks; exit `0` = GO, `64` = NO-GO, `65` = working tree busy. Lives in
`launcher.py` / `agents_never_sleep/launcher.py`.

**TOFU config-trust** — Trust-On-First-Use for `.claude/agents-never-sleep.json`. The config travels with
the repo and describes commands the launcher executes (agent argv, host checks), so a new or changed
config must be trusted once per user — interactively, or explicitly via `ans-run --trust` after review.
Trust is recorded *outside* the repo (`~/.config/agents-never-sleep/trusted.json`, keyed on the config's
SHA-256), because the repo cannot vouch for itself. Headless + untrusted = NO-GO. Lives in `trust.py`.

**Autonomy-confirmed preset** — A launcher agent preset that has explicitly recorded `autonomy_confirmed:
true`. A detached run needs an autonomy flag (e.g. Claude Code's `--permission-mode acceptEdits`) or it
stalls at its first approval prompt; that flag grants real power, so the wizard shows what it grants before
asking the human to confirm. A preset without confirmation refuses to launch detached — a deliberate
NO-GO instead of a silent stall. Map in `agent_clis.py`.

**Working-tree flock** — The mutual-exclusion lock (`<repo>/.unattended/ans-run.lock`) the launcher takes
via a non-blocking `flock(2)` before the expensive probes, handing the open FD to the long-lived agent
process. The kernel holds it exactly as long as the run lives and releases it on any crash — no stale
pidfile state. Repo-local, so only principals who can write the repo can touch it. Opt-out for genuinely
disjoint worktrees: `ANS_RUN_NO_LOCK=1`.

**Capability probe** — A 5-second `--version` check the launcher runs on the agent binary before spending
tokens, to catch CLI flag drift. Resolution of any token-ref credential happens *before* the probe (probe
== spawn rule), so a doomed launch fails cheaply.

---

## Resilience

**Watchdog** — A standalone sidecar (`watchdog.py`) that runs the unattended command as a child and polls
its heartbeat file. If the heartbeat goes stale (the parent *hung* — the gap the Stop-hook cannot see), it
kills the child and restarts it resumable. On exhausted restarts it runs an optional alert command and
exits `75`. The stale threshold (default 2400s) must exceed the worst-case single-ticket work time, or it
would false-restart a healthy run. Integrating it is opt-in composition, not a rewrite of any shared
wrapper.

**Heartbeat** — The liveness signal (`heartbeat.json`, written by `heartbeat.py`) beaten at `next`/
`complete` boundaries. The watchdog watches it to tell a working run apart from a hung one. Note: heartbeat
age climbing *during* a ticket is normal (the agent is implementing); a stall is high age **and** no commit
**and** no file edits.

---

## Security

**Secret redaction** — Shape-anchored scrubbing (`redact.py`) that strips credential-shaped values from the
run report, gate artefacts, Paperclip comments, and emitted JSON. Resolved credentials are registered
with a redaction registry so they are never printed. Built, not "Phase 2".

**Keysource** — The credential resolver (`keysource.py`) for token-refs of the form `env:VAR` (resolved
from the launcher's own environment at spawn) or `vault:<mount>/<path>[#field]` (resolved via HashiCorp
Vault, AppRole / `VAULT_TOKEN`, KV-v2). A credential is **never** a literal in the config — only a ref. A
failed resolution is a blocking NO-GO with a clear message, never a silent empty value; a configured source
that can't be read at *run* time degrades to a run-report blind spot rather than a hard stop.

**Token-ref** — A pointer to a credential, never the credential itself: `env:VAR` or
`vault:<mount>/<path>[#field]`. The form the wizard writes for the Paperclip token and the Tokonomix key.
Which launcher env vars an `env:` ref may pull into the child is part of what a human vouches for at
`--trust` time. Resolved by `keysource.py`.

**Least privilege** — The principle that ANS holds the minimum authority needed: run the cron/systemd job
*as* the target user (no sudo); where the root-guard re-exec is genuinely required, use a command-scoped
sudoers rule, never `NOPASSWD: ALL` (which would hand an autonomous shell-executing agent a passwordless
privilege-escalation primitive). Enforced by the launcher's identity / root-guard checks.

**Deny-list floor** — The conservative, enumerated set of shell-command patterns
(`enforcement.py::_IRREVERSIBLE`) DENYed/PARKed by default during an unattended run — force-push, remote
branch/tag delete, destructive SQL, disk-destructive commands, secret-path Vault writes/deletes, mass Redis
flush, docker volume delete, sending real email, package/release publishing, infra teardown, stateful
`kubectl delete`, and other outward/destructive shapes. A **backstop against an honest mistake, not a
boundary against evasion** — it inspects command text, not script contents. See *Consent manifest* for the
one sanctioned way to widen it.

**Consent manifest** — A human's run-setup pre-authorization of specific deny-list classes (e.g.
`redis_flush`), recorded **out-of-repo** (`~/.config/agents-never-sleep/consent/`, TOFU-style — the
unattended agent cannot author its own consent) via the interactive wizard's "Actions ANS may perform
unattended" step. Frozen into the agent process as `UE_CONSENT` at launch; upgrades PARK→ALLOW only for a
single clean shell statement matching exactly one consented class. Coarse (per-class / once / at-setup /
whole-run / all-targets) — target-scoping is a deferred v2 item. Every consent-upgraded command is logged
to an out-of-repo audit trail and surfaced in the morning report.

---

## Delegated verification (NOT an ANS capability)

> **Scope boundary — load-bearing.** ANS owns **execution only**. It does **not** generate code, judge
> whether code is correct, reason about model quality, or run consensus/verification. Those are different
> jobs that belong to other Tokonomix building blocks. The terms below describe a *delegated integration*
> with the external verification layer — not an ANS feature.

**Delegated review (council)** — For a high-risk diff, ANS may *optionally delegate* a second opinion to an
external verification/consensus layer — the **Tokonomix Council MCP**, a separate, standalone building
block — and use the verdict for **one** purpose only: to decide whether to trust the finished work or flag
it `DONE_LOW_CONFIDENCE` + NEEDS DAYLIGHT REVIEW. The multi-model reasoning happens *outside* ANS (the
harness is stdlib Python and cannot call an LLM); ANS owns only the budget gate and the trust-or-flag
decision. **Advisory — it never blocks the run and never reverts.** Scaffolding in `council.py`.

**Specialist lenses** — A finer-grained companion to the whole-diff council: distinct review *lenses*
(`architect` + `security` always, plus `tenant-safety` / `mobile` / `ux` / `i18n` / `performance` / `seo`
only when the diff touches that domain). Like the council, they are *delegated* to the external verification
layer, advisory, and folded into the same daylight-review disposition for a high-blast-radius concern.
Scaffolding in `specialists.py`.

**Advisory** — Describes any delegated review: it informs the trust-or-flag decision but **never** hard-
blocks a ticket and **never** reverts a diff. The deterministic gate (your tests) remains the only hard
gate. Model agreement is recall, not truth — which is exactly why ANS treats the council as advisory.

---

## ANS in the Tokonomix ecosystem

ANS is one of *dozens* of planned specialized Tokonomix building blocks, each standalone-usable but stating
its place. Knowing the boundary is how you know *when not to reach for ANS*:

| Responsibility | Building block | ANS relationship |
|---|---|---|
| **Execution** governance | **ANS** (this project) | — |
| **Decision-making** / verification / consensus | **Tokonomix Council MCP** | ANS *delegates* the high-risk second opinion here (advisory) |
| **Verification** of media/output | Media QC | outside ANS |
| **Measurement** | Benchmark | ANS reports spend; benchmarking is separate |
| **Provider selection** | Routing | which model/provider runs is not ANS's job |
| **Long-term context** | Memory | ANS state is per-run + durable, not long-term memory |

The rule of thumb: **if it is not "how an unattended agent should behave while it works", it is not ANS.**
Code generation → the coding agent. Is-the-code-correct → the deterministic gate + delegated Council.
Which model → Routing. ANS governs the run; everything else is delegated.

---

*Verified against `agents_never_sleep/` (ANS v1.0.0): `decide.py`, `state.py`, `gates.py`, `ledger.py`,
`orchestrator.py`, `launcher.py`, `trust.py`, `keysource.py`, `redact.py`, `watchdog.py`, `heartbeat.py`,
`vcs.py`, `run.py`, `council.py`, `specialists.py`. No benchmark results are claimed in this document.*
