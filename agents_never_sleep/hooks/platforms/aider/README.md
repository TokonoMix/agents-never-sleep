# Aider enforcement adapter (wrapper / launch-preset)

Aider (0.86.2) has **no hook / plugin / event API of any kind** — there is no pre-execution
seam to deny on. So Aider is ANS's first **wrapper-shaped** adapter: a hardened launch preset
+ git-reversibility + the prose contract, **not** a hook dispatcher. It does **not** go through
`enforce.py`. Aider is also the first platform where **deny-irreversible is not native** — it
breaks the old "deny-irreversible works on every platform" invariant.

## Capability matrix row — all three soft-enforced

| Guarantee | Status | How (and the residual) |
|---|---|---|
| deny-irreversible | 🟡 soft | `--no-suggest-shell-commands` + never wiring `--test-cmd`/`--auto-test`/`--lint-cmd` closes the LLM-suggested shell and the operator-cmd shell paths. **Residual hole:** `cmd_test`/`cmd_run` (`commands.py`) run shell with no confirm and no hook — un-interceptable. Recovery = git-revert to the captured pre-SHA. Reported as a BLIND SPOT. |
| never-stop | 🟡 soft (needs a wall-clock timeout) | aider is one-shot in `--message` mode (`run()` returns after one turn); continuation is the OUTER ANS loop. **Live smoke-test (2026-06-28): preset flags do NOT prevent all hangs** — see below. never-stop is enforced by the driver's hard wall-clock timeout (kill → PARK), not by flags. |
| never-ASK | 🟡 soft | `--yes-always` auto-answers stdin prompts; `stdin < /dev/null` EOFs any unanticipated stdin prompt. Closes *stdin* hangs — but NOT the network/onboarding hangs (below). |

deny-irreversible is the genuine 🟡 with a residual hole; never-stop/never-ASK close the
*stdin* paths but the smoke-test below shows network/onboarding paths still need the timeout.

## ⚠️ Live smoke-test finding (2026-06-28) — aider has hang paths no flag closes

Running the hardened preset with `stdin < /dev/null` still **hung** in two real cases:
1. **No key configured** → aider opens an OpenRouter **OAuth browser flow** ("Waiting up to 5
   minutes for you to finish in the browser…") — a *network* wait `stdin=/dev/null` does not defuse.
2. **Invalid/slow key** → the LLM call stalls (and the model-warnings prompt stalled until
   `--no-show-model-warnings` was added — now in the preset).

**Therefore the ANS driver MUST, around the aider subprocess:**
- run it under a **hard wall-clock timeout** (`agents_never_sleep.aider_launcher.RECOMMENDED_TIMEOUT_SECONDS`,
  default 600s) and **kill → PARK** on expiry — this is how never-stop is actually enforced;
- **pre-flight** that a model + key are configured (else the keyless OAuth onboarding hangs);
  aider needs its OWN LLM key (e.g. `OPENROUTER_API_KEY`), outside ANS billing.

## The launch preset

ANS drives aider as a subprocess, **once per ticket**. The canonical argv is built by
`agents_never_sleep.aider_launcher.build_aider_argv()`:

```bash
# what the builder produces (run with stdin redirected from /dev/null):
aider --message-file .unattended/ticket-<id>.prompt \
      --yes-always --no-detect-urls --no-suggest-shell-commands \
      --no-auto-test --no-auto-lint <files...> < /dev/null
```

Per ticket the ANS driver:
1. records the **pre-invocation HEAD SHA**;
2. runs the preset above;
3. runs **its own** deterministic gate (tsc / tests) — never aider's;
4. on gate failure: `git reset --hard <pre-SHA>` (revert the whole ticket — note one
   `--message` can emit multiple commits, so revert to the captured SHA, never `HEAD~1`).

Keep aider's **auto-commit ON** (the default) — it is the reversibility anchor.

## Blast warnings (carry these)

- **NEVER** wire `--test-cmd` / `--auto-test` / `--lint-cmd` (or let the prompt use `/run`,
  `/test`): they run arbitrary shell with no confirm and no hook. `build_aider_argv` refuses
  these flags via `extra`, but an operator config could still set them — a hard prohibition.
- Aider needs its **own LLM key**; that spend is **outside** ANS's billing/guardrails.
- Run under a **sandbox** for egress control (startup version-check / URL-scrape are not
  ANS-interceptable; the preset passes `--no-detect-urls`).
- Aider passes git `--no-verify` by default (`--git-commit-verify` default false), so a repo
  pre-commit hook would **not** fire on aider's commits — a `decide()` pre-commit bridge is a
  *future* option, not the v1.1 adapter.

## Verification status

Built to the documented behavioral contract + hermetically tested
(`acceptance/test_aider_launcher.py`). The **headless launch behavior was live smoke-tested**
(2026-06-28) on the real `aider 0.86.2` — that test FOUND the onboarding/network hang paths
above and drove the `--no-show-model-warnings` + mandatory-timeout hardening. **Not** in
`LIVE_VERIFIED`: a full keyed end-to-end ticket (real edit → auto-commit → revert to pre-SHA)
needs aider's own LLM key + spend and is maintainer-side.
