# agents-never-sleep

### Let a coding agent work your backlog **overnight, alone — without ever stalling on a question.**

> The pain: *"the agent stops at 2 a.m. with a dumb question and wastes the whole night."*
> This skill makes that structurally impossible.

`agents-never-sleep` is a portable [Agent Skill](https://www.agensi.io/learn/agent-skills-open-standard)
plus a small zero-dependency Python harness. You hand it a backlog (local `.md` tickets or a tracker);
it works ticket-by-ticket to completion while you sleep, makes its own judgment calls inside safe
limits, never does anything irreversible unsupervised, and leaves a **morning report** of exactly what
it did, what it parked for you, and why.

It runs on **Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub Copilot CLI, Cursor and Windsurf** —
one engine, per-platform enforcement.

---

## Why it exists

A normal agent, left alone on a big job, fails in one of two ways: it **stops** at the first ambiguity
(and the night is wasted), or it **charges ahead** and does something destructive you can't undo. Both
come from collapsing every uncertainty into a single response.

This skill keeps the three responses to uncertainty **separate** — that's the whole idea:

| | Meaning | Example |
|---|---|---|
| **PROCEED** | assume + log + keep going (low blast-radius, reversible) | naming, internal structure, a local refactor |
| **PARK** | defer *this one* ticket, move to the next — normal and healthy | "which DB migration direction?" → park, build the rest |
| **HALT** | stop the *whole* run (only on irreversible danger with no safety net) | no version control and none creatable |
| ~~**ASK**~~ | **forbidden while unattended** — there is nobody to answer at 2 a.m. | converted to PARK automatically |

The result: the night never idles on one cursed ticket, and nothing irreversible happens while you're
away.

---

## How it works

The agent **is** the worker; the harness owns the scheduling, safety and bookkeeping. You drive a tiny
two-command loop until the backlog drains:

```bash
python3 -m harness.run next     --repo <project> --tickets <dir>   # hands you ONE ready ticket…
#   …implement only that ticket…
python3 -m harness.run complete --repo <project> --attempted "what you did"   # gate + record
#   …repeat until DRAINED / HALTED / LOW_YIELD.
```

Under the hood, every ticket goes through a durable state machine:

1. **Preflight** measures what's available (version control, test gates, secrets backends) — a missing
   capability never stops the run, it just lowers ambition and raises caution.
2. **Decide** PROCEED / PARK / HALT by **blast radius**. High-stakes classes (schema, auth, money,
   public APIs, cross-ticket contracts) are *hard-park* — never guessed.
3. **Implement** the one PROCEED ticket.
4. **Gate** deterministically (your test suite) with a failure taxonomy: a failure the diff *introduced*
   → revert to last-green + park; a *pre-existing* / flaky / environment failure → keep the work but
   lower confidence. Gates run non-interactively with timeouts, so they can never hang.
5. **Record** exactly one durable outcome (DONE, DONE_LOW_CONFIDENCE, PARKED_*, BLOCKED_ENV, FAILED_*).
   Atomic + resume-safe: kill the process mid-run and it picks up cleanly.
6. **Anti-starvation:** per-ticket attempt caps and loop detection force-park a cursed ticket so the
   night is never burned on one item; a low-yield circuit-breaker stops + alerts if most work is parking.

Optional **multi-model review** (via the Tokonomix gateway): high-risk diffs are reviewed by a council
of frontier models and specialist lenses (architect / security / tenant-safety / …). It's *advisory* —
it never blocks the run, it only withholds the "trusted" stamp: an unvetted high-risk change is marked
**NEEDS DAYLIGHT REVIEW** in the report instead of a silent "done". Per-night cost brakes included.

---

## Security & safety model

Safety is enforced at the **code layer**, not by trusting the agent's 2 a.m. discipline:

- 🛑 **never-ASK** — the "ask the human" tool is denied; the agent is steered into PARK/PROCEED instead.
- 🧨 **deny-irreversible** — destructive / outward commands are blocked at the source: force-push,
  remote branch deletes, `rm -rf /`, destructive SQL, disk wipes, secret deletion/rotation, sending
  real email, service/volume teardown. (The harness's *own* `git reset --hard` / `git clean` revert is
  deliberately allowed.)
- ⏳ **never-stop** — a premature end-of-turn is blocked while the backlog isn't drained (with an
  anti-infinite-loop backstop).
