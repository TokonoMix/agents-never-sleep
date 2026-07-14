# ANS Roadmap

> **30-second version.** Direction, not promises. ANS v1.0.0 is the baseline: the reliability spine **and**
> the quality machinery are built. What's ahead is mostly *promotion and reach* — publish to PyPI,
> live-verify more host platforms (the adapters are already built to contract), run the autonomy benchmark
> for real, remove the back-compat shim in 2.0, and integrate more deeply with the rest of the Tokonomix
> ecosystem. No dates we can't keep. The checkable stability policy is in `SEMVER.md`; the per-version
> record in `CHANGELOG.md`. See [benchmarks](benchmarks.md), [security](security.md), [glossary](glossary.md).

## The baseline (what is already built — v1.0.0)

So the roadmap is read against reality, not aspiration: as of v1.0.0 the spine (durable per-ticket state,
PARK-vs-continue semantics, anti-starvation) **and** the quality machinery (agent-as-worker bridge,
delegated council / specialist scaffolding, never-ASK enforcement, secret redaction, watchdog, Vault
keysource, Paperclip integration, the fresh-session loop) are all built — not "Phase 2". The going-forward
import name is `agents_never_sleep`; `import harness` still works via a back-compat shim. Verify any status
claim against the `agents_never_sleep/` source.

## Direction

### 1. PyPI publish — shipped (v1.4.0, 2026-07-12)
The bare `pip install agents-never-sleep` is live on PyPI; a tag push (`v*`) auto-publishes via GitHub
Actions Trusted Publishing (OIDC, no stored token). The pinned/dev fallback
(`pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.4.0` or `@main`) remains available for
anyone who wants a specific tag or an unreleased checkout.

### 2. More live-verified platforms
Today **only Claude Code is live-verified**. The other hosts are **built to their documented hook
contract** and hermetically tested, but not yet confirmed firing on the real tool:

- Gemini, Codex, Copilot, Cursor, Windsurf — promoting each to *live-verified* is a ~5-minute smoke-test on
  the real tool (`hooks/platforms/README.md`).
- The CHANGELOG `[Unreleased]` section targets additional enforcement platforms (additive / MINOR): an
  in-process **Hermes** plugin, a **wrapper-style Aider** preset, and a **Crush** dispatcher hook. These
  are built and opt-in but **not live-verified** — a host smoke-test promotes them.
- Aider wrapper hardening (notably the network/OAuth hang) is on the same track.

Honest framing per `SEMVER.md` decision D5: the SemVer stability guarantee covers the **ANS API** (loop /
launcher CLIs, config + ticket schema, outcome states, entry points), **not** the behaviour of a
third-party host's hook contract — which can change outside ANS's control. Per-platform adapter behaviour
is best-effort, validated against the hook-contract version recorded in `capabilities.py`.

### 3. Run the benchmark methodology for real
The autonomy [methodology](benchmarks.md) is built; *running* it is a separate, dated effort. The first
reproducible experiment ("normal-agent vs ANS on a controlled backlog", Paperclip `18eee818`) will be
folded into a dated, reproducible appendix **when run** — never as an inline number before.

### 4. Deprecation cleanup — shim removal in 2.0
The `harness` back-compat shim (which keeps `import harness` / `python -m harness.run` working with a
deprecation warning through all of 1.x) is **removed in 2.0** per `SEMVER.md`. `agents_never_sleep` is the
going-forward name. A breaking change to any Stable surface requires a MAJOR bump announced in the
CHANGELOG with a migration note — so the shim removal will be exactly that.

### 5. Deeper ecosystem integration
ANS is one of many planned Tokonomix building blocks. The direction is tighter, cleaner delegation across
the ecosystem — execution (ANS) → decision-making/verification (Council MCP) → measurement (Benchmark) →
provider-selection (Routing) → long-term context (Memory) — each standalone-usable but stating its place.
The delegated-council integration is the first such seam; more adjacent responsibilities get the same
clean-boundary treatment over time. See the [glossary](glossary.md) ecosystem table.

### 6. Consent target-scoping (v2)
The run-setup consent manifest (v1.5.0) is deliberately coarse: a pre-authorized class applies to the
**whole run** across every reachable target. Scoping consent to specific hosts/URLs/recipients (so
`send_email` could be pre-authorized only for one domain) is the planned v2 refinement — not built today.

### 7. Per-task settings
Consent, consensus-eligibility and gate strictness are today set **once per run** — the whole backlog
inherits one policy. A planned refinement lets a single ticket carry its own overrides: pre-authorize one
deny-list class for just this ticket, mark just this ticket eligible for a consensus-assisted resolution
attempt, or tighten (never loosen below the floor) its gate. The unit of governance becomes the ticket, not
only the run. This composes with §6: per-task scoping is the finer grain of the same consent model, not a
separate mechanism — and, like everything above, it never lets an unattended agent widen its own authority.

## What is deliberately NOT on the roadmap

To keep the scope boundary honest: ANS will **not** grow into a code generator, a model, a correctness
oracle, or a verification/consensus engine. Those are different responsibilities owned by different
building blocks. Anything that smells like "ANS should also judge the code" is, by design, a *delegation*
to the verification layer — not a future ANS feature.

## Limitations of this roadmap

This is direction, not a commitment to dates or to shipping any specific item. Items can be re-ordered or
dropped. The only binding promises ANS makes are the SemVer stability guarantee (`SEMVER.md`) and the
honesty bar — no roadmap item overrides either.

---

*Verified against `agents_never_sleep/` (v1.0.0): README §15, `SEMVER.md` (binding-from-1.0.0, decision
D5, shim removal in 2.0), `CHANGELOG.md` (`[Unreleased]` Hermes/Aider/Crush platforms, not live-verified),
`capabilities.py` (`_HOOK_CONTRACT`), `hooks/platforms/`.*
