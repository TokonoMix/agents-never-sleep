# ANS Secrets — redaction & key source

> **30-second version.** ANS keeps credentials out of two places: out of what the run **writes** (secret
> redaction scrubs keys/tokens/passwords from reports, logs, comments, and emitted JSON) and out of the
> **config** (credentials are resolved from `env:` or `vault:` token-refs, never stored as literals). The
> clever part of redaction is that it matches a secret by its **value shape**, never by a nearby keyword —
> because ANS's own legitimate output is full of words like "token", "auth", and "password". See
> [security](security.md), [launcher](launcher.md), [glossary](glossary.md).

## Two problems, two mechanisms

1. **Secrets must not leak on the way out** — a resolved key, a pasted credential in a free-text field, a
   connection string in a log. → **Secret redaction** (`redact.py`).
2. **Secrets must not live in the config or the repo** — the config travels with the repo and is
   inspected; a literal key there is a leak waiting to happen. → **The key source** (`keysource.py`),
   which resolves token-*refs*.

## Secret redaction (`redact.py`)

ANS scrubs credentials from **everything the run writes out**: the run report, gate artefacts,
Paperclip comments, emitted JSON, and the free-text `attempted` / `exact_blocker` fields (which could carry
a pasted credential) — scrubbed on write. Two complementary halves:

- **Shape-anchored patterns (the backstop).** Each pattern matches a secret by its **value shape**, never
  by a nearby keyword. This is deliberate and was learned the hard way: ANS's own legitimate output is
  saturated with the words a naive scrubber keys on — "token", "auth", "key", "secret", "password",
  "bearer" appear in report prose, coverage tags, and the security lens. A keyword-anchored scrubber would
  shred legitimate prose (and break the acceptance suite, since `build_report` runs inside it). So patterns
  are anchored on the *value* (e.g. a long high-entropy token-shaped string, a connection-string password),
  with `\g<...>` preserving any non-secret prefix.
- **The literal-value registry (the precise half).** The exact resolved credential values are *registered*
  (`register_secret`) and scrubbed **verbatim** everywhere, even when they match no pattern. Because each
  `next`/`complete` is a fresh process, `register_env_secrets()` is called at every CLI entry to harvest
  known-credential env *values* into the registry — harvesting the **value**, never the name, and only from
  an explicit env-name allowlist so unrelated config isn't vacuumed in. Resolved Vault / launcher
  credentials register themselves too. Stdlib-only and deterministic.

### The one residual surface, stated honestly

The spawned agent legitimately *holds* the resolved key, and its raw background run-log
(`.unattended/logs/`) is the agent's own output stream — **not** redaction-scoped. ANS does not pretend to
scrub it. Instead the launcher shrinks the blast radius the cheap, complete way: it creates that log
**`0600`** (owner-only, never world-readable; see `bin/ans-run`'s `open_log`). For an untrusted agent
binary, use `--fg` or a restricted `launcher.log_dir`. This is the honest design — a real surface, bounded
by file permissions rather than a false claim of scrubbing.

## The key source (`keysource.py`)

Credentials are referenced, never embedded. A `token_ref` is the form the wizard already writes:

- **`env:VAR`** — read an environment variable (resolved from the launcher's own environment at spawn).
- **`vault:<mount>/<path>[#field]`** — read a KV-v2 secret from the configured HashiCorp Vault.

The Vault contract (`keysource.py`, `VaultClient`): loopback `http://127.0.0.1:8200`, KV-v2, auth via a
direct `VAULT_TOKEN` *or* AppRole login (`role_id` + `secret_id` → `auth.client_token`, cached), reading at
`/v1/<mount>/data/<rest>`. Whatever is resolved is **registered with the redaction layer** so it can never
appear in a log or report, and a token / secret-id / KV value **never** appears in an exception message.

**Remote Vault must be https.** The shipped default is loopback, so the auth material never leaves the
host. If you point `VAULT_ADDR` (or `integrations.paperclip.base_url`) at a **remote** host, it must be
`https://` — a non-loopback `http://` endpoint puts the credential on the wire in cleartext.
`keysource.is_safe_endpoint()` is the check; a resolution that succeeds against an unsafe endpoint still
raises a run-report blind spot (it does not block the run — only a resolution *failure* is a hard-fail).

### Resolve-time policy: fail-closed at launch, degrade at runtime

The two failure modes are deliberately different:

- **At launch** (the launcher resolving a credential the run *needs* to start): a failed resolution —
  missing env var, unreadable Vault path, disabled Vault integration — is a **blocking NO-GO** with a clear
  message, never a silent empty value. (Resolution happens *before* the capability probe — probe == spawn
  rule.) A literal value that *looks* like a pasted key is loudly flagged.
- **At runtime** (a configured source unreadable mid-run, e.g. the Paperclip token): the unattended run
  must never hard-stop, so the read **degrades** — it returns no value plus a run-report **blind spot**,
  and the run continues with that capability disabled.

## The managed-routing tie-in

When a launcher preset points the agent at the Tokonomix gateway, the gateway key is an `env:` /`vault:`
token-ref — resolved into the child env before the probe, registered with redaction, never printed. Which
launcher env vars an `env:` ref may pull into the child is part of what a human vouches for at `--trust`
time. See [launcher](launcher.md).

`run.py` also resolves a Vault-backed Tokonomix key into `os.environ` for its own in-process consumers
(the onboarding gate, council). That process-wide env is *not* what a **gate command** runs with: the
gate is the target repo's own, arbitrary test/build command, so `gates._run_command` strips ANS's own
known resolved-credential env vars (`redact.known_secret_env_var_names()`) before spawning it — a
`vault:`/`env:` ref's whole point is keeping the key out of reach of code that doesn't need it, and the
gate command is exactly that code.

## Boundary

This is *execution-side* secret hygiene — keeping credentials out of ANS's own output and config. It is not
application secret management for the code being built, and it does not verify the code's handling of
secrets (that is the delegated `security` review lens). See the [glossary](glossary.md) ecosystem table.

## Limitations

Shape-anchored matching is a backstop, not a proof that a secret in *any* conceivable shape is caught — the
literal-value registry is the precise guarantee, and it only covers values ANS actually resolved or
harvested from the allowlist. The agent's own run-log is owner-only (`0600`), not scrubbed. The acceptance
suite uses **fake fixtures only** — no real credential exists anywhere in the repo.

---

*Verified against `agents_never_sleep/` (v1.0.0): `redact.py` (shape-anchored `_PATTERNS`, literal-value
registry, `register_secret`, `register_env_secrets`, env-name allowlist, `0600` log rationale),
`keysource.py` (`resolve_ref`, `env:`/`vault:`, `VaultClient` KV-v2 / AppRole / `VAULT_TOKEN`, register +
degrade), `bin/ans-run` (`open_log`), `SECURITY.md`.*