- 🔑 **secret redaction** — every report, log, saved test-output and outward message is scrubbed of
  credentials by *shape* (tokens, JWTs, private keys, connection-string passwords, …) plus a registry
  of known secret values — without mangling ordinary text or git SHAs.
- ♻️ **git-backed reversibility** — snapshot before each ticket, revert on a red gate; no version control
  → it establishes a safety net first, or stays non-destructive.
- 🔐 **Vault-aware** — optional tokens (tracker, gateway) resolve from env *or* HashiCorp Vault, and any
  resolved value is auto-registered for redaction.

Every guarantee is **opt-in and env-gated** — completely inert in your normal interactive sessions until
you switch a run to unattended mode.

---

## Per-agent differences (honest capability matrix)

The harness is identical everywhere; **enforcement uses each platform's native hook system**. Where a
platform has no hook for a given guarantee, the skill falls back to the written contract **and reports a
loud BLIND SPOT in the morning report — never a silent gap.** (Strategy: *best-effort + graceful
degradation*.)

| Platform | deny-irreversible | never-stop | never-ASK |
|---|:---:|:---:|:---:|
| **Claude Code** | ✅ native | ✅ native | ✅ native |
| **GitHub Copilot CLI** | ✅ native | ✅ native | ✅ native |
| **Gemini CLI** | ✅ native | ✅ native | ⚠️ prose-contract |
| **OpenAI Codex CLI** | ✅ native | ✅ native | ⚠️ prose-contract |
| **Cursor** | ✅ native | ⚠️ prose-contract | ⚠️ prose-contract |
| **Windsurf** | ✅ native | ⚠️ prose-contract | ⚠️ prose-contract |

- **deny-irreversible works everywhere** — the most important guarantee is universal.
- **never-stop** is native on all but Cursor/Windsurf (their stop hooks can't block).
- **never-ASK** is native only where the platform exposes an "ask the user" tool with a pre-hook
  (Claude, Copilot). Elsewhere it's the written contract + a reported blind spot.

⚠️ cells fall back gracefully; the run tells you, up front and in the report, exactly which guarantees
are native vs degraded on the platform you're using.

> **Verification status:** the Claude adapter is live-proven. The other five are built to each
> platform's *documented* hook contract and proven by an automated test suite (correct deny/block shape
> per platform); a ~5-minute live smoke-test on each real tool is the final confirmation step and is
> documented in `hooks/platforms/README.md`.

---

## What you get in the morning

A single ranked report: what's **done & trusted**, what **needs daylight review** (gates passed but a
high-risk change wasn't cleanly vetted), what's **parked** (with the candidate interpretations and the
exact next action for you), what's **blocked**, and any **blind spots** (a degraded guarantee, a missing
review credential, an unresolved secret). A LOW-YIELD night is flagged loudly so "the run finished" can
never be mistaken for "the work got done".

---

## At a glance

- ✅ Runs a backlog to completion unattended, overnight, without stalling on questions
- ✅ PROCEED / PARK / HALT autonomy contract — never ASK, never block the night on one ticket
- ✅ Deterministic test-gates with a failure taxonomy; git-backed undo
- ✅ Code-layer security: never-ASK, deny-irreversible, never-stop, secret redaction
- ✅ Optional multi-model + specialist review with cost brakes (advisory, fail-safe)
- ✅ Six agent platforms, one engine, honest per-platform capability reporting
- ✅ Zero runtime dependencies (Python stdlib); portable open SKILL.md standard
- ✅ MIT licensed

*Start at `README.md` (install + the loop), `SKILL.md` (the full agent-facing contract), or
`references/cross-platform-enforcement-design.md` (the per-platform hook research).*
