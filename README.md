# Agents Never Sleep (ANS)

**Autonomous Execution Governance** — the governance layer between autonomous coding agents and
software engineers.

## Quick Start

> **TL;DR:** A coding agent handed a long backlog stops dead the moment it hits one uncertain
> decision — ANS lets it defer just that ticket and keep working through the rest, reversibly.

```bash
pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.3.0

python3 -m agents_never_sleep.run next     --repo . --tickets ./backlog
#   …implement the ticket it hands you…
python3 -m agents_never_sleep.run complete --repo . --attempted "what you did"
```

Full walkthrough: [Quick Start](#9-quick-start).

> **Mission:** *Enable autonomous AI agents to complete meaningful work safely, predictably,
> transparently and without unnecessary human interruption.*

ANS governs **how an AI agent behaves while it works autonomously** — not the AI itself. It is not a
model, an IDE, or a coding agent, and it does not write code. It sits *above* Claude Code, OpenAI
Codex, Cursor, OpenHands, Aider and CI/CD: they do the work; ANS decides what an unattended run should
do when it is unsure, keeps every change reversible, and never lets one unanswerable question stall the
rest of the backlog.

**The 20-second version:** *"My AI agent no longer has to halt the entire backlog because of one
unanswered question."*

- **Use it when:** you hand a coding agent a backlog or milestone and walk away — overnight, "while
  I'm out", a long CI job, a run-to-completion handoff — and you need it to make real, *reversible*
  progress without a human babysitting every prompt.
- **Do not use it for:** writing the code, judging whether the code is correct, or choosing a model.
  Those are different jobs (see [Scope boundary](#5-scope-boundary--what-ans-owns-and-what-it-does-not)
  and [the ecosystem](#11-ans-in-the-tokonomix-ecosystem)).

ANS names and proposes a category — *Autonomous Execution Governance* — the missing operational layer
between a model that can act and a developer who isn't watching. The category is one we propose here, not
an externally settled standard: there is no outside spec and no second implementation yet.

- **Version:** 1.3.0
- **Install today:** `pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.3.0`
  (PyPI publish is pending — see [Installation](#8-installation)).
- **Source:** [TokonoMix/agents-never-sleep](https://github.com/TokonoMix/agents-never-sleep) · MIT

### ANS is NOT / ANS IS

For fast disambiguation — by a person scanning, or an AI system deciding when to reach for this:

- **ANS is NOT** an AI model · an IDE · an MCP server · a chatbot · a code reviewer · a testing
  framework · a competitor to Claude Code / Codex / Cursor / OpenHands / Aider.
- **ANS IS** execution governance · an autonomous-run workflow · execution orchestration · resilience
  for long unattended jobs · operational safety for an agent nobody is watching.

### The emergence of Autonomous Execution Governance

Each time software engineering took on a new kind of risk, a new operational discipline appeared to
govern it. Source code that many people touched produced **version control**. Code that had to ship
safely and often produced **CI/CD**. Infrastructure that grew too large to click together by hand
produced **Infrastructure as Code**. The widening gap between building and running produced **DevOps**,
and systems too complex to reason about blind produced **Observability**.

Autonomous AI agents introduce the next kind of risk: software that *acts on its own*, for hours, with
nobody watching — and whose only built-in answer to uncertainty is to stop, or to guess. That risk asks
for its own discipline: **Autonomous Execution Governance** — the operational layer that decides *how*
an autonomous agent should behave, stay reversible, and stay honest while it runs unattended. ANS is the
**first concrete implementation of that discipline** — the first working / open reference implementation
of the principles *we* define here. There is no external spec or second implementation yet, and no
outside party has adopted it. (The depth — the full emergence narrative, the principles, the argument for
the category — lives in the [Autonomous Execution Governance Manifesto](docs/manifesto.md).)

> **The Git analogy (as a thought, not a claim).** *ANS is to autonomous AI what Git became to source
> code.* Git did not make anyone a better programmer — it solved *collaboration*: a disciplined,
> reversible, auditable way for many hands to touch one codebase safely. ANS does not make a model
> smarter — it solves *autonomy*: a disciplined, reversible, auditable way for an agent to run on its
> own safely. The tool isn't the point; the **discipline around the tool** is.

### Why Autonomous Execution Governance matters

The principles a system must honour the moment it runs without a human watching. Autonomous systems
should:

1. **Never guess irreversible decisions.**
2. **Never silently fail.**
3. **Always remain reversible.**
4. **Always remain auditable.**
5. **Always explain uncertainty.**
6. **Never waste an entire backlog on one unknown.**
7. **Separate execution from verification.**
8. **Separate governance from intelligence.**
9. **Prefer recovery over perfection.**
10. **Optimise for trust, not speed.**

These are the *values of the discipline* (the **engineering** principles ANS is built on — Single
Responsibility, Determinism, Least Privilege, … — are listed in
[Design Principles](#design-principles)).

---

## 1. Why ANS exists — the mission story

A coding agent that hits uncertainty has exactly one move: **stop and ask the human.** That is fine
when you are sitting there. It is the defining failure of every *unattended* run. The moment the agent
meets a single ambiguous ticket — "which migration direction?", "is this the intended interface?" — it
collapses the whole session into a blocking prompt that nobody is there to answer. The agent waits.
The other forty tickets that did *not* depend on that question are never touched. You return to a run
that burned eight hours, and real money, frozen on one decision a human could have made in five seconds.

The inverse is just as expensive: an agent that *doesn't* stop charges ahead and does something
irreversible — force-pushes, drops a column, deletes a secret — that you cannot undo afterwards.

**This is a governance problem, not an AI problem.** A smarter model does not fix it. No matter how
capable the model becomes, "I'm uncertain" still maps to a binary — *stop* or *guess* — and neither is
right for autonomous work. What is missing is a layer that turns that binary into a disciplined
decision: *which* uncertainties a run may resolve on its own (because the choice is reversible and
isolated), *which* it must defer to a human (because the blast radius is large), and *which* must halt
the whole run (because the action is genuinely irreversible). The coding agent cannot supply this layer
itself — it is the *worker*, optimizing the next edit; it has no durable, structural authority over its
own autonomy, no enforced reversibility, and no memory of the contract once its context fills up
mid-run.

ANS is that layer. It gives the run an **ASK / PARK / HALT autonomy contract**, enforces it
*structurally* (deny-hooks at the tool layer, not the agent's good intentions), keeps every change it
permits **reversible** (git-backed snapshot/revert), and produces a ranked **run report** so the
run is honest about what got done, what is parked, and what needs a human afterwards. After
reading this, a person — or another AI evaluating tools — should be able to state plainly *why ANS
exists* (a coding agent's only response to uncertainty is STOP, and that halts the whole backlog) and
*when to use it* (any long, unattended, run-to-completion handoff).

**Your infra, your data policy.** ANS is MIT-licensed stdlib Python that runs inside your own
execution environment — your laptop, your CI runner, your server. No code leaves your machine through
ANS itself. This is a deliberate architectural choice and an answer to cloud-based autonomous agents
that execute on vendor VMs: if your data policy or compliance posture requires that code stays on your
infrastructure, ANS doesn't add a new data-egress path. (The optional delegated Council review sends
a diff excerpt to the Tokonomix gateway — that path is explicit, opt-in, and budget-gated.)

In the vocabulary developers actually search with: ANS is **AI governance** for **long-running** coding
agents — a **coding workflow** layer for **autonomous software engineering** that keeps unattended
**developer automation** safe, reversible, and auditable. It governs the **AI execution** of an **agent
workflow**; it does not replace the agent, the model, or your tests.

## 2. The problem

Most coding agents stop the moment they hit a question. That is the failure mode of every *unattended*
run — "while I'm away", a long CI job, a milestone you handed off and walked away from.

A single ambiguous ticket collapses the whole session into a blocking prompt that nobody is there to
answer. The agent waits. The forty tickets that did *not* depend on that question never get touched.

The inverse failure is just as expensive: an agent that *doesn't* stop charges ahead and does something
irreversible — force-pushes, drops a column, deletes a secret — that you cannot undo afterwards.

### One night, two ways

You hand a coding agent a 40-ticket backlog at 22:00 and go to sleep.

**Without governance:**

```
22:00  start. Ticket 1… ticket 12, fine.
22:11  ticket 13: "should migration B replace migration A, or run alongside it?"
       → the agent isn't sure. Its only move is to ASK. It stops and waits.
…      tickets 14–40 are independent of that question. None are touched.
08:00  you wake up. Zero progress since 22:11. One unanswered question
       froze nine hours and real money on a decision you'd have made in five seconds.
```

**With ANS:**

```
22:00  start.
22:11  ticket 13 hits the same migration question. Schema direction = high blast radius
       → PARK (record the two candidate interpretations + the exact morning decision), move on.
22:11  ticket 14 → DONE.   ticket 15 → DONE.   …keeps going, reversibly, all night.
08:00  morning report: 39 completed (each git-snapshotted), 1 parked — with the one
       five-second decision waiting for you, and nothing irreversible done unsupervised.
```

Same agent, same backlog, same uncertainty. The difference is a governance layer that turns "I'm not
sure" into a disciplined PARK instead of a full stop — and keeps every change it *does* make reversible.

## 3. Why existing tools stall

A coding agent has exactly one lever for uncertainty: it asks the human. That single lever is
overloaded to mean three completely different things, and collapsing them is the root cause:

- *"I need a human to decide this."* — a genuine decision (schema direction, an API contract).
- *"I'm not sure, so I'll stop."* — defensible caution, but it shouldn't stop **everything**.
- *"This is irreversibly dangerous."* — the one case where stopping is correct.

Without structured autonomy, all three become the same prompt, and an unattended agent has no
principled way to keep moving past the first one. The agent is the *worker*; nothing above it owns the
question "what should an autonomous run do when it isn't sure?" That missing layer — between the model
and the developer — is what ANS provides.

### Why existing AI agents cannot solve this themselves

It is tempting to assume a sufficiently capable agent would just handle this. It would not — and the
reason is **not** that Claude, GPT, Cursor or any other agent is too weak. It is that the problem sits
**above their responsibility**. This is separation of concerns, not a capability gap.

A coding agent's job is to produce the next correct edit. Governing its own autonomy is a *different*
job, and one it is structurally not positioned to do:

- **It cannot bind its own future behaviour.** "Don't force-push during this run" is a promise made in a context
  window that fills up and rolls over at 2 a.m. Real enforcement has to live *outside* the agent, at the
  tool layer — a deny-hook that fires whether or not the agent remembers the rule.
- **It cannot guarantee its own reversibility.** An agent can intend to keep changes revertible, but the
  guarantee has to be a mechanism (snapshot-before-edit, revert-on-red-gate) owned by something that
  doesn't share the agent's mutable state.
- **It has no durable authority over the run.** The decision "PARK this one and move on" is a property of
  the *whole backlog*, persisted across crashes and fresh sessions — a stateful concern the worker,
  optimizing one edit at a time, doesn't and shouldn't hold.
- **It would conflate roles if it tried.** An agent that also judged whether its own diff was
  trustworthy, also enforced its own limits, and also owned reversibility would be doing four jobs and
  none of them cleanly. The governor governs; the worker works; verification verifies elsewhere.

So this is solved the way every operational discipline is solved: not by making the thing smarter, but
by adding a layer that owns a responsibility the thing was never meant to own. A better model produces
better edits. It does not produce better *governance of its own autonomy* — that is what ANS is for.

## 4. How ANS solves it — the ASK / PARK / HALT autonomy contract

ANS gives the agent a contract with three distinct, never-collapsed responses to uncertainty. While
unattended the agent only ever chooses **PROCEED**, **PARK**, or **HALT** — it never **ASK**s.

| Response | What it means | Effect on the run |
|---|---|---|
| **PROCEED** | Assume + log + continue. For low-blast-radius, reversible choices (naming, internal structure, log wording, equivalent local implementations). The assumption is committed so it can be reverted. | Run keeps moving. |
| **PARK** | Defer *this one ticket/decision* and move to the next independent ticket. Normal and healthy — **not** a stop. Records why, the candidate interpretations, the exact human next-action, and the contamination scope. | Run keeps moving. |
| **HALT** | Stop the *whole run*. Only on genuinely irreversible danger with no safety net (e.g. read-only filesystem, no VCS and none creatable). | Run ends; operator must intervene. |
| ~~**ASK**~~ | **Forbidden while unattended** — nobody is there to answer. Converted to PARK. | — |

The discipline that keeps "unsure" rare is **deciding PROCEED vs PARK by blast radius**, made concrete
so the agent isn't guessing about whether to guess:

- **Hard-PARK (never guess):** DB schema / migration direction, public or shared API contract,
  security / auth / tenant-isolation boundary, money / billing / pricing, a cross-ticket interface
  others build on, and *requirement meaning* (you don't know **what** to build) — unless it is both
  locally reversible and isolated, in which case build it reversibly behind a flag **and** park the
  decision (a hybrid).
- **PROCEED:** naming, internal structure, log/comment/error wording, test fixtures, a choice between
  two equivalent local implementations, trivially-toggled defaults.

A wrongly-parked small item costs a five-second decision afterwards; a wrongly-assumed big one costs a
run of wrong work. **PARK is the safe default:** anything that does not clearly meet the PROCEED bar
(or the HALT bar) is parked, so the contract covers the whole decision space rather than leaving a gap.
Blast-radius classification is the system's weakest link — it is the agent's judgment, helped by the
harness auto-classifier — which is exactly why every PROCEED change is made reversible (below).

This contract is enforced **structurally**, not by trusting the agent's 2 a.m. discipline — see the
architecture below. The guarantees are opt-in and env-gated (`CLAUDE_UNATTENDED=1`), completely inert
in normal interactive sessions.

## 5. Scope boundary — what ANS owns, and what it does NOT

This boundary is the most important thing to understand about ANS, because it is what makes the rest of
the design coherent. **ANS owns execution only.**

**ANS is responsible for:**

- **Execution governance** — the ASK/PARK/HALT contract and how an autonomous run behaves.
- **Scheduling & autonomy** — when work proceeds, defers, or halts; the per-ticket state machine.
- **Resilience & recoverability** — resume-safe state, attempt/loop caps, watchdog restarts.
- **Reversibility** — git-backed snapshot/revert; every PROCEED assumption committed.
- **Workflow continuity** — one unanswerable question never stalls the rest of the backlog.
- **Deterministic execution** — the gate (your test suite) is the only HARD block; outcomes are
  recorded atomically.
- **Operational safety** — deny-hooks that block irreversible/outward actions at the tool layer;
  secret redaction.

**ANS is explicitly NOT responsible for:**

- **Code generation** — the coding agent writes the code; ANS never does.
- **Model quality or AI reasoning** — ANS does not make the agent smarter or judge its thinking.
- **Consensus** — multi-model agreement is a separate concern, owned elsewhere.
- **Verification / correctness** — ANS does not decide whether a diff is *right*. Your deterministic
  gate catches regressions; a second opinion on a high-risk diff is *delegated* (next section).

When ANS needs any of these, it **delegates** them to a separate building block and uses only the
result — it never absorbs the responsibility. Clean separation of concerns: the governor governs; the
worker works; verification verifies; each is independently reasoned about, swapped, and trusted.

### Delegated second opinion (NOT an ANS capability)

ANS does **not** verify code or reason about correctness. For a genuinely high-risk diff it can
optionally **delegate** a second opinion to an external verification/consensus layer — the **Tokonomix
Council MCP**, a separate, standalone building block — and it uses that verdict for **one** purpose
only: deciding whether to mark the ticket `DONE` (trusted) or `DONE_LOW_CONFIDENCE` + **NEEDS DAYLIGHT
REVIEW**.

What ANS owns here is purely deterministic governance, *not* verification:

- It **routes** the risk tier from the actual diff (changed files + content), not the ticket text.
- It applies a **budget gate** (per-run € cap — config key `per_night_euro_cap`, named for the classic
  overnight case — call-count cap, balance) before any delegation.
- It **disposes** the returned verdict: convert "concerns / errored / never ran" on a HEAVY-risk diff
  into `DONE_LOW_CONFIDENCE` instead of a silent `DONE`.

The multi-model reasoning itself happens **outside ANS**, in the Council MCP (reached by the agent
through the Tokonomix gateway — the harness is stdlib Python and cannot call LLMs). The delegated
review is **advisory: it never blocks the run and never reverts.** It can only withhold the "trusted"
stamp. Model agreement is not correctness — frontier models share training data and can be uniformly
wrong — so this is a recall amplifier and a flag, never a truth oracle. *Verification lives in the
Council; ANS governs the trust-or-flag decision around it.*

## 6. Architecture

The agent **is** the worker. ANS owns scheduling, safety, reversibility and bookkeeping. The pieces:

### Per-ticket state machine

Every ticket runs through a durable, resume-safe loop (`agents_never_sleep/driver.py`, `state.py`):

1. **Preflight** (`preflight.py`) measures capabilities — VCS/reversibility, platform, gates,
   execution mode, optional Tokonomix/Vault/Paperclip. A missing capability never stops the run; it
   lowers expected yield and raises conservatism. No VCS → establish a safety net (git init /
   timestamped backup) before any risky edit, or stay non-destructive.
2. **Decide** PROCEED / PARK / HALT by blast radius (`decide.py`). Unattended: ASK → PARK.
3. **Implement** — the agent edits files for exactly one PROCEED ticket.
4. **Gate** (`gates.py`) — deterministic, the only HARD gate (see below).
5. **Record** exactly one durable outcome (`state.py`) — atomic writes, resume-safe.
6. **Next ticket** — attempt/loop caps (`ledger.py`) force-park a cursed item; a low-yield circuit
   breaker stops and alerts if most work is parking/blocking.

### Deterministic gates — the only HARD gate

A **gate** is a shell command (your test suite) run after every edit. Exit 0 = green; non-zero = red.
The harness classifies a red as *introduced-by-the-diff* (revert to last green + park/fail) vs
*pre-existing / flaky / env* (downgrade confidence, keep the work, note it as a blind spot) via
snapshot comparison. Every gate runs with a per-step timeout and a non-interactive environment, so it
can never hang on a TTY prompt; a timeout yields `BLOCKED_ENV`, never a run halt. **The deterministic
gate is the only thing that can hard-block a ticket** — the delegated review (§5) is advisory and never
reverts. ANS never deletes or skips a failing test to go green; doing so is a blocking blind spot.

### Git-backed snapshot / revert

Each PROCEED ticket is snapshotted before edits (`vcs.py`); a red gate reverts to the last green
commit. If the snapshot commit cannot be made (git lock / timeout / read-only object store), the ticket
is recorded `BLOCKED_ENV` rather than edited unrevertibly. Every PROCEED assumption is committed so it
can be reverted afterwards.

**Revert-surviving scratchpad (opt-in).** A revert correctly rolls the *code* back to green — but the
agent's reasoning for that ticket would be lost, so on resume it re-derives from scratch. With
`autonomy.scratchpad.enabled`, the agent logs progress to a per-ticket `note` that lives outside the
reverted set (under `.unattended/`, gitignored + protected), so it **survives the revert** and is
re-injected — along with a compact *do-not-repeat* digest of the dead ends already tried this run —
so a resumed or fresh session continues its reasoning instead of repeating it. Default off → the
handout payload is byte-for-byte unchanged.

### Attempt / loop caps

`ledger.py` enforces a cross-resume attempt cap per ticket and detects provable loops, force-parking
anything that would otherwise burn the run on one cursed item. A low-yield breaker halts the run and
alerts when most outcomes are parks/blocks.

### The launcher (`bin/ans-run`) — preflight + working-tree flock

`preflight.py` measures capabilities only *after* the agent session boots — by then the first tokens
are spent. For headless/cron launches, `bin/ans-run` (installed as `ans-run`) is a deterministic
GO/NO-GO gate that runs **before** the agent CLI boots:

- **Config trust (TOFU):** `.claude/agents-never-sleep.json` travels with the repo and describes
  commands the launcher will execute. A new/changed config must be trusted once per user (keyed on its
  SHA-256, recorded outside the repo). Headless + untrusted = NO-GO.
- **Identity / root-guard:** configurable `launcher.target_user`; started as root with a target user →
  re-exec as that user; as root with none configured → NO-GO.
- **Agent selection:** named, human-confirmed presets (`--agent`). No launch-time platform detection
  (env markers are spoofable and gone under cron). Each preset must pass a 5 s `--version` capability
  probe (catches flag drift before tokens are spent) and carry `autonomy_confirmed: true`.
- **Autonomy flags are an explicit human decision, never a default.** A detached run with permissions
  fully on stalls at the first approval prompt; the flag that prevents that grants real power
  (`--permission-mode acceptEdits`, `--sandbox workspace-write`, `--yolo`, `--allow-all-tools`). The
  wizard shows what the flag grants before the preset can be marked launchable, and a detached launch
  **preflight-verifies the resolved argv actually carries a non-interactive permission flag** — so a
  hand-edited config that keeps `autonomy_confirmed: true` but drops the flag is refused (NO-GO)
  instead of hanging silently at the first tool prompt.
- **Opt-in capability restriction:** a preset may declare a `capabilities` list (e.g.
  `--strict-mcp-config --mcp-config <file>`) so the agent loads only the MCP servers / tools a run
  needs — smaller memory footprint and attack surface. Absent = the full set (today's behaviour).
- **Atomic mutual exclusion:** a non-blocking `flock(2)` on `<repo>/.unattended/ans-run.lock`, held by
  the long-lived agent process and released by the kernel on any crash/kill. Two simultaneous starts →
  exactly one winner (no TOCTOU-racy pidfiles). Exit codes: `0` GO, `64` NO-GO, `65` tree busy.
- **Token-refs, never literal keys:** a preset's `env` can point the CLI at a gateway via `env:VAR` or
  `vault:<mount>/<path>[#field]`, resolved through the keysource and registered for redaction. A failed
  resolution is a blocking NO-GO, never a silent empty value.

### Delegated review hook — council & specialists (advisory; see §5)

When configured (and a Tokonomix gateway is reachable), a high-risk diff's second opinion is
**delegated** to the multi-model **council** (`council.py`) and specialist **lenses**
(`specialists.py`: architect/security always, plus tenant-safety/mobile/ux/i18n/performance/seo when
the diff touches them). This is a *delegated integration with the external verification layer, not an
ANS verification capability* — the harness owns only the deterministic budget/route/disposition
scaffolding; the model reasoning happens in the Council MCP. It is **advisory and never blocks the
run:** it only withholds the "trusted" stamp, recording a HEAVY-risk diff whose review raised concerns,
errored, or never ran as `DONE_LOW_CONFIDENCE` + **NEEDS DAYLIGHT REVIEW**. Per-run euro and
call-count caps brake the spend. See [Scope boundary](#5-scope-boundary--what-ans-owns-and-what-it-does-not).

### Watchdog, secret redaction, key source, Paperclip

- **Watchdog** (`watchdog.py`) — a sidecar that runs the agent as a child and restarts it resumably
  when the heartbeat goes stale (the hang a Stop-hook can't see — e.g. a run wedged by a sustained
  529/overload wave that freezes the heartbeat), up to a cap, then alerts and exits 75. **`ans-run`
  wraps every detached launch in it by default** (opt out with `--no-watchdog`), so an unattended run
  can recover from an overload freeze (a resumable restart, up to the cap) instead of sitting dead
  until you return. It also **reaps its own
  leaked child tree** — the agent's MCP servers (context7, etc.) that would otherwise accumulate
  toward OOM on a long run — strictly by *parent-chain lineage from the run's own pid*, **never by a
  name match** (a name match would also kill other users' / other projects' runs). Honest limit: a
  force-*killed* (SIGKILL) supervisor can't self-reap, so that residual leak is reduced, not
  eliminated. Composes with `claude-run`; never rewrites it.
- **Secret redaction** (`redact.py`) — every report, log, saved gate-output, Paperclip comment and
  emitted JSON is scrubbed of credentials by *shape* (tokens, JWTs, private keys, connection-string
  passwords) plus a registry of known secret values, without mangling ordinary text or git SHAs.
- **Vault key source** (`keysource.py`) — optional tokens resolve from env or HashiCorp Vault
  (AppRole / `VAULT_TOKEN`, KV-v2); a resolved value is auto-registered for redaction and degrades to a
  run-report blind spot, never a hard stop, when unreadable.
- **Paperclip integration** (`sources/paperclip.py`) — optionally pull open issues from one project as
  the work source and push per-ticket status transitions + parked/daylight comments back, with graceful
  degrade-to-local when the board can't be read.

These are all **built in 1.0**, not future phases.

## Design Principles

The engineering principles ANS is built on — each one is a concrete mechanism in the codebase above, not
an aspiration. (Distinct from the discipline's *values* in [Why Autonomous Execution Governance
matters](#why-autonomous-execution-governance-matters): those are what an autonomous system should
honour; these are how ANS is engineered.)

- **Single Responsibility.** ANS owns *execution governance* and nothing else — it does not write code,
  judge correctness, or pick models. Everything outside execution is delegated (see the
  [scope boundary](#5-scope-boundary--what-ans-owns-and-what-it-does-not)).
- **Separation of Concerns.** Worker (the coding agent), governor (ANS), and verifier (the delegated
  Council) are separate building blocks, each independently reasoned about, swapped, and trusted.
- **Determinism.** The only HARD gate is a deterministic shell command (your test suite), classified by
  snapshot comparison (`gates.py`) — green/red is mechanical, not a model's opinion.
- **Reversibility.** Every PROCEED ticket is git-snapshotted before edits and reverted on a red gate
  (`vcs.py`); every assumption is committed so it can be undone afterwards.
- **Least Privilege.** Token-refs resolve from env/Vault and never appear as literal keys
  (`keysource.py`); irreversible/outward actions are denied at the tool layer; autonomy flags are an
  explicit human decision, never a default (`bin/ans-run`).
- **Fail-Safe.** PARK is the safe default and HALT covers genuine irreversible danger — the contract
  covers the whole decision space, so "unclassifiable" defers rather than guesses (`decide.py`).
- **Auditability.** Exactly one durable outcome per ticket (`state.py`) feeds a single ranked run
  report (`report.py`); nothing the run did is left implicit.
- **Recovery.** A stale heartbeat is restarted resumably by the watchdog (`watchdog.py`); attempt/loop
  caps force-park a cursed item (`ledger.py`) — recovery over perfection.
- **Statefulness.** A durable, resume-safe per-ticket state machine (`driver.py`, `state.py`) survives
  crashes and fresh sessions; each `next`/`complete` is a fresh subprocess over persisted state.
- **Governance.** The run behaves by an explicit, enforced ASK/PARK/HALT contract (`enforce.py`,
  `enforcement.py`) — structural deny-hooks, not the agent's 2 a.m. good intentions.

## 7. Workflow

The harness cannot call the agent from inside a Python loop, so the agent drives a two-command loop
until the backlog drains. Each command prints one JSON object to stdout.

```bash
# Hand me ONE ready ticket (auto-parks ambiguous / high-blast-radius ones), or a terminal signal.
python3 -m agents_never_sleep.run next     --repo . --tickets <dir-of-.md-tickets>
#   …implement ONLY ticket.body by editing files in the repo…
python3 -m agents_never_sleep.run complete --repo . --attempted "one-line summary of what you did"
#   …repeat next/complete until next returns a terminal status.
```

`next` reads the JSON `status`:

- **`PROCEED`** → implement the ticket, then call `complete`. If the payload carried a `council` /
  `specialists` block (a delegated-review request — §5), feed the returned verdict back on `complete`
  (`--council-verdict pass|concerns|error --council-cost <€>`, `--specialist-concerns …`), or the
  advisory trust-gating silently never fires.
- **`DRAINED` / `HALTED` / `LOW_YIELD`** → the run is over; the run report is written. Stop.
- **`NON_DESTRUCTIVE`** → unattended with no saved config; do a configuring interactive run first.

`next` owns the never-stop sentinel that blocks a premature stop; never invent your own loop or stop
early. Operator escapes for a confused resume: `reset-attempts <id>` (clear one ticket's attempt
counter), `reset-spend` (zero the per-run spend accounting), `parked` (protect/restore parked WIP),
`report` (re-write the run report from the store). (There is **no `run` subcommand** — real runs
use `next`/`complete`.)

### Outcome states

Exactly one durable outcome is recorded per ticket:

| State | Meaning |
|---|---|
| `DONE` | Implemented, gate green. |
| `DONE_LOW_CONFIDENCE` | Implemented, gate green, but a HEAVY-risk diff's *delegated* review raised concerns / errored / never ran. **Needs daylight review.** |
| `PARKED_DECISION` | Requires a human decision before implementation. Parked cleanly. |
| `PARKED_FOUNDATIONAL` | Depends on a not-yet-completed prerequisite. |
| `BLOCKED_ENV` | Gate timed out / environment issue — not a code bug. |
| `FAILED_RETRYABLE` | Gate caught a bug the diff introduced; reverted, can retry. |
| `FAILED_BUG_IN_AGENT` | Repeated failures suggest a systematic problem. |

### The run report

A single ranked report (`report.py`): what's **done & trusted**, what **needs daylight review**, what's
**parked** (with candidate interpretations and the exact next action), what's **blocked**, and any
**blind spots** (a degraded guarantee, a missing review credential, an unresolved secret). A LOW-YIELD
run is flagged loudly so "the run finished" is never mistaken for "the work got done".

## 8. Installation

The harness is pure Python standard library — **zero runtime dependencies**.

**Today (PyPI not yet live):**

```bash
# From the tagged GitHub release:
pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.3.0

# Or from a checkout (editable, to hack on it):
git clone https://github.com/TokonoMix/agents-never-sleep
cd agents-never-sleep
pip install .          # or: pip install -e .
```

**Once published to PyPI** (publish is a pending, deliberate release step — not yet available):

```bash
pip install agents-never-sleep      # ← will work after the PyPI publish; not yet
```

Either install puts two console scripts on PATH: `ans` (= `python3 -m agents_never_sleep.run`, the
per-ticket loop) and `ans-run` (the preflight launcher). A checkout also works without installing — run
`bin/ans-run` and `python3 -m agents_never_sleep.run` directly (set `PYTHONPATH` to the skill root for
the latter).

> **Migration note (pre-1.0 → 1.0):** the import package was renamed `harness` → `agents_never_sleep`.
> A back-compat `harness` shim keeps the old form working (`import harness`, `python3 -m harness.run`,
> `-m harness.enforce`) **through all of 1.x** — it emits one `DeprecationWarning` and is removed in
> 2.0. New code should use `agents_never_sleep`.

## 9. Quick Start

Five minutes from zero to a first unattended run.

1. **Install** (above) — `pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.3.0`.
2. **Understand the contract:** unattended, the agent only ever **PROCEEDs** (assume + log + continue,
   reversibly), **PARKs** (defer this one ticket, keep going), or **HALTs** (only on irreversible
   danger). It never **ASKs** — there's nobody to answer. PARK keeps the run moving; that's the whole
   point.
3. **Write a few tickets** as `.md` files in a directory (see §10 for the format). The body is the only
   required content; the harness auto-classifies blast radius.
4. **First (interactive) run** to create the per-project config via the wizard, then drive the loop:

   ```bash
   cd /path/to/project
   python3 -m agents_never_sleep.run next     --repo . --tickets ./backlog
   #   …implement the ticket it hands you…
   python3 -m agents_never_sleep.run complete --repo . --attempted "what you did"
   #   …repeat until DRAINED.
   ```

5. **Integrate / go unattended:** install the Claude Code enforcement hooks (opt-in, `hooks/README.md`)
   so the contract is enforced structurally, then launch detached through `ans-run` (§10). For other
   platforms, see `hooks/platforms/README.md`.
6. **Read the run report** (`python3 -m agents_never_sleep.run report --repo .`): done & trusted,
   needs-daylight-review, parked, blocked, blind spots.

## 10. Examples & integration

### Drive a local backlog

Drive a backlog of local `.md` tickets from the repo root (`--repo .` keeps the never-stop sentinel
path auto-aligned):

```bash
cd /path/to/your/project

# Get a ticket
python3 -m agents_never_sleep.run next --repo . --tickets docs/backlog
# → {"status":"PROCEED","ticket":{"id":"add-rate-limit","body":"…","path":"…"},"snapshot":"<sha>", …}

#   …you (the agent) implement only that ticket…

# Record the outcome (gate runs here)
python3 -m agents_never_sleep.run complete --repo . --attempted "added token-bucket limiter + tests"
# → {"status":"RECORDED","ticket_id":"add-rate-limit","state":"DONE","next":"call `next`"}

# Loop until DRAINED / HALTED / LOW_YIELD, then read the report
python3 -m agents_never_sleep.run report --repo .
```

A ticket is a Markdown file with optional YAML front-matter (the body is the only required part):

```markdown
---
id: add-rate-limit
title: Add rate limiting to the public API
blast_radius: medium      # optional hint; the harness auto-classifies
---

Add a token-bucket rate limiter to POST /api/submit. 100 req/min per key.
Reject over-limit with HTTP 429 + Retry-After. Cover it with tests.
```

Launch a detached, headless run through the preflight launcher (installed as `ans-run`):

```bash
ans-run --repo /path/to/project --agent claude "work through the backlog unattended"
#   GO/NO-GO preflight runs BEFORE any token is spent; one winner per working tree.
ans-run --repo /path/to/project --check          # preflight report only; never starts/disturbs a run
```

Self-test the harness (hermetic, no credentials, no network):

```bash
for t in acceptance/test_*.py acceptance/run_acceptance.py; do
  python3 "$t" >/dev/null && echo "$t ✅" || echo "$t ❌"
done
```

### How a platform plugs in

ANS composes with coding agents — it is the governor, they are the worker. There are three distinct
ways a platform plugs in. **Be precise about which:** *live-verified*, *built-to-contract*, or
*portable preset*.

- **Hook-enforced platforms** (the enforcement matrix below) — the never-ASK / deny-irreversible /
  never-stop guarantees are wired into the platform's native hook system from one shared decision core
  (`agents_never_sleep/enforce.py`). **Claude Code is live-verified;** Gemini / Codex / Copilot /
  Cursor / Windsurf are **built to each platform's documented hook contract** (run `acceptance/` on the
  real tool to promote a cell).
- **Launcher-preset platforms** — selected via `--agent <preset>` in `bin/ans-run`, with autonomy flags
  confirmed once by a human. The shipped preset map (`agents_never_sleep/agent_clis.py`) keeps the
  autonomy flag and what it grants explicit:

  | CLI | unattended invocation | the autonomy flag grants |
  |---|---|---|
  | Claude Code | `claude -p --permission-mode acceptEdits` | file edits auto-approved; shell/network stay gated |
  | OpenAI Codex | `codex exec --sandbox workspace-write` | edits/commands inside the workspace sandbox |
  | Gemini | `gemini --yolo -p` | EVERYTHING — run in a container/throwaway checkout |
  | GitHub Copilot | `copilot --allow-all-tools -p` | everything (required for programmatic `-p`) |

- **Portable SKILL.md platforms** — OpenHands, CI/CD pipelines (GitHub Actions, etc.), and any of the
  30+ tools that read the open `SKILL.md` standard. There is **no bespoke enforcement adapter** for
  these in 1.0; ANS runs via the portable skill contract + the launcher preset, and any guarantee the
  host can't enforce natively is surfaced as a loud **BLIND SPOT** in the run report — never a
  silent gap.

**Aider** is a launcher-preset/**wrapper** adapter (`agents_never_sleep/aider_launcher.py`): Aider
0.86.2 has no hook/plugin API, so the guarantees are approximated with launch flags (`--yes-always`,
`stdin < /dev/null`, `--no-suggest-shell-commands`), git-revert reversibility, and the SKILL.md prose
contract. **Honest caveat:** a 2026-06-28 smoke-test showed Aider can hang on a network/OAuth wait that
stdin redirection does not defuse, so the Aider preset **requires** a hard wall-clock timeout (kill →
PARK) and a pre-flight that a model + key are configured. It is built-to-contract, **not** live-verified
to the standard of the hook-enforced platforms.

**CI/CD:** run `ans-run` as a step (or the `next`/`complete` loop directly) as the job's user; gate on
`ans-run --check`; the working-tree flock makes concurrent jobs safe.

### Enforcement capability matrix

| Platform | deny-irreversible | never-stop | never-ASK | status |
|---|:---:|:---:|:---:|---|
| **Claude Code** | ✅ | ✅ | ✅ | **live-verified** |
| **GitHub Copilot CLI** | ✅ | ✅ | ✅ | built-to-contract |
| **Gemini CLI** | ✅ | ✅ | 🟡 prose | built-to-contract |
| **OpenAI Codex CLI** | ✅ | ✅ | 🟡 prose | built-to-contract |
| **Cursor** | ✅ | 🟡 prose | 🟡 prose | built-to-contract |
| **Windsurf** | ✅ | 🟡 prose | 🟡 prose | built-to-contract |

✅ = the platform's native hook enforces the guarantee at the tool layer. 🟡 prose = the platform
exposes **no native hook** for that guarantee (a limitation of the host CLI, not of ANS) → the skill
falls back to the SKILL.md written contract and reports any residual gap as a loud BLIND SPOT. **Only
Claude Code is live-verified on the real tool** (`capabilities.py: LIVE_VERIFIED`); the other five are
built to the platform's documented hook contract and verified by the hermetic test suite — run
`acceptance/` there to promote a cell.

Two further built-to-contract adapters ship under `hooks/platforms/` — **Crush** and **OpenCode**
(deny-irreversible native, never-stop / never-ASK in the prose-fallback class, same as Cursor/Windsurf;
OpenCode has a documented caveat that subagent/task-tool calls bypass the deny hook). An internal
**Hermes** in-process adapter also exists. None of these are live-verified yet.

## 11. ANS in the Tokonomix ecosystem

ANS is one of a planned family of specialized Tokonomix building blocks — each standalone-usable, each
owning a single responsibility, each composable with the others. The point of the [scope
boundary](#5-scope-boundary--what-ans-owns-and-what-it-does-not) is that ANS stays inside *execution*
and delegates everything else to the block that owns it:

| Building block | Responsibility |
|---|---|
| **ANS** | **execution** — autonomous-run governance, scheduling, resilience, reversibility (this repo) |
| **Council** | **decision-making** — multi-model deliberation; a *delegated second opinion* on a high-risk diff (Tokonomix Council MCP) |
| **Media QC** | **verification** — quality control of generated media |
| **Benchmark** | **measurement** — reproducible evaluation harnesses |
| **Routing** | **provider-selection** — choosing which model/provider serves a call |
| **Memory** | **long-term context** — durable knowledge across sessions |

Each block is usable on its own, but states its place. The seam that matters for ANS: when a run wants
a second opinion on whether a high-risk diff is sound, ANS does not reason about correctness itself — it
**delegates to Council** and consumes only the verdict (trust → `DONE`, or flag →
`DONE_LOW_CONFIDENCE`). **For verification / decision-making, look to Council, not ANS.** This is
deliberate: a governor that also tried to be a judge would be neither cleanly.

## 12. Best Practices

- **Backlog shape.** Independent tickets parallelize cleanly across the run; coupled tickets that share
  an interface should name the interface explicitly (or Hard-PARK the interface decision so it isn't
  guessed differently by two tickets).
- **When to PARK vs PROCEED.** PROCEED only when the choice is *locally reversible and isolated*
  (naming, internal structure, equivalent local implementations). Hard-PARK anything in the
  high-blast-radius classes (schema/migration, auth/tenant boundary, money, public API, cross-ticket
  interface, unclear requirement meaning). When genuinely unclassifiable → PARK.
- **Fresh session per N for long backlogs.** A single agent session degrades as it accumulates context
  (empirically around ticket ~19 it starts deferring large work and losing earlier constraints). The
  *harness state* never degrades — each `next`/`complete` is a fresh subprocess — but the driving agent
  context does. For long, independent backlogs set `launcher.fresh_session_every: N` (opt-in,
  default 0/off) so a fresh agent session takes over every N tickets and re-reads the durable state,
  giving ticket 40 the same quality as ticket 1. Never use a mid-task %-compaction trigger — it
  summarizes lossily, busts the prompt cache, and drops design constraints.
- **Delegated-review cadence.** When the delegated council review (§5) is enabled, the default caps are
  sized for ~3 calls per ticket (plan / mid / diff review). Per-call review on every change is possible
  but only worth it when explicitly requested, given the per-run euro and call caps. Always report
  the real charged cost (`--council-cost`) so the spend brake stays accurate.

## 13. FAQ

**Does ANS replace Claude Code / Codex / Cursor / Aider?**
No. ANS is a layer *above* them. They are the worker that writes the code; ANS governs how that worker
behaves across a long unattended run. It complements your coding agent, it does not compete with it —
and it is not a model or an IDE.

**Does it only work overnight?**
No. "Overnight" is the obvious case, but it works just as well during the day — hand off the backlog
and do other work while it runs, without watching every step.

**Does ANS verify that my code is correct?**
No — and that is by design (§5). ANS owns execution governance, not verification. Your deterministic
gate (test suite) catches regressions; for a high-risk diff, a second opinion is *delegated* to the
Tokonomix Council MCP and used only to flag `DONE_LOW_CONFIDENCE` / NEEDS DAYLIGHT REVIEW. ANS never
claims a diff is correct.

**Does it need an LLM API key of its own?**
No. The harness is provider-neutral stdlib Python. The *agent* you drive has whatever credentials it
already uses. The optional delegated review needs the Tokonomix gateway (or your own provider keys);
the DIY path stays fully functional without it.

**What happens if it can't undo something?**
It doesn't get there. Deny-hooks block irreversible/outward actions at the source (force-push, remote
branch deletes, destructive SQL, secret deletion, disk wipes). If there's no reversibility safety net at
all and none creatable, the run HALTs rather than proceed.

**Can I run two at once on the same repo?**
The launcher's atomic working-tree lock yields exactly one winner per working tree. For intentionally
disjoint worktrees, opt out with `ANS_RUN_NO_LOCK=1`.

### Limitations (read this)

ANS is a governance layer, not a correctness oracle. Concretely:

- **It does not guarantee the code is correct.** The deterministic gate (your test suite) is the **only
  HARD gate**, and ANS does not replace it — regression-catching is exactly your tests' job. What ANS
  adds is orthogonal: it governs *whether an autonomous agent should have touched a given surface at
  all*, and keeps every change it does make reversible.
- **The delegated review is advisory.** The Tokonomix Council second opinion (§5) can raise concerns
  and withhold the "trusted" stamp (`DONE_LOW_CONFIDENCE` + NEEDS DAYLIGHT REVIEW), but it never blocks
  the run and never reverts. Model agreement is not correctness — frontier models share training data
  and can be uniformly wrong. Verification is delegated, not guaranteed.
- **PARK can defer real work.** A run that parks heavily is honest about it (LOW-YIELD flag, ranked
  report), but you can still come back to a run where most tickets are waiting on your decisions.
- **A wrong PROCEED assumption is possible.** Blast-radius tiering reduces the odds, but a misjudged
  PROCEED can be wrong. **This is precisely why every PROCEED change is built to be reversible** —
  git-backed snapshot/revert and every assumption committed — while the genuinely irreversible
  operations are blocked outright by deny-hooks (they HALT). So a wrong call during a run is a
  five-minute revert afterwards, not a disaster.
- **Cross-platform enforcement is live-verified only on Claude Code.** Everywhere else it is built to
  the platform's documented hook contract and hermetically tested, but not yet confirmed on the real
  tool.

### Safety posture — honest status

ANS is a governance layer, not a security product. Here is what each protection actually is:

- **Primary protection: your execution environment.** Run the agent in a container, a throwaway
  checkout, or a least-privilege CI user. ANS cannot substitute for that; it assumes you have it.
- **Deny-hooks (secondary):** on Claude Code (live-verified), the hook fires at the tool layer and
  blocks irreversible/outward actions (force-push, destructive SQL, secret deletion, disk wipes)
  before they execute. On other platforms (built-to-contract, not yet live-verified on the real tool),
  the same decision core runs but whether the native hook fires is not confirmed. Any gap is reported
  as a loud BLIND SPOT, never a silent one.
- **Secret redaction:** scrubs credentials from all reports, logs, and Paperclip comments — by shape
  and by a registry of known values. It does not guarantee zero-leakage; it is a defence-in-depth
  layer.
- **Config trust (TOFU):** `.claude/agents-never-sleep.json` must be explicitly trusted before a
  headless run; a changed config re-gates. This prevents a compromised config from silently changing
  what the launcher executes, but it is not a cryptographic supply-chain guarantee.
- **The state machine is verified by the acceptance suite** (`acceptance/test_*.py`) — the
  PROCEED/PARK/HALT/ASK enforcement is mechanically tested. Architecture and governance are
  well-reasoned; correctness of the implementation is what the hermetic tests check.

## 14. Benchmarks — methodology, not claimed results

> **Honesty note:** the autonomy metrics below are a **reproducible methodology**, not results we are
> claiming. Most are **not yet measured**. This section describes *how to measure* unattended-run
> autonomy and the harness to do it. When we run them for real, results will go in a clearly-dated,
> reproducible appendix with the exact setup — never inline as a bare number.

The metric of interest is an **autonomy-index** — a function of:

- **continuous runtime** without a human touch,
- **tickets completed** per run / per session,
- **human interruptions** (target: 0),
- **recovery after failure** (does a red gate / crash / stale heartbeat resume cleanly?),
- **unfinished tickets** (parked + blocked, with reasons).

**Measurement procedure:** run a fixed, controlled backlog twice — once with a normal agent, once with
ANS — on the same repo + gate, and record the five quantities above. The reproducible harness lives in
`acceptance/` (hermetic `test_*.py` + `run_acceptance.py`, exit 0 = green), which exercises the loop
end-to-end with a deterministic worker.

**What each metric does and doesn't prove:** continuous runtime and zero-interruption show the
never-stop/never-ASK contract held; tickets-completed and unfinished-tickets show *throughput vs
deferral* (a high park rate is honest, not necessarily a win); recovery-after-failure shows the
durability spine works. None of these is a code-correctness measure — that is the gate's job (and the
delegated Council's concern), and it is out of scope for an autonomy benchmark.

A controlled "normal-agent vs ANS on a backlog" comparison is scoped as the **first reproducible
experiment** (tracked internally as Paperclip `18eee818`); it will be folded in here *when run*, with
its setup, not before.

## 15. Roadmap

Direction, not promises. The current published state is the baseline.

- **PyPI publish.** 1.3.0 is distributed via the GitHub release today; a bare
  `pip install agents-never-sleep` becomes available once the package is published to PyPI (a
  deliberate, separate release step).
- **More live-verified platforms.** Today only Claude Code is live-verified. Gemini / Codex / Copilot /
  Cursor / Windsurf are built-to-contract; promoting each to live-verified is a ~5-minute smoke-test on
  the real tool (`hooks/platforms/README.md`). Aider (wrapper preset) hardening — particularly the
  network/OAuth hang — is on the same track.
- **Run the benchmark methodology for real** (§14) and publish a dated, reproducible appendix.
- **Deprecation cleanup:** the `harness` back-compat shim is removed in 2.0; `agents_never_sleep` is the
  going-forward import name.

The exact, checkable surface-stability policy is in `SEMVER.md`; the per-version record is in
`CHANGELOG.md`.

## 16. Glossary

Consistent terminology — for human readers and for AI systems parsing this README.

| Term | Meaning |
|---|---|
| **Autonomous Execution Governance** | The category ANS names and proposes (not an externally settled standard — no outside spec or second implementation yet): the operational layer that governs *how* an autonomous coding agent behaves during an unattended run (autonomy, reversibility, resilience) — distinct from code generation, model quality, or verification. |
| **Autonomy contract** | The ASK / PARK / HALT rule set that gives an unattended run a principled, never-collapsed response to every kind of uncertainty. |
| **ASK** | "I need a human to decide." **Forbidden while unattended** (nobody is there to answer) → automatically converted to PARK. |
| **PROCEED** | "Assume, log, and continue" — chosen only for low-blast-radius, reversible, isolated choices. The assumption is committed so it can be reverted. |
| **PARK** | "Defer *this one* ticket/decision and move to the next." Keeps the run moving; records the reason, candidate interpretations, the exact human next-action, and the contamination scope. Not a stop. |
| **HALT** | "Stop the *whole run*." Reserved for genuinely irreversible danger with no reversibility safety net. |
| **Blast radius** | How far a wrong choice can spread. Large blast radius (schema, auth, money, public API, shared interface, unclear requirement) → Hard-PARK; small + reversible + isolated → PROCEED. The primary PROCEED-vs-PARK discriminator. |
| **Deterministic gate** | A shell command (your test suite) run after every edit: exit 0 = green, non-zero = red. **The only HARD gate** — the one thing that can block a ticket. |
| **Delegated second opinion** | An optional, advisory multi-model review of a high-risk diff, *delegated* to the external Tokonomix Council MCP. Used only to flag trust vs `DONE_LOW_CONFIDENCE`; never blocks the run, never reverts, never owned by ANS. |
| **Outcome state** | The single durable verdict recorded per ticket (`DONE`, `DONE_LOW_CONFIDENCE`, `PARKED_DECISION`, `PARKED_FOUNDATIONAL`, `BLOCKED_ENV`, `FAILED_RETRYABLE`, `FAILED_BUG_IN_AGENT`). |
| **DONE_LOW_CONFIDENCE** | A green-gated diff that the delegated review flagged (concerns / errored / never ran) on a HEAVY-risk change → **NEEDS DAYLIGHT REVIEW** rather than a silent `DONE`. |
| **Blind spot** | A degraded guarantee surfaced loudly in the run report (a missing capability, an unreadable secret, a host that can't natively enforce a guarantee) — never a silent gap. |
| **Reversibility** | The property ANS preserves: every PROCEED change is git-snapshotted and committed so it can be reverted afterwards; irreversible operations are blocked outright. |
| **Run report** | The single ranked end-of-run summary: done & trusted, needs-daylight-review, parked (with next actions), blocked, blind spots. |
| **Launcher (`ans-run`)** | The pre-token GO/NO-GO preflight + atomic working-tree lock that gates a headless/cron run *before* the agent CLI boots. |
| **Watchdog** | The sidecar that restarts a stalled unattended run resumably when its heartbeat goes stale. |
| **Live-verified vs built-to-contract** | *Live-verified* = enforcement confirmed firing on the real tool (only Claude Code today). *Built-to-contract* = built to the platform's documented hook contract and hermetically tested, but not yet confirmed on the real tool. |

The full, term-by-term reference (with the module each term lives in) is in the
[Glossary](docs/glossary.md).

---

## Documentation

The deep-dive docs live in [`docs/`](docs/). Each is dual-audience (a senior engineer *and* an AI system
parsing it) and verified against the `agents_never_sleep/` source for v1.3.0.

**Foundations**
- [Manifesto](docs/manifesto.md) — the *Autonomous Execution Governance* discipline: emergence, the ten principles, design principles, the Git-analogy thesis.
- [Glossary](docs/glossary.md) — every ANS term defined precisely, with its module.

**How it works**
- [Architecture](docs/architecture.md) — the components and how they compose.
- [Execution Model](docs/execution-model.md) — the `next` → implement → `complete` loop; the agent-is-the-worker design.
- [Governance](docs/governance.md) — why a governance layer; the autonomy contract as policy.
- [Decision Model](docs/decision-model.md) — how PROCEED / PARK / HALT is decided; ASK → PARK unattended.
- [Blast Radius](docs/blast-radius.md) — the Hard-PARK vs PROCEED tiering; classification as the weakest link.

**The machinery**
- [State Machine](docs/state-machine.md) — the seven durable outcome states.
- [Recovery](docs/recovery.md) — resume-safety, attempt caps, loop detection, the low-yield breaker.
- [Scheduling](docs/scheduling.md) — independent-next scheduling and anti-starvation.
- [Deterministic Gates](docs/deterministic-gates.md) — the only hard gate and its failure taxonomy.
- [Launcher](docs/launcher.md) — the pre-token GO/NO-GO gate and the working-tree flock.
- [Watchdog](docs/watchdog.md) — restarting a hung run.
- [Security](docs/security.md) · [Secrets](docs/secrets.md) — least privilege, TOFU config-trust, secret redaction, the keysource.

**Reference & getting started**
- [Benchmarks](docs/benchmarks.md) — the reproducible methodology (not claimed results).
- [Roadmap](docs/roadmap.md) — what is built, what is next.
- [Getting Started tutorial](docs/tutorial-getting-started.md) — run your first backlog end-to-end.

---

## Layout

```
SKILL.md                     the portable skill (read by the agent)
AGENTS.md                    router for file-based agents
bin/ans-run                  launcher: pre-token GO/NO-GO preflight + atomic working-tree lock
agents_never_sleep/          stdlib-Python engine (state machine, gates, driver, council, …)
  enforcement.py             shared cross-platform decision core
  enforce.py                 cross-platform hook dispatcher
  capabilities.py            per-platform capability matrix + degradation reporting
harness/                     back-compat shim for the old `harness` import name (removed in 2.0)
hooks/                       Claude bash hooks + platforms/ config snippets
acceptance/                  hermetic acceptance tests (run each test_*.py; exit 0 = green)
references/                  design docs
```

## License

MIT — see `LICENSE`.
