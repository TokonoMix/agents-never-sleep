# ANS Security

> **30-second version.** An unsupervised agent that can edit files and run shells is a real security
> surface. ANS bounds it with four design guarantees: **never ASK** unattended (an ask-tool is denied),
> **never do something irreversible** unsupervised (force-push, remote branch/tag delete, release-tag push,
> destructive SQL, `mkfs`/`dd of=/dev/…`/`shred`, secret Vault writes, Redis mass-flush, `docker volume rm`,
> email send, publishing, infra teardown, stateful `kubectl delete`, `systemctl mask`, and more are denied by
> hooks — a representative list, not exhaustive), **never leak a secret** into a log or report
> ([secret redaction](secrets.md)), and **least privilege** (resolve credentials from a server-managed
> source, never run as root). A run-setup consent manifest additionally lets a human pre-authorize specific
> irreversible-op classes for a given run. These are guarantees to *test against*, not aspirations. See
> [launcher](launcher.md), [secrets](secrets.md), [glossary](glossary.md).

## The threat model: an agent that runs for hours with no one watching

The thing ANS governs is dangerous precisely because it is autonomous and capable: it edits files, runs
commands, and decides for itself for hours. So ANS's security posture is **fail-safe by default** — the
safe action (deny, revert, park, flag) is the default, and a risky action requires either an explicit
human-confirmed grant or is blocked outright. Security here is not a feature bolted on; it is the same
governance discipline applied to "what is this agent allowed to do unsupervised".

## The four design guarantees

These are stated in `SECURITY.md` as the things to test against; if you can break one, that is a
vulnerability.

1. **Never-ASK in unattended mode.** Under `CLAUDE_UNATTENDED=1`, the `deny_ask` PreToolUse hook denies the
   `AskUserQuestion` tool; the run PARKs or PROCEEDs, never blocks. A blocking question mid-run is both a
   stall and an availability problem; ANS removes the possibility structurally.
