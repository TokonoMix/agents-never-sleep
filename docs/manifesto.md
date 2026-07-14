# The Autonomous Execution Governance Manifesto

> **A reference document on a new engineering discipline — and its first concrete implementation.**
>
> Authored by the Tokonomix team for *Agents Never Sleep* (ANS) v1.0.0.
> This is not a README and not a product brochure. It is a position paper. It argues that a new
> engineering discipline — **Autonomous Execution Governance** — is emerging out of necessity, that it
> is distinct from the AI it governs, and that ANS is one concrete, falsifiable implementation of it.
>
> **Honesty bar.** Nothing here is a market claim. We say ANS *pioneers* and *defines* a category
> because that is a statement about design intent and structure, not about adoption. We do **not** claim
> it is "the industry standard." Every limitation is named in §9. Where a metric is mentioned, it is a
> *methodology*, never a result we are asserting. A claim we could not ground, we flag rather than make.

---

## Table of contents

1. [Thesis](#1-thesis)
2. [The emergence of a discipline](#2-the-emergence-of-a-discipline)
3. [Why existing AI agents cannot own this themselves](#3-why-existing-ai-agents-cannot-own-this-themselves)
4. [The ten principles](#4-the-ten-principles)
5. [The design principles](#5-the-design-principles)
6. [The Git analogy, developed rigorously](#6-the-git-analogy-developed-rigorously)
7. [ANS as the first concrete implementation](#7-ans-as-the-first-concrete-implementation)
8. [The road ahead and the open problems](#8-the-road-ahead-and-the-open-problems)
9. [Limitations and intellectual honesty](#9-limitations-and-intellectual-honesty)
10. [Closing](#10-closing)

---

## 1. Thesis

For the first time in the history of software engineering, we routinely hand non-trivial work to a
worker that can *act* on its own initiative and that we are *not watching*. A coding agent given a
backlog and left alone will read files, write code, run commands, edit a database, push to a remote.
It will do this for hours, across dozens of decisions, while the human who set it going is asleep or
elsewhere. This is genuinely new. We have automated builds, tests and deployments for two decades, but
those pipelines are *deterministic*: they do exactly the steps we wrote, in the order we wrote them. An
autonomous agent is different in kind. It *chooses*. It encounters situations we did not anticipate and
it decides what to do about them, unsupervised.

The central claim of this manifesto is simple and, we think, hard to escape once stated:

> **Autonomous execution is a capability we have acquired faster than we have acquired the means to
> govern it — and governing it is a distinct engineering problem from building the thing that executes.**

The instinct of the field has been to treat "the agent isn't reliable enough to leave alone" as a
*model* problem — something a stronger model, a better prompt, or more tools will eventually solve. We
argue this instinct is wrong, and that mistaking it has real cost. The failure that matters most for
unattended work is not that the model is too weak to make a good decision. It is that the model, however
strong, has **exactly one move when it is uncertain: stop and ask the human.** That single move is
correct when a human is present and catastrophic when one is not. No increase in model capability
changes the shape of that move. "I am uncertain" still collapses to a binary — *stop* or *guess* — and
for autonomous work neither branch is acceptable on its own. Stopping freezes an entire night of
independent work on one unanswered question. Guessing risks an irreversible action nobody can undo in
the morning.

What is missing is not intelligence. What is missing is a **governance layer** — a structural authority,
sitting *above* the agent, that converts the agent's overloaded "ask the human" into a disciplined,
enumerated decision: *which* uncertainties an autonomous run may resolve on its own (because the choice
is reversible and isolated), *which* it must defer to a human (because the blast radius is large), and
*which* must halt the whole run (because the action is genuinely irreversible). That layer must be
distinct from the agent, must be enforced rather than merely requested, must keep every permitted action
reversible, and must report honestly on what it did. We call the discipline that studies and builds such
layers **Autonomous Execution Governance**.

The rest of this document defends three propositions:

1. **A new discipline is emerging.** It follows the same pattern by which Version Control, CI/CD,
   Infrastructure-as-Code, DevOps and Observability each emerged — each arose to *govern a new
   capability we could no longer supervise by hand*. Autonomy is the next such capability (§2).
2. **The agent cannot own this governance itself.** Not because models are weak, but because an actor
   governing the limits of its own autonomy is a conflict of interest and a violation of separation of
   concerns (§3).
3. **The discipline reduces to a small set of principles, and those principles can be implemented
   concretely.** ANS is one such implementation: it instantiates the principles in a deterministic
   decision contract, a reversible execution spine, and a clean separation between *governing execution*
   and *judging correctness* (§4–§7).

A serious reader is entitled to be skeptical of any document that announces a "new discipline." The test
we hold ourselves to is the one DevOps, Observability and the others passed: *did a real, recurring
failure mode exist that the existing disciplines did not address, and did a coherent body of practice
form around addressing it?* We believe the answer for autonomy is yes, and §2 makes that case from the
history rather than from assertion.

---

## 2. The emergence of a discipline

Engineering disciplines are not invented by committee. They *precipitate* — out of a recurring,
expensive failure that the existing toolset cannot prevent, around a new capability the field has just
acquired and cannot yet supervise by hand. The pattern is consistent enough to be predictive.

**Version control** emerged when more than one person could change the same code. The new capability was
*parallel modification*; the new failure was lost work, silent overwrites, and an inability to answer
"who changed this, when, and why." Diffing files by eye did not scale past a couple of collaborators.
The discipline that precipitated — branching, merging, history, blame, the pull request — does not write
code and does not make anyone a better programmer. It governs *collaboration over a shared artifact*.

**Continuous Integration / Continuous Delivery** emerged when integration and release happened often
enough that doing them by hand became the bottleneck and the source of error. The new capability was
*frequent change*; the new failure was the integration-day disaster and the manual deploy that worked on
one machine and nowhere else. CI/CD governs *the path from a change to a running system*. It does not
improve the change.

**Infrastructure-as-Code** emerged when infrastructure became numerous and ephemeral enough that
click-ops in a console could no longer be reproduced, reviewed or rolled back. The new capability was
*programmable infrastructure*; the new failure was drift, the un-reproducible server, the change nobody
could audit. IaC governs *the description and provisioning of infrastructure*. It does not make the
infrastructure faster.

**DevOps** emerged when the wall between "those who write the change" and "those who run it" became the
dominant source of slow, brittle, blame-soaked delivery. It governs *the operational lifecycle of
software as a shared responsibility*. It is a discipline of practice and ownership, not a tool.

**Observability** emerged when systems became distributed and dynamic enough that you could no longer
reason about a failure by reading the code — you had to interrogate the *running system* through signals
it emitted. The new capability was *complex production behavior*; the new failure was the outage you
could not explain. Observability governs *our ability to ask new questions of a running system without
shipping new code to answer them*. It does not change what the system does.

Each of these has a common shape, and it is worth stating it explicitly because it is the argument:

> A new **capability** arrives. It is valuable, so it is adopted quickly. It introduces a **failure mode
> the existing disciplines do not cover**, because it crosses a threshold past which **hand-supervision
> no longer scales**. A new discipline precipitates whose job is to *govern that capability* — and,
> crucially, the discipline is **distinct from the capability it governs.** Git is not programming. CI/CD
> is not the application. IaC is not the server. Observability is not the business logic.

Now apply the pattern. The new capability is **autonomous execution**: an agent that takes initiative,
makes decisions, and acts on the world without a human in the loop for each step. It has arrived, and it
has been adopted fast, because it is obviously valuable — hand a backlog to a competent agent and walk
away. The failure mode it introduces is not covered by any prior discipline. Version control records
*what changed* but says nothing about *whether an unsupervised actor should have changed it*. CI/CD runs
a fixed pipeline but has no concept of an actor that improvises mid-run. Observability tells you, after
the fact, that something went wrong — it does not stop an autonomous agent from force-pushing unattended.
And it crosses exactly the hand-supervision threshold the pattern predicts: **the entire premise of
autonomous execution is that a human is *not* watching.** You cannot supervise by hand a thing whose
defining purpose is to run while you are not there.

So the pattern predicts a new discipline, and the failure modes confirm the gap. We name it **Autonomous
Execution Governance**: the body of practice concerned with *how an autonomous agent should behave while
it executes unsupervised* — what it may decide alone, what it must defer, what must stop it cold, how its
actions remain reversible, and how it reports honestly on a run nobody watched.

We should be careful here, because this is the part most prone to overreach. We are **not** claiming this
discipline is mature, widely adopted, or standardized. We are claiming something narrower and more
defensible: that the *failure mode is real and recurring*, that *no existing discipline covers it*, and
that *the historical pattern by which disciplines precipitate applies cleanly*. Maturity is earned over
years by many practitioners; it cannot be declared. What can be declared, and is the point of this
document, is that the gap exists and is worth a name — and that at least one concrete, working
implementation of the discipline now exists to argue from.

One honest caveat about terminology. We use "Autonomous Execution Governance" throughout. "Operational
Governance" is a plausible alternative or companion label, and which one lands better with a broad
international audience is an open naming question rather than a settled fact. We flag it here rather than
pretend the name is finished; the *substance* — execution governance as a distinct concern — does not
depend on which label wins.

---

## 3. Why existing AI agents cannot own this themselves

The most natural objection to a separate governance layer is: *why not put the governance inside the
agent?* The agent is the smartest component in the system; surely it can also decide its own limits.
This objection is wrong, and understanding *why* it is wrong is the load-bearing insight of the whole
discipline. It is wrong **not because models are weak.** A frontier model is entirely capable of
reasoning about blast radius and reversibility. It is wrong for structural reasons that no amount of
capability removes.

**First: it is a conflict of interest.** The coding agent's job, in the moment, is to make progress on
the task in front of it — to land the next edit, to complete the ticket, to keep moving. An actor
optimizing for progress is precisely the wrong actor to also decide *when progress should stop.* This is
not a slur on the model's character; it is the same reason we separate the developer who writes a change
from the test suite that gates it, the trader from the risk desk, the author from the editor. When the
party that benefits from "proceed" is also the party that decides "proceed vs stop," the decision is
systematically biased toward proceeding. Governance that the governed administers is not governance.

**Second: it violates separation of concerns.** "Write good code for this ticket" and "decide what an
unattended run is *allowed* to do from start to finish" are different responsibilities operating at
different altitudes and over different time horizons. The first is local and momentary: this file, this
function, this test. The second is global and durable: this run, this backlog, the contract that holds
across forty tickets and a dozen context resets. Fusing them into one actor means the global,
slow-changing policy lives inside the same context window that is busy with the local, fast-changing
task — and is therefore subject to being crowded out, rationalized away, or simply forgotten as the
context fills. A governance contract must be *external* to the work it governs precisely so it cannot be
overwritten by the work.

**Third: a request is not an enforcement.** Even a model that perfectly understands the policy can only
*intend* to follow it. Intention is not a control. The history of safe systems is the history of
replacing "the operator will remember to" with "the system makes it structurally impossible to."
Seatbelt interlocks, dead-man's switches, two-key launch, read-only production credentials — none of
these trust the human to do the right thing under pressure; they make the wrong thing unavailable.
Asking a model "please don't force-push while unattended" is the seatbelt-as-suggestion. The governance
layer must be able to *deny the action at the point of execution*, not merely to have asked nicely in
the system prompt. An agent cannot deny its own tool calls structurally for the same reason a process
cannot revoke its own privileges and still use them.

**Fourth: the agent has no durable memory of the contract.** A long autonomous run exhausts and resets
context. The constraint the agent agreed to at the start of the run — "park anything touching the schema" — is, by
ticket 30, competing for attention with thirty tickets' worth of accumulated state, and a fresh session
may not have seen the original instruction at all. Governance that lives only in the agent's context is
governance with the durability of a single context window. It must instead live in **durable state
outside the agent** — a per-ticket record that each fresh subprocess re-reads — so that ticket 40 is
governed by the same contract as ticket 1.

None of these four reasons is about the model being insufficiently smart. Make the model arbitrarily
capable and all four still hold: the conflict of interest is structural, the altitude mismatch is real,
intention is still not enforcement, and a context window is still finite. This is the deepest point in
the manifesto, so we state it as plainly as we can:

> **Autonomous Execution Governance must be a separate discipline from the AI for the same reason it
> must be a separate component from the agent: an actor cannot be both the thing that takes initiative
> and the thing that bounds its own initiative. Separation of concerns is not a convenience here; it is
> the entire mechanism by which the governance is trustworthy.**

This is also why the discipline is not a passing artifact of today's models. Even when agents become far
more reliable, you will still want an external, enforced, reversible, auditable boundary on what an
unsupervised actor may do — exactly as you still want version control even though programmers got better,
and still want CI even though developers got more careful. The discipline outlives any particular
generation of the capability it governs.

---

## 4. The ten principles

Autonomous Execution Governance reduces to a small number of principles. They are stated here as
*norms an autonomous system should satisfy*, each with its rationale and the concrete failure it
prevents. They are deliberately about *behavior under autonomy*, not about model quality. An
implementation that satisfies all ten has, we argue, met the discipline's bar; ANS's instantiation of
each is given in §7.

**1. An autonomous system should never guess an irreversible decision.**
*Rationale.* Reversibility is the entire safety budget of unattended work. A reversible mistake is a
five-minute correction; an irreversible one is a disaster you cannot walk back. The cost asymmetry
is so steep that an irreversible action taken on a *guess* is never justified by the convenience of not
stopping.
*Failure it prevents.* The 3 a.m. force-push, the dropped column, the deleted secret, the destructive
migration run on a wrong assumption — the class of action that nothing afterward can undo.

**2. An autonomous system should never silently fail.**
*Rationale.* A silent failure is worse than a loud one, because it masquerades as success. "The run
finished" must never be confused with "the work got done." Every degraded guarantee, every skipped
check, every unresolvable credential must surface.
*Failure it prevents.* The run that *looks* productive but quietly skipped a failing test, ran without
its review credential, or completed tickets that all need redoing — discovered days later, in production.

**3. An autonomous system should always remain reversible.**
*Rationale.* Reversibility is not a feature you add; it is the precondition that makes proceeding-on-a-
guess survivable at all. If every change is reversible, then a wrong autonomous decision is bounded
damage by construction.
*Failure it prevents.* The unwindable mistake. With reversibility as an invariant, "we guessed wrong"
degrades from catastrophe to chore.

**4. An autonomous system should always remain auditable.**
*Rationale.* A human returning to a run they did not watch must be able to reconstruct *what was
decided, why, and with what confidence* — per decision, not in aggregate. Trust in autonomy is built on
the ability to inspect it after the fact.
*Failure it prevents.* The opaque run: forty completed tickets and no way to know which rested on a
shaky assumption, which were genuinely verified, and which merely went green.

**5. An autonomous system should always explain its uncertainty.**
*Rationale.* When the system defers a decision, the deferral is only useful if it carries the *reason*,
the *candidate interpretations*, and the *exact next action* a human must take. A bare "I stopped" wastes
the human's time re-deriving the question afterward.
*Failure it prevents.* The unhelpful block — a parked ticket with no context, forcing the human to redo
all the analysis the agent already did before it deferred.

**6. An autonomous system should never waste an entire backlog on one unknown.**
*Rationale.* This is the founding failure the discipline exists to fix. One unanswerable question must
not freeze the thirty-nine tickets that did not depend on it. Deferral must be *local* — defer this one
decision, keep moving — not global.
*Failure it prevents.* The classic eight-hour idle: an agent that hit one ambiguity at 22:11 and did
nothing else for the rest of the run, returning a backlog where one stalled ticket cost the other thirty-nine.

**7. An autonomous system should separate execution from verification.**
*Rationale.* Deciding *whether to do the work* and judging *whether the work is correct* are different
jobs with different failure modes and different owners. A governor that also tried to be a correctness
oracle would be a worse governor and a worse oracle. Verification, when needed, is *delegated* to a
component that owns it.
*Failure it prevents.* The conflated system that marks its own work "verified" because it was the one
who did it — the test-author-grades-their-own-test antipattern, at the scale of a whole run.

**8. An autonomous system should separate governance from intelligence.**
*Rationale.* The same structural argument as §3. The thing that takes initiative cannot be the thing
that bounds its own initiative. Governance must be external to, and enforced upon, the intelligent
actor — not requested of it.
*Failure it prevents.* The self-governed agent that rationalizes past its own constraints under the
pressure of "just finish the ticket," because the constraint lived in the same context as the
temptation.

**9. An autonomous system should prefer recovery over perfection.**
*Rationale.* Over a long unattended run, *durability* beats *brilliance*. A run that resumes cleanly
after a crash, a stale heartbeat, or a red gate — and gets thirty-nine of forty done — is worth more than
a run that aimed for forty-of-forty and died irrecoverably at ticket twelve. Build for the failure that
will happen, not the success you hope for.
*Failure it prevents.* The brittle perfectionist run that cannot survive its own interruption — one
crash and the whole run is lost, with no resumable state to pick up from.

**10. An autonomous system should optimize for trust, not speed.**
*Rationale.* The product of an autonomous run is not throughput; it is *a result a human can trust
without re-checking every line.* A slightly slower run whose every claim is honest and every change
reversible is strictly more valuable than a faster run you must audit from scratch — because the second
one didn't actually save you the time.
*Failure it prevents.* The fast-but-untrustworthy run whose speed is illusory, because the human has to
redo the verification the run skipped.

These ten are not independent axioms; they reinforce one another. Reversibility (3) is what makes
proceeding-past-uncertainty (6) survivable. Separation of execution from verification (7) and governance
from intelligence (8) are what make the whole thing trustworthy (10) rather than self-graded. Auditability
(4) and explained uncertainty (5) are what make a non-silent failure (2) actionable rather than merely
loud. Together they describe a system that an engineer can leave alone *and still trust* — which is the
entire point.

---

## 5. The design principles

The ten principles of §4 say *what* an autonomous system should do. This section names the *engineering
properties* a governance layer must embody to deliver them — the structural commitments. Each is a
familiar idea from sound systems design, here applied to the specific problem of unsupervised execution,
and each is tied to the mechanism that realizes it.

**Single Responsibility.** The governance layer governs execution and *nothing else*. It does not write
code, judge correctness, choose models, or reason about the domain. A component that does one thing can
be reasoned about, tested, trusted and replaced; a component that does five is none of those. The
discipline's first commitment is to *stay small* — to own execution governance and to delegate every
adjacent responsibility to the component that owns it. (*Mechanism:* the scope boundary — §7.)

**Separation of Concerns.** The corollary at the system level. The worker writes; the governor governs;
the verifier verifies. Each is independently reasoned about, swapped and trusted, and the seams between
them are explicit contracts rather than fused responsibilities. This is the property that makes §4's
principles 7 and 8 structural rather than aspirational. (*Mechanism:* the agent-as-worker / harness-as-
governor / council-as-delegated-verifier decomposition.)

**Determinism.** The governance decisions that *can* be deterministic *must* be. A gate either passes
or it does not; a launcher either grants GO or it does not; an outcome is recorded exactly once. Where a
judgment is irreducibly probabilistic (the model's reasoning), it is pushed *out* of the governance core
and treated as advisory. Determinism is what makes the layer auditable and reproducible — you can run it
again and get the same governance decision. (*Mechanism:* the deterministic gate and the pre-token
GO/NO-GO launcher.)

**Reversibility.** Every action the layer *permits* must be undoable; every action that is *not* undoable
must be blocked outright rather than governed. This is the binary that makes autonomy survivable: the
reversible set is the set the system may proceed on; the irreversible set is the set it must refuse.
(*Mechanism:* git-backed snapshot/revert for permitted changes; deny-hooks for irreversible operations.)

**Least Privilege.** An autonomous run gets exactly the authority it needs and no more, and any
escalation of that authority is an explicit, human-confirmed decision rather than a default. The flags
that let an agent run unattended *grant real power*; treating them as defaults is how unattended runs
become dangerous. (*Mechanism:* autonomy flags are never defaults — they are human-confirmed presets,
each showing what it grants before it can be marked launchable.)

**Fail-Safe.** When a capability is missing or a check cannot be completed, the system degrades toward
*more* conservatism, never toward silent permissiveness. A missing reversibility net does not mean
"proceed anyway"; it means "establish a net or stay non-destructive." A failed credential resolution is
a blocking refusal, not a silent empty value. The default direction of failure is *safe*. (*Mechanism:*
preflight degradation that lowers expected yield and raises conservatism; fail-closed credential
resolution.)

**Auditability.** Every decision leaves a durable, inspectable record: what was decided, why, with what
confidence, and what a human must do about it. What happened is reconstructable from state, not from
memory. (*Mechanism:* the per-ticket durable outcome store and the ranked run report.)

**Recovery.** The system is built to resume, not to run perfectly once. State is persisted atomically so
that a crash, a kill, or a stale heartbeat resumes cleanly rather than restarting or corrupting. Recovery
is treated as the common case, because over a long enough run it is. (*Mechanism:* atomic, resume-safe
state; a watchdog that restarts a stalled run resumably; attempt/loop caps that prevent a cursed item
from consuming the run.)

**Statefulness.** Governance lives in durable state *outside* the intelligent actor, precisely so it
cannot be crowded out of a context window. The contract that holds at ticket 1 holds at ticket 40 because
it is re-read from disk by each fresh subprocess, not remembered by an agent whose context has long since
turned over. (*Mechanism:* the per-ticket state machine; fresh-session-every-N for long backlogs.)

**Governance.** The meta-principle that binds the others: there *is* a layer whose explicit job is to
hold the policy, enforce it structurally, and own the trust-or-flag decision — distinct from, and
external to, the actor it governs. Without this principle the other nine are merely good intentions
inside the agent; with it they are a control. (*Mechanism:* the harness as a separate process holding the
ASK/PARK/HALT contract, enforced at the tool layer by hooks.)

The throughline is that none of these is exotic. They are the ordinary virtues of sound systems — single
responsibility, determinism, least privilege, fail-safe, auditability — recognized, perhaps surprisingly,
as the *right* virtues for the unfamiliar problem of governing a non-deterministic actor that works while
you sleep. The discipline's contribution is less the invention of new principles than the recognition of
*which* established principles govern autonomy, and the insistence that they be enforced structurally
rather than requested politely.

---

## 6. The Git analogy, developed rigorously

We claim: *ANS is to autonomous AI what Git became to source code.* This is a thought, not a literal
equivalence, and it is worth developing carefully — both because it illuminates the discipline and
because a sloppy version of it would be exactly the kind of hype this document refuses.

Start with what Git actually did. Before distributed version control, the bottleneck on software was not
*writing* code — programmers could already write code. The bottleneck was **collaboration over a shared,
mutable artifact**: multiple people changing the same files, needing to merge, needing history, needing to
answer "who changed this and why," needing to undo. Git did not make anyone a better programmer. It did
not improve the code. It solved a *different* problem that sat *above* programming — the problem of many
hands on one artifact over time — and by solving it cleanly it unlocked a scale of collaboration that was
previously impractical. The crucial point: **Git solved collaboration, not programming.** Those are
distinct problems, and conflating them would have produced a worse tool for both.

Now the parallel. Today the bottleneck on autonomous AI is not *intelligence* — the models can already
reason, write code, and act. The bottleneck is **safe, trustworthy execution over a long unsupervised
run**: the agent needs to know what it may decide alone, what it must defer, what must stop it; its
actions need to be reversible; its run needs to be auditable. ANS does not make the agent smarter. It
does not improve the model. It solves a *different* problem that sits *above* the AI — the problem of
governing an autonomous actor's behavior over time — and the claim is that solving it cleanly unlocks a
scale of *unsupervised* work that is otherwise impractical. **ANS solves autonomy, not AI.** Those are
distinct problems, and conflating them produces a worse tool for both.

The structural correspondence is precise on several axes, and it is worth laying out so the analogy can
be *checked* rather than merely felt:

| Axis | Git (governs collaboration) | Autonomous Execution Governance (governs autonomy) |
|---|---|---|
| What it governs | Concurrent change to a shared artifact | An autonomous actor's behavior during unsupervised execution |
| What it does **not** do | Write the code; make you a better programmer | Write the code; make the model smarter |
| The capability it presupposes | People can write code | Models can reason and act |
| The failure it removes | Lost work, unmergeable change, un-undoable mistake, no history | Frozen backlog, irreversible action, silent failure, unauditable run |
| Its core invariant | History is durable; any committed state is recoverable | Every permitted change is reversible; every decision is recorded |
| Why it lives *outside* the worker | The artifact is shared; one author can't own the merge | Governance must be external to the governed (conflict of interest, §3) |

The analogy also tells us, by extension, what *not* to claim. Git did not eliminate bugs, did not
guarantee good code, and did not make merging *automatic* in every case — it made collaboration
*tractable and recoverable*, which is a humbler and more durable claim than "it solved software." By the
same token, Autonomous Execution Governance does not guarantee correct code, does not eliminate the
possibility of a wrong autonomous decision, and does not make autonomy *risk-free*. It makes autonomy
*tractable and recoverable*: bounded by an enforced contract, reversible by construction, auditable after
the fact. That is the honest version of the analogy, and it is the only version we make.

There is a final, sharper point in the comparison. Git became *infrastructure* — invisible, assumed,
the thing you would not start a serious project without — precisely because it solved a problem that does
not go away as programmers improve. Collaboration over a shared artifact is permanent; better programmers
still need merges and history. The bet of this manifesto is that *governing an autonomous actor* is
similarly permanent: better models still need an external, enforced, reversible, auditable boundary on
what they may do unsupervised. If that bet is right, Autonomous Execution Governance becomes
infrastructure for the same structural reason Git did — not because of any particular implementation, but
because the problem it governs is enduring and distinct from the capability that created it.

We are careful to keep the analogy at the level of *structure*. We are not claiming ANS has Git's
adoption, ubiquity, or track record — it manifestly does not; it is one v1.0.0 implementation of a young
discipline. The claim is narrower and, we think, sound: the *shape* of the problem ANS addresses
(governing-the-new-capability, distinct-from-the-capability, recoverable-by-invariant, external-to-the-
worker) is the same shape Git addressed for collaboration. That structural likeness is the thought worth
carrying forward.

---

## 7. ANS as the first concrete implementation

A discipline argued in the abstract is a hypothesis. A discipline with at least one working, falsifiable
implementation is a position you can build on. This section shows how *Agents Never Sleep* instantiates
each principle from §4–§5 in concrete mechanism. The point is not to sell ANS — it is to demonstrate that
the discipline is *implementable*, that its principles translate into structure rather than slogans, and
to be specific enough that the implementation can be inspected and disagreed with.

Everything below describes ANS v1.0.0 as built. Where a guarantee holds only under stated conditions, we
say so; the honest limitations are collected in §9 rather than scattered or omitted.

### The ASK / PARK / HALT contract — principles 1, 5, 6, 8 made structural

ANS gives an unattended run a contract with three distinct, never-collapsed responses to uncertainty.
While unattended, the agent only ever chooses **PROCEED**, **PARK**, or **HALT** — it never **ASK**s,
because nobody is there to answer, and ASK is structurally converted to PARK.

- **PROCEED** — assume, log, and continue, for a low-blast-radius, reversible, isolated choice (naming,
  internal structure, log wording, an equivalent local implementation). The assumption is *committed* so
  it can be reverted. This is principle 6 (keep moving) bounded by principle 3 (reversibly).
- **PARK** — defer *this one* ticket or decision and move to the next independent one. It is not a stop;
  it records the reason, the candidate interpretations, the exact human next-action, and the
  contamination scope. This is principle 5 (explain the uncertainty) and principle 6 (don't waste the
  backlog) together: deferral is *local and informative*, never global and bare.
- **HALT** — stop the *whole run*, reserved for genuinely irreversible danger with no reversibility
  safety net. This is principle 1 (never guess an irreversible decision) in its strongest form: when
  even reversibility cannot be established, the only safe move is to stop.

The discriminator between PROCEED and PARK is **blast radius**, made concrete rather than left to
vibe. An enumerated set of *hard-PARK categories* — database schema and migration direction, public or
shared API contracts, security / auth / tenant-isolation boundaries, money / billing / pricing,
cross-ticket interfaces, and ambiguous requirement *meaning* — is parked unless a change is both locally
reversible and isolated (in which case it is built reversibly behind a flag *and* the decision is
parked: a hybrid). **PARK is the safe default:** anything that does not clearly clear the PROCEED bar is
parked, so the contract covers the entire decision space with no silent gap. This enumeration is
deliberate engineering: it is what keeps the agent from landing in "unsure" too often, and it is honestly
the system's weakest link — blast-radius classification is a judgment, assisted by a heuristic
auto-classifier — which is *exactly why* every PROCEED change is made reversible. The principle and the
limitation are the same fact seen from two sides.

That ASK-becomes-PARK is not a prose suggestion. It is a coded state distinction (`decide.py` defines
`Action.{PROCEED,PARK,HALT,ASK}` and forbids emitting `ASK` unattended), enforced structurally so that a
careless reading of "never stop" plus "park ticket" cannot invert the spine into "stop the run."
Separation of governance from intelligence (principle 8) is realized by the contract living in a
*separate process and durable state*, re-read by each fresh subprocess, rather than in the agent's
context.

### The deterministic gate — principle 7, and the determinism design principle

The only thing that can *hard-block* a ticket in ANS is the **deterministic gate**: a shell command —
your test suite — run after every edit. Exit 0 is green; non-zero is red. A red is classified, by
snapshot comparison, as *introduced-by-the-diff* (revert to last green, park or fail) versus
*pre-existing / flaky / environmental* (keep the work, downgrade confidence, record a blind spot). The
gate runs with a per-step timeout in a non-interactive environment, so it can never hang on a TTY prompt;
a timeout is recorded as a `BLOCKED_ENV` outcome, never a run halt.

This is principle 7 — *separate execution from verification* — instantiated precisely. The gate is *your*
regression check; ANS does not replace it, second-guess it, or claim it proves correctness. ANS's role is
governance *around* the gate: classify the failure, revert on the diff-introduced case, record the
outcome atomically. **ANS never deletes or skips a failing test to go green** — doing so is treated as a
blocking blind spot, because a silent pass (principle 2) is the cardinal sin. The gate's determinism is
what makes the governance auditable: re-run it and you get the same verdict.

### Reversibility — principle 3 and the reversibility design principle

Each PROCEED ticket is snapshotted before edits; a red gate reverts to the last green commit. If the
snapshot commit cannot be made at all (git lock, timeout, read-only object store), the ticket is recorded
`BLOCKED_ENV` rather than edited un-revertibly — fail-safe (the design principle) in action. Every
PROCEED assumption is committed so it can be cheaply reverted. The genuinely irreversible operations
— force-push, remote-branch delete, destructive SQL, secret deletion, disk wipe — are not *governed* on a
guess; they are **denied at the tool layer by hooks**, which is the reversibility binary of §5: permit
only the undoable, refuse the rest. This is why a wrong PROCEED during an unattended run is a five-minute
revert rather than a disaster — by default the irreversible class has no path to execution. The one
sanctioned exception is explicit and human-authored, not agent-granted: at interactive setup a human may
pre-authorize a specific class for a run (the *consent manifest*), recorded out-of-repo so the unattended
agent cannot widen its own floor — the same separation-of-concerns argument as principle 8, applied to the
floor itself. And the denial is a backstop against an honest mistake, not a boundary against evasion: it
matches command shapes, not script contents, so a non-shell wrapper can still slip past it.

### The pre-token launcher — least privilege, determinism, fail-safe

Capabilities can only be measured *after* the agent session boots — by then the first tokens are spent.
So `ans-run` is a deterministic GO/NO-GO gate that runs *before* the agent CLI boots, embodying several
design principles at once. It enforces **trust-on-first-use** on the repo config that describes the
commands it will execute (a changed config must be re-trusted, keyed on its SHA-256). It enforces an
**identity / root-guard**. It selects an agent only from **named, human-confirmed presets** — no
launch-time platform detection, because environment markers are spoofable and absent under cron — and
treats **autonomy flags as an explicit human decision, never a default**, showing what each flag grants
before a preset can be marked launchable (least privilege). It takes an **atomic working-tree lock**
(`flock(2)`), so two simultaneous starts yield exactly one winner with no pidfile race. And it resolves
**token-refs, never literal keys**, through a key source, registering each resolved value for redaction;
a failed resolution is a blocking NO-GO, never a silent empty value (fail-safe). Every one of these is the
discipline's principles expressed as a deterministic pre-condition rather than a runtime hope.

### Resilience and recovery — principle 9 and the recovery/statefulness design principles

The per-ticket state machine records exactly one durable outcome per ticket with atomic, resume-safe
writes, so a crash or kill resumes cleanly rather than corrupting or restarting. An **attempt/loop
ledger** caps cross-resume retries and detects provable loops, force-parking a cursed item before it
burns the run; a low-yield circuit breaker halts and alerts when most outcomes are parks or blocks, so
"the run finished" is never silently confused with "the work got done" (principle 2). A **watchdog**
sidecar restarts a stalled run resumably when its heartbeat goes stale — the hang an in-process stop-hook
cannot see — and exits with a distinct code on exhausted restarts so the failure is loud. For long
backlogs, **fresh-session-every-N** hands off to a new agent session that re-reads the durable state, so
ticket 40 is governed by the same contract — and gets the same quality — as ticket 1, because the
*governance* state never degrades even as an agent's context would.

### Auditability — principle 4 and the auditability design principle

Exactly one of seven durable outcome states is recorded per ticket — `DONE`, `DONE_LOW_CONFIDENCE`,
`PARKED_DECISION`, `PARKED_FOUNDATIONAL`, `BLOCKED_ENV`, `FAILED_RETRYABLE`, `FAILED_BUG_IN_AGENT` — and
the run ends in a single **ranked run report**: what is done and trusted, what needs daylight
review, what is parked (with candidate interpretations and the exact next action), what is blocked, and
every **blind spot** (a degraded guarantee, a missing review credential, an unresolved secret, a host
that could not natively enforce a guarantee). A blind spot is surfaced loudly, never swallowed. This is
the after-the-fact reconstructability that principle 4 demands.

### Verification is *delegated*, not owned — principle 7 at its sharpest

This is the cleanest demonstration that ANS holds to separation of concerns, and the place the discipline
is most easily misunderstood. **ANS does not verify code or reason about correctness.** For a genuinely
high-risk diff it can *optionally delegate* a second opinion to an external verification/consensus layer
— the Tokonomix Council MCP, a *separate* building block — and it uses that verdict for exactly one
purpose: deciding whether to mark a ticket `DONE` (trusted) or `DONE_LOW_CONFIDENCE` + *needs daylight
review*. What ANS owns there is purely deterministic governance, not judgment: it *routes* the risk tier
from the actual diff, applies a *budget gate* before any delegation, and *disposes* the returned verdict
(convert "concerns / errored / never ran" on a heavy-risk diff into a flagged outcome rather than a
silent `DONE`). The multi-model reasoning itself happens *outside* ANS — the harness is standard-library
Python and cannot call a model. The delegated review is **advisory: it never blocks the run and never
reverts; it can only withhold the trusted stamp.** Model agreement is not correctness — frontier models
share training data and can be uniformly wrong — so this is a recall amplifier and a flag, never a truth
oracle.

We dwell on this because it is the discipline's thesis made physical. A lesser design would have folded
"check the code with several models" into ANS as a headline feature. Doing so would have made ANS a
worse governor (now conflicted between governing and judging) and a worse verifier (now constrained by a
governor's altitude). Instead: *execution governance and verification are separate disciplines, owned by
separate components, with an explicit delegated seam between them.* The governor governs; the verifier
verifies; ANS consumes only the verdict. That is principle 7 not as an aspiration but as an architectural
fact — and it is the part of the design we are most willing to be judged on.

### The EXECUTION-only boundary as the unifying commitment

Every mechanism above sits inside one boundary: **ANS owns execution, and only execution.** It is
responsible for execution governance, scheduling, autonomy, resilience, recoverability, reversibility,
workflow continuity, deterministic execution, and operational safety. It is *explicitly not* responsible
for code generation, model quality, AI reasoning, consensus, or verification — those belong to other
components and are *delegated*, never absorbed. This is Single Responsibility and Separation of Concerns
(§5) as the organizing decision of the whole system, and it is what makes every other guarantee coherent:
a layer that does one thing can keep its promises about that one thing.

---

## 8. The road ahead and the open problems

A young discipline is more honestly described by its open problems than by its claims. We name the ones we
think are real, including the ones that bear on ANS itself.

**Cross-platform enforcement is unevenly verified.** ANS's structural guarantees — never-ASK,
deny-irreversible, never-stop — are enforced through each coding platform's native hook system. As of
v1.0.0, **only one platform (Claude Code) is live-verified** firing on the real tool; the others are
*built to each platform's documented hook contract* and exercised by a hermetic test suite, but not yet
confirmed on the real CLI. This is a transparency obligation, not a hidden caveat: the gap between
"built-to-contract" and "live-verified" is stated wherever it matters, and promoting a platform is a
concrete, repeatable smoke-test. The open problem is general, not specific to ANS: *a governance layer is
only as strong as the host's ability to enforce its hooks*, and hosts differ. Where a host exposes no
native hook for a guarantee, the honest move is to fall back to the written contract and surface the
residual gap as a loud blind spot — which is what ANS does — but a prose contract is weaker than a tool-
layer denial, and saying so is part of the discipline.

**Blast-radius classification is the soft center.** The PROCEED-vs-PARK decision rests on judging how far
a wrong choice can spread, and that judgment is the system's weakest link. ANS mitigates it three ways —
an enumerated hard-PARK list, PARK-as-the-safe-default, and universal reversibility of PROCEED changes —
but mitigation is not elimination. A genuinely better classifier (learned, calibrated, perhaps itself
delegated) is an open research direction for the discipline as a whole.

**Measuring autonomy is unsolved.** The discipline needs a way to *quantify* how well a system governs an
unattended run — something like an *autonomy index* over continuous runtime, tickets completed, human
interruptions (target zero), recovery-after-failure, and unfinished (parked + blocked) tickets. ANS ships
a *reproducible methodology* and a hermetic harness for this — **not measured results.** We are deliberate
about the distinction: a controlled "normal-agent vs governed-run on the same backlog" comparison is
scoped as a first experiment, to be reported, when run, with its exact setup and a date — never asserted
in advance as a bare number. Until then the metrics are a way to *measure*, not a result we are claiming.
The open problem is methodological maturity: which metrics actually capture *trustworthy* autonomy rather
than mere throughput, and how to compare systems fairly.

**Terminology and category boundaries are unsettled.** Whether the discipline is best called "Autonomous
Execution Governance" or "Operational Governance" (or both, primary and secondary) is an open naming
question with real consequences for how the category is found and understood. More substantively, the
*boundaries* between execution governance, verification, decision-making, measurement and routing — which
ANS draws sharply by delegating everything outside execution — are a proposal, not a settled taxonomy. A
healthy discipline will argue about where those seams belong.

**The ecosystem is a bet, not a fact.** ANS is designed as one of a planned family of single-
responsibility components — execution (ANS), decision-making (Council), verification, measurement,
provider-routing, long-term memory — each standalone-usable, each delegating outside its lane. That
clean decomposition is a *hypothesis about how autonomous-agent infrastructure should be built*, and the
honest status is: the seam between execution governance and delegated verification is implemented and
working; the rest of the family is direction, not delivered fact. We present it as the bet it is.

The largest open problem is the one the discipline exists to keep honest: **trust does not come from a
claim that a system is safe; it comes from the structural ability to inspect, bound and reverse what an
unsupervised system did.** The road ahead for Autonomous Execution Governance is the steady, unglamorous
work of making those structural guarantees stronger, more verifiable, and more measurable — and of
resisting, at every step, the temptation to convert an aspiration into an asserted result.

---

## 9. Limitations and intellectual honesty

This document would betray its own thesis if it overstated its case. Stated plainly:

- **Autonomous Execution Governance is a young discipline, not a mature one.** We argue the gap is real
  and the pattern of emergence applies; we do **not** claim adoption, ubiquity, or standardization. A
  discipline is matured by many practitioners over years, not declared by one document.
- **ANS is one v1.0.0 implementation, not the category.** It is evidence that the discipline is
  implementable, and a concrete thing to argue from and against. It is not "the" implementation and not
  the standard.
- **The governance layer is not a correctness oracle.** ANS governs *whether an unsupervised agent
  should have touched a surface* and keeps changes reversible; it does not make code correct. The
  deterministic gate (your tests) is the only hard correctness check, and a delegated council second
  opinion is advisory, never a guarantee. Model agreement is not truth.
- **A wrong PROCEED assumption is possible.** Blast-radius tiering lowers the odds; it does not zero
  them. The defense is reversibility, not infallibility — which is why we built the system so a wrong
  call is a cheap revert, not a catastrophe.
- **Enforcement strength varies by host.** Only Claude Code is live-verified today. Elsewhere a guarantee
  may rest on a prose contract plus a loudly-reported blind spot, which is weaker than a tool-layer
  denial.
- **No benchmark *results* are claimed anywhere in this document.** The autonomy metrics are a
  methodology. If and when they are run, they will be reported dated, reproducible, and with their setup
  — never inline as an unearned number.

We make exactly two strong claims, and we believe both are defensible. First, that *a distinct
governance discipline is emerging for autonomous execution, separate from the AI it governs* — defended
from the historical pattern and the structural separation-of-concerns argument, not from market data.
Second, that *ANS is a concrete, pioneering implementation that defines the category by instantiating it*
— defended by the implementation in §7, which exists and can be inspected. "Pioneers" and "defines the
category" are statements about being early and about giving the category a worked-out shape — by
*proposing* the principles and being their first implementation, with no external spec, no second
implementation, and no outside adoption yet. They are *not* the claim "the industry standard," which we
do not make.

---

## 10. Closing

We acquired, almost overnight, a worker that takes initiative and acts while we are not watching. The
field's reflex has been to treat its unreliability as a problem to be solved by a better model. We have
argued that the deepest failure of unattended work is not a capability gap at all — it is a *governance*
gap: an autonomous actor's only honest response to uncertainty is *stop* or *guess*, and neither is right
when nobody is there. Closing that gap requires a layer that is distinct from the agent, external to it,
structurally enforced, reversible by construction, and honest after the fact. That is a new engineering
discipline, and it follows the same pattern by which Version Control, CI/CD, Infrastructure-as-Code,
DevOps and Observability each precipitated: a new capability arrives, it outruns hand-supervision, and a
distinct discipline forms to govern it.

Git solved collaboration, not programming. ANS solves autonomy, not AI. That distinction is the whole
idea. *Agents Never Sleep* is offered not as a finished standard but as the first worked-out instance of
Autonomous Execution Governance — a place to stand, argue from, and improve upon. The discipline is
larger than any one implementation, and the work ahead is to make its guarantees stronger, more
verifiable, and more measurable, while never once trading honesty for a louder claim.

That last constraint is not a footnote. In a discipline whose entire purpose is to make autonomous work
*trustworthy*, the documents about it must be trustworthy too. We would rather under-claim and be
believed than over-claim and be right by accident. Trust, not speed, is the point — for the systems, and
for the words about them.

---

*Agents Never Sleep is MIT-licensed and developed in the open at
[TokonoMix/agents-never-sleep](https://github.com/TokonoMix/agents-never-sleep). The product
documentation (README, ARCHITECTURE, SKILL) describes the implementation in full; this manifesto
describes the discipline that implementation serves. Where the two differ in emphasis, the README is the
authority on mechanics and this document is the authority on intent.*
