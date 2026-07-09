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
- **Never-irreversible unsupervised** — force-push, remote branch/tag delete, destructive SQL,
  and disk-destructive commands (`mkfs`, `dd of=/dev/…`, `shred`) are denied by the hooks. This is a
  **backstop, not a boundary**: the deny patterns (`agents_never_sleep/enforcement.py`, the single
  source of truth every platform hook shares) are shape-anchored and robust against standard-form
  variance — whitespace, short-flag bundling/reordering (`-qf`, `-fr`), long- vs short-flag spelling,
  force-via-`+`refspec — but cannot see through a command routed via a non-shell wrapper (a Python
  `subprocess` call, a git alias, a base64-decoded script), which never produces the literal
  substrings the matcher looks for.
- **Secret redaction** — `agents_never_sleep/redact.py` strips keys/tokens/connection-string passwords from
  logs and reports. The acceptance suite (`acceptance/test_redact.py`, `test_keysource.py`) uses
  **fake fixtures only** — no real credentials exist anywhere in this repo.
- **Key source** — credentials resolve from env / a server-managed source, never committed.

If you find a way to make the harness ASK in unattended mode, perform an irreversible action
through the hooks, or leak a secret into a log/report, that's a vulnerability — please report it.