2. **Never-irreversible unsupervised.** The deny-hooks block the operations that cannot be undone
   afterward: `git push --force`, remote branch/tag deletion, release-tag push, mirror push, destructive
   SQL, disk-destructive commands (`mkfs`, `dd of=/dev/…`, `shred`), secret-path Vault writes and
   delete/rotate-root, `redis-cli flush(all|db)`, `docker volume rm`, single and mass email send,
   publishing, infra teardown (`terraform destroy`, `aws s3 rb`/`rm --recursive`), stateful `kubectl
   delete`, `systemctl mask`, `crontab -r`, and recursive `chmod`/`chown` on root/home, among others. This
   is a **backstop, not a boundary**: the matcher is shape-anchored against the shell text a tool call
   carries, not the semantics of what a script does, so it stops the honest mistake, not a determined
   attempt to route the same command through a non-shell wrapper. The reversibility safety net (git
   snapshot/revert) handles the reversible mistakes; the hooks handle the ones no revert can fix.

   **Consent manifest (the escape hatch).** At interactive `ans-run` setup a human can pre-authorize
   specific floor classes for a run; consent is stored out-of-repo (the agent can't author its own),
   frozen at launch into `UE_CONSENT` (a repo-local/agent-writable file is never trusted), and upgrades
   PARK→ALLOW only for a single clean shell statement matching exactly one consented class. It is
   per-class/once/whole-run/every-reachable-target — coarse by design, not per-target-scoped (deferred
   v2). Every consent-upgraded ALLOW is written to an out-of-repo audit trail and surfaced in the morning
   report.
3. **Secret redaction.** `redact.py` strips keys, tokens, and connection-string passwords from everything
   the run writes out — the run report, gate artefacts, Paperclip comments, emitted JSON, even the
   free-text `attempted` / `exact_blocker` fields. See [secrets](secrets.md) for how (shape-anchored
   matching + a literal-value registry).
4. **Least-privilege key source.** Credentials resolve from env or a server-managed source (Vault), never
   committed and never a literal in the config. See [secrets](secrets.md).

## Least privilege in the launcher

The launcher (`launcher.py`) is where privilege is bounded *before* the agent runs:

- **Never as root.** Started as root with a configured `launcher.target_user` → re-exec as that user;
  started as root with none → NO-GO. An unattended run must never own the machine as root.
- **Run as the target user.** The recommended setup is a cron/systemd job that runs *as* the target user
  (no sudo at all). Where the root-guard re-exec is genuinely required, use a **command-scoped** sudoers
  rule (`ops ALL=(ansrunner) NOPASSWD: /usr/local/bin/ans-run`) — **never `NOPASSWD: ALL`**, which would
  hand an autonomous shell-executing agent a passwordless privilege-escalation primitive.
- **TOFU config-trust.** A repo's config describes commands the launcher will execute; it must be trusted
  once per user and re-trusted on any change, so a malicious or altered config never runs headless. See
  [launcher](launcher.md).
- **Verified non-interactive permission mode.** A detached launch is a NO-GO unless the *resolved argv*
  actually carries a non-interactive permission flag — checked from the argv, not just the
  `autonomy_confirmed` boolean a hand-edited config could desync — so a misconfigured preset fails loudly
  instead of hanging at the first prompt. A preset can also declare a `capabilities` list to shrink the
  loaded MCP/tool surface a run exposes.
- **Reaps only its own process tree, never by name.** The watchdog reaps a run's leaked child processes
  (the agent's MCP servers, which otherwise accumulate toward OOM) **strictly by parent-chain lineage from
  the run's own pid** — never a name match. A `pkill -f claude` would match the reaping command itself and
  kill other users' / other projects' runs; rooting the walk at the agent pid means it can only reach that
  agent's own subtree (a cross-user signal fails with `EPERM` regardless), and the reaper refuses `pid ≤ 1`.
  Honest limit: a `SIGKILL`'d watchdog can't self-reap, so the leak is reduced, not eliminated. See
  [watchdog](watchdog.md).

## Cross-platform enforcement — honest status

The never-ASK and never-irreversible guarantees are enforced by a hook layer. ANS ships a shared
decision core (`enforce.py` / `enforcement.py`) and a per-platform capability matrix (`capabilities.py`).
**Only Claude Code is live-verified** — the enforcement is confirmed firing on the real tool. The other
platforms (Gemini, Codex, Copilot, Cursor, Windsurf) are **built to each platform's documented hook
contract** and hermetically tested, but not yet confirmed firing on the real tool. Where a platform cannot
natively enforce a guarantee, the degradation is surfaced as a **blind spot** in the run report —
never a silent gap. This distinction is stated everywhere it matters; ANS does not claim live verification
it does not have.

## Defense-in-depth, not a single wall

The guarantees compose: the deny-hooks stop the irreversible; the snapshot/revert makes the reversible
cheap to undo; the launcher bounds privilege before the run; redaction stops leaks on the way out; and the
blast-radius classifier keeps high-stakes decisions out of the agent's hands entirely. No single mechanism
is the whole defense — which is the point.

## Boundary

ANS secures the *execution* of an unattended run (what the agent may do, what cannot leak, what runs as
root). It does not verify the *code's* security (that is a delegated review lens — `security` — sent to
the external Tokonomix Council MCP, advisory only) and it does not choose models. See the
[glossary](glossary.md) ecosystem table.

## Limitations

The deny-hooks block an enumerated set of irreversible operations; a novel destructive command outside the
patterns could slip through, and this is broader than novelty — *any* known dangerous command routed
through a python subprocess, a git alias, or a base64-encoded script evades the patterns outright, because
the literal substrings the matcher looks for never appear on the shell line (the patterns are a backstop,
the broader defense is least privilege + running in a constrained user/container). The consent manifest has
its own honest limit: it is coarse — whole-run, all-targets — with no per-invocation or per-target scoping
(deferred v2). Live verification exists only on Claude Code today. Redaction is shape-anchored +
registry-based and stdlib-only — it is robust but not a proof that no secret in any shape can ever appear.
Report vulnerabilities per `SECURITY.md`.

---

*Verified against `agents_never_sleep/` (v1.0.0): `redact.py`, `keysource.py`, `enforce.py`,
`enforcement.py`, `capabilities.py`, `consent_store.py`, the `deny_ask` / `deny_irreversible` / Stop hooks
in `hooks/`, `launcher.py` (root-guard, TOFU), `SECURITY.md`.*
