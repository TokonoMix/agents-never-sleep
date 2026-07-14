# Tutorial — workflow patterns (feature backlog, bugfix sweep, refactor, migration)

> **30-second version.** ANS runs whatever backlog you hand it — but *how you shape the tickets*
> decides how much of the run ends `DONE` versus parked. This tutorial shows four real work shapes
> and how to cut each into unattended-safe tickets: a **feature backlog** (independent, right-sized,
> blast radius declared), a **bugfix sweep** (repro-first, so the gate proves the fix), a
> **refactor** (behavior-preserving steps, never big-bang), and a **migration** (the decision parks
> by contract; reversible scaffolding can proceed). The rules referenced here are the
> [decision model](../decision-model.md) and the [state machine](../state-machine.md) — this page
> applies them, it does not replace them (terms: [glossary](../glossary.md)).

The mechanics in one line: for every ticket the classifier (`decide.py`) picks
**PROCEED / PARK / HALT** from the ticket *text* by blast radius; a PROCEED is snapshotted,
implemented, and judged by the deterministic gate; every ticket ends in exactly one durable
[outcome state](../state-machine.md#the-seven-outcome-states); and the run report groups those
outcomes with, per parked item, the recorded `why`, the category, and the exact
`human_action_required`. Keep that pipeline in mind — each pattern below is just a way of shaping
work so it flows through it cleanly.

## Pattern A — feature backlog

**The shape.** A set of additive features, each deliverable on its own: new endpoints on an
existing contract, a new module, a template, a CLI subcommand.

**What to put in the ticket body:**

- **One reversible change per ticket.** A ticket is the unit of snapshot, gate, revert, attempt
  cap, and report line. "Add the CSV exporter with tests" is a ticket; "build the reporting
  subsystem" is a project wearing a ticket's clothes.
- **Make tickets independent.** The scheduler hands the agent only a next ticket whose
  contamination scope does not intersect a parked one — independence is what lets the run keep
  moving when one item parks. If B needs A's result, say so in B ("depends on A's interface") so a
  foundational park of A quarantines B instead of letting the agent build on an unknown.
- **Declare the blast radius honestly.** Classification matches the ticket *text*. If the feature
  touches an enumerated high-risk area (schema, public API contract, auth/tenant, billing,
  cross-ticket interface), name it — the correct outcome is a PARK you decide in daylight, not a
  PROCEED that slipped past the keywords. Hiding a schema change behind innocuous wording is the
  classifier's [documented weak spot](../decision-model.md#limitations); don't exploit it against
  your own run.
- **State the acceptance check.** The gate is the arbiter, so say what green means: "suite passes,
  including a new test for X".

**What the harness does.** Clean, low-blast-radius feature tickets take the PROCEED path:
snapshot → implement → gate. Gate green → `DONE` (or `DONE_LOW_CONFIDENCE` when review coverage was
degraded). A gate failure introduced by the diff reverts the edit and records `FAILED_RETRYABLE`,
under the attempt cap and loop detection described in [scheduling](../scheduling.md).

**What lands in the report.** Each ticket is one line under its outcome state; `DONE` items you can
trust to the gate's level, `DONE_LOW_CONFIDENCE` items carry a needs-daylight-review flag.

## Pattern B — bugfix sweep

**The shape.** A pile of known bugs, each small, spread across the codebase.

**What to put in the ticket body:**

- **Repro first, in the same ticket.** Instruct: "write a failing test that reproduces the bug,
  then fix until it passes." The deterministic gate can only prove a fix if the failing test exists
  inside the ticket's own diff — a fix without a repro test is `DONE` by gate-green, but the gate
  never actually witnessed the bug.
- **One bug per ticket.** Distinct bugs fail independently; batching five bugs means one stubborn
  repro can burn the batch's attempts and revert four good fixes with it.
- **Paste the evidence.** The observed behavior, the expected behavior, the error text. Unattended,
  the agent cannot ask you what "sometimes wrong" means — an under-specified bug ticket signals
  requirement-meaning ambiguity, and that classifies as a PARK, not a fix.

**What the harness does.** The suite is the gate, so "the failing test goes green" is literally the
completion condition: a fix that doesn't hold makes the gate red *on the diff*, which reverts the
edit (`FAILED_RETRYABLE`) rather than leaving a half-fix in the tree. A bug whose same fix fails
the same way repeatedly trips loop detection and force-parks instead of eating the run.

**What lands in the report.** Fixed bugs under `DONE` with the attempted summary; stubborn ones
under `FAILED_RETRYABLE` or force-parked with the failure recorded — a triage list, not a mystery.

## Pattern C — refactor

**The shape.** Internal restructuring that must not change behavior: extract a module, rename
across a package, replace a hand-rolled helper with a library.

**What to put in the ticket body:**

- **Behavior-preserving, in steps.** Each ticket is one mechanical, gate-checkable step ("extract
  X into Y; all call sites updated; suite green"), not "modernize the module".
- **Gates before refactors.** The gate is the *only* definition of "behavior preserved" the
  harness has. If the code you are refactoring has thin test coverage, the honest first ticket is
  "add characterization tests for X" — otherwise a green gate on the refactor certifies very little.
- **Say "no API changes".** A refactor that changes a public contract is not a refactor; if the
  ticket genuinely needs one, declare it and expect the PARK.

**Why big-bang refactors PARK.** A sweeping refactor is high blast radius by definition: it touches
shared interfaces other tickets depend on, which is an enumerated Hard-PARK category
(`cross_ticket_interface` — foundational, PACKAGE scope). A foundational park also quarantines
dependent tickets, so one big-bang refactor ticket can freeze a whole slice of the backlog. That is
the mechanism working as designed: the decision "restructure everything at once" is exactly the
kind a human should make. Cut the same work into behavior-preserving steps and it flows as a series
of PROCEEDs instead.

**What lands in the report.** Step tickets under `DONE`; a big-bang ticket under
`PARKED_FOUNDATIONAL` with its quarantined dependents listed.

## Pattern D — migration

**The shape.** A schema or data migration — add a column, split a table, backfill, change what a
field means.

**Why the decision parks, by contract.** `db_schema_or_migration` is an enumerated Hard-PARK
category: *foundational, SERVICE scope* — regardless of how reversible the ticket looks. A schema
is the foundation everything else stands on, and unattended there is nobody to confirm the choice
(ASK always becomes PARK). So the migration ticket does not run; it parks with the candidate
interpretations and the exact human next-action recorded, and every ticket whose contamination
scope intersects it is quarantined.

**How reversible scaffolding can still proceed.** Split the work so the *decision* and the
*scaffolding* are separate tickets:

- **The decision ticket** — "migrate accounts to the new address schema" — parks
  (`PARKED_FOUNDATIONAL`). That is its job: it is the recorded, daylight-ready decision.
- **Scaffolding tickets** — code-level preparation that is reversible and does not touch the
  schema: an adapter behind a config flag, the new read path coded against both shapes, tests for
  the target behavior. These are ordinary low-blast-radius PROCEED work.

Two honest caveats. First, classification is substring matching — a scaffolding ticket whose text
mentions "migration" will hard-park too. Either write the scaffolding ticket in terms of what it
does ("add a dual-read adapter behind `NEW_ADDRESS_MODEL`") or use the operator override
(`classify.overrides` in the project config) that exists precisely for this mis-hit. Second, the
built-in build-narrowly-behind-a-flag hybrid (recorded as `work_product_behind_flag`) applies only
on the requirement-ambiguity branch, [not on Hard-PARK categories](../decision-model.md#how-a-ticket-is-classified-the-decidepy-flow)
— for a migration, proceeding scaffolding is something you design into the backlog, not something
the classifier does for you.

**What lands in the report.** The decision ticket under `PARKED_FOUNDATIONAL` with its category,
`why`, and next-action — a decision you make in minutes over coffee; the scaffolding under `DONE`,
meaning the moment you make the call, the run has already built the ground for it.

## The common thread

All four patterns are the same discipline: **put the decision where the human is, and the
mechanical work where the agent is.** Small reversible tickets with declared blast radius flow to
`DONE`; genuinely high-stakes choices park loudly with everything needed to decide them fast. A run
that ends "12 DONE, 2 parked decisions" did not fail on the 2 — it converted them from 2am
assumptions into 5-second daylight calls.

Scope note: only Claude Code is the live-verified enforcement platform; adapters for other agent
CLIs are built to their documented hook contracts and are not live-verified. Install:
`pip install agents-never-sleep` (live on PyPI since v1.4.0), or pinned from GitHub
(`pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.0.0`).

---

*Verified against `agents_never_sleep/` (v1.0.0): `decide.py` (`HARD_PARK_CATEGORIES` incl.
`db_schema_or_migration` / `cross_ticket_interface` scopes, `AMBIGUITY_SIGNALS`, the F5
build-narrow branch limited to `requirement_meaning`, `classify.overrides`), `state.py`
(`OutcomeState`, `TicketOutcome` fields incl. `work_product_behind_flag`), `report.py` (per-state
sections, `human_action_required` / `category` lines), `orchestrator.py` + `ledger.py` (independent
next, quarantine, attempt cap, loop detection), and the linked docs.*
