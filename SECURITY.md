# Security Policy

`agents-never-sleep` runs autonomous coding agents over a backlog, so its whole
design is about *not* doing anything irreversible unsupervised. Security reports
are taken seriously.

## Reporting a vulnerability

**Do not open a public issue for security problems.** Email **security@tokonomix.ai**
with a description, impact, reproduction steps, and the affected version/commit.

We aim to acknowledge within **2 business days** and to give a remediation timeline
after triage. Please allow a reasonable window before public disclosure; we credit
reporters who follow coordinated disclosure.

## Design guarantees (what to test against)

- **Never-ASK in unattended mode** — an ask-tool is denied; the run PARKs or PROCEEDs, never blocks.
- **Never-irreversible unsupervised (two controls, two jobs).** The enforcement model is a
  conservative deny-list **floor** plus a run-setup **consent manifest**:
  - **The floor** (`agents_never_sleep/enforcement.py`, the single source of truth every platform
    hook shares) DENYs the genuinely-irreversible / outward-mass shapes by default: force-push,
    remote branch/tag delete, release-tag push, destructive SQL, disk-destructive commands (`mkfs`,
    `dd of=/dev/…`, `shred`), `redis-cli flush(all|db)`, `docker volume rm`, secret-path
    `vault kv put`/`write`, single + mass email, publishing, infra teardown (`terraform destroy`,
    `aws s3 rb`/`rm --recursive`), stateful `kubectl delete`, `systemctl mask`, `crontab -r`, prune,
    `compose down -v`, power-state, recursive `chmod/chown` on root/home. It is a **backstop, not a
    boundary**: the patterns are shape-anchored and robust against standard-form variance
    (whitespace, short-flag bundling/reordering like `-qf`/`-fr`, long- vs short-flag spelling,
    force-via-`+`refspec) but cannot see through a command routed via a non-shell wrapper (a Python
    `subprocess` call, a git alias, a base64-decoded script), which never produces the literal
    substrings the matcher looks for. This is the trusted-run threat model: a backstop against an
    honest mistake, not a defence against a determined evader.
  - **The consent manifest** is the operator's explicit escape hatch: at interactive setup a human
    can pre-authorize specific floor classes for a run. Consent lives OUT-OF-REPO
    (`~/.config/agents-never-sleep/consent/…`, TOFU-style like the config-trust store) so the
    unattended agent structurally cannot author its own consent; it is frozen at launch into the
    `UE_CONSENT` env var (a child cannot rewrite its parent's env), read ONLY from that env (never a
    repo file), and upgrades PARK→ALLOW only for a **single clean shell statement** matching
    **exactly one** consented class — any chaining (`;`, `&`, `&&`, `||`, `|`), substitution,
    redirection, newline, or interpreter `-c` wrapper voids the upgrade. Missing/malformed/oversized
    consent → no consent → the floor holds.
  - **Honest limits of consent (by design, not oversold):** a grant is per-class, once, at setup,
    for the WHOLE run across EVERY reachable target — there is no per-invocation or per-target check,
    so pre-authorizing `redis_flush` to clear a dev cache also authorizes an errant `flushall`
    against a prod Redis reachable in the same run. Pre-authorize high-blast classes only for runs
    scoped to non-critical environments; structured target-scoping is the deferred v2 feature.
    Consent is keyed to the primary repo where the wizard captured it and frozen only by the
    `ans-run` launcher: a run whose `--repo` realpath differs, or a raw `claude -p` launched by
    cron/`claude-run` without the launcher, simply gets no consent → the floor holds (fail-safe).
- **Secret redaction** — `agents_never_sleep/redact.py` strips keys/tokens/connection-string passwords from
  logs and reports. The acceptance suite (`acceptance/test_redact.py`, `test_keysource.py`) uses
  **fake fixtures only** — no real credentials exist anywhere in this repo.
- **Key source** — credentials resolve from env / a server-managed source, never committed.

If you find a way to make the harness ASK in unattended mode, perform an irreversible action
through the hooks, or leak a secret into a log/report, that's a vulnerability — please report it.
