"""Secret redaction — scrub credentials from everything the night WRITES OUT.

An unattended run writes to five surfaces that can leak a secret a failing test or a careless log
printed: the morning report, the saved gate artifacts (raw command stdout — the biggest risk), the
JSON the driver emits, the persisted outcome store (`.unattended/state/*.json` — the agent's
free-text `attempted`/`exact_blocker` could carry a pasted credential; scrubbed on write as
defense-in-depth, T05), and — irreversibly — Paperclip comments on shared infra. This module is the
single chokepoint applied at all five.

NOT in scope — a deliberate boundary: the agent's own raw run-log (`.unattended/logs/`, written
straight from the spawned CLI's stdout/stderr in background mode) is NOT redacted. It is the agent's
own output stream, the agent legitimately holds the gateway key it was launched with, and it can
emit that key anywhere (files it writes, network, non-line-buffered binary output) — so a partial
line-redactor over the log would add launcher-hot-path complexity and a false sense of safety. The
launcher instead shrinks the blast radius the cheap, complete way: the log is created 0600
(owner-only, never world-readable; see `bin/ans-run`'s `open_log`). For an UNTRUSTED agent binary,
prefer `--fg` (output goes to the operator's terminal, not an on-disk log) or a `launcher.log_dir`
on restricted storage.

DESIGN (deliberate, learned the hard way): match a secret by its VALUE SHAPE, never by a nearby
keyword. This codebase's own legitimate output is saturated with the words a naive scrubber keys on —
"token" (tokonomix), "secret", "Authorization" (the Paperclip client), "security" (a specialist
lens), "password" — and `build_report` runs inside the acceptance tests. A keyword-anchored pattern
would shred report prose and coverage tags (and break the suite). So every pattern below is anchored
to the credential's own structure (a `pcp_` board token, a JWT's three base64 segments, a PEM block,
`scheme://user:pass@host`, a provider key prefix). Notably we do NOT redact bare long hex/base64:
a 40-char hex string is just as likely a git SHA (which the harness commits/reports) as a secret.

Pattern matching is the backstop; the LITERAL-VALUE REGISTRY is the precise half — register the exact
secret strings the process knows (PAPERCLIP_TOKEN from env now; Vault-resolved values in a later
slice) and they are scrubbed verbatim even when they match no pattern. Stdlib-only, deterministic.
"""
from __future__ import annotations

import os
import re

# Literal secret values to scrub verbatim (exact-match). Populated per-process from env / Vault.
# Short values are refused on registration (a 3-char value would match everywhere, incl. "" → every
# gap), so this set only ever holds genuinely secret-length strings.
_SECRETS: set = set()
_MIN_SECRET_LEN = 8

# Env var NAMES whose VALUES are known credentials worth scrubbing. We harvest the value, never the
# name. Kept to an explicit allowlist so we don't vacuum unrelated config into the scrubber.
_SECRET_ENV_VARS = (
    "PAPERCLIP_TOKEN", "VAULT_TOKEN", "TOKONOMIX_API_KEY", "TOKONOMIX_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)

# Shape-anchored patterns. Each (compiled regex, replacement). `\g<...>` keeps a non-secret prefix
# (e.g. the literal "Bearer ") and replaces only the credential.
_PATTERNS = [
    # PEM private key block (multi-line) — the whole block.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED:private-key]"),
    # JWT — three base64url segments. (The middle starts eyJ too; require it to avoid version strings.)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
     "[REDACTED:jwt]"),
    # Paperclip board/API token: pcp_<kind>_<40+ hex>.
    (re.compile(r"\bpcp_[a-z]+_[0-9a-f]{16,}"), "[REDACTED:paperclip-token]"),
    # HashiCorp Vault tokens.
    (re.compile(r"\bhv[sb]\.[A-Za-z0-9]{20,}"), "[REDACTED:vault-token]"),
    (re.compile(r"\bs\.[A-Za-z0-9]{24,}"), "[REDACTED:vault-token]"),
    # Provider API keys with a distinctive prefix.
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "[REDACTED:openai-key]"),
    # Tokonomix live keys: tok_live_<base62 body 8+ chars>. Body minimum is 8 (not 20) because
    # short keys issued during onboarding and sandbox trials are valid credentials.
    (re.compile(r"\btok_live_[A-Za-z0-9]{8,}"), "[REDACTED:tokonomix-key]"),
    # Tokonomix key-name label patterns: TOKONOMIX_API_KEY=<value> or TOKONOMIX_KEY=<value>.
    # Catches raw values that do NOT carry the tok_live_ prefix (e.g. test-issuance, rotated keys).
    (re.compile(r"(?i)(?P<p>TOKONOMIX_(?:API_)?KEY\s*[:=]\s*)[A-Za-z0-9._~+/=\-]{8,}"),
     r"\g<p>[REDACTED:tokonomix-key]"),
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    # Authorization header value (Bearer/Basic/<token>) — keep the field name, drop the value.
    (re.compile(r"(?i)(?P<p>authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[A-Za-z0-9._~+/=\-]{8,}"),
     r"\g<p>[REDACTED:authorization]"),
    # Bare "Bearer <token>".
    (re.compile(r"(?i)(?P<p>bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\g<p>[REDACTED:bearer]"),
    # Credentials embedded in a connection URL: scheme://user:password@host -> scheme://user:***@host.
    (re.compile(r"(?P<p>[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(?P<a>@)"),
     r"\g<p>[REDACTED:url-credential]\g<a>"),
]


def register_secret(value) -> bool:
    """Register one literal secret to be scrubbed verbatim everywhere. Refuses falsy / short values
    (a short string would over-match). Returns True iff it was added."""
    if not value:
        return False
    v = str(value).strip()
    if len(v) < _MIN_SECRET_LEN:
        return False
    _SECRETS.add(v)
    return True


def register_env_secrets() -> int:
    """Harvest known-credential env VALUES into the registry. Called at every CLI entry because each
    next/complete is a fresh process. Returns how many were registered."""
    n = 0
    for name in _SECRET_ENV_VARS:
        if register_secret(os.environ.get(name)):
            n += 1
    return n


def redact(text):
    """Return `text` with every known literal secret and every shape-matched credential replaced.
    Non-secret content (incl. this codebase's 'token'/'security'/'council:pass' vocabulary, ticket
    IDs and git SHAs) is returned untouched. Non-str input is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    # Literal values first (longest first so an overlapping shorter secret can't leave a fragment).
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, "[REDACTED:value]")
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(obj):
    """Recursively redact every string inside a JSON-like structure (dict/list/str); other scalars
    pass through. Used for the JSON the driver emits to stdout."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
