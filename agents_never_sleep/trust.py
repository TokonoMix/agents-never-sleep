"""TOFU trust for repo-supplied launcher config (security review 2026-06-10).

The threat: `.claude/agents-never-sleep.json` travels WITH the repo, and the launcher
executes what it says (agent_cmd argv, preflight check commands) BEFORE any agent-level
permission system boots. A cloned third-party repo must therefore not get silent pre-boot
code execution. Model: trust-on-first-use, like direnv `allow` / ssh known_hosts —

  * trust is recorded PER USER in ~/.config/agents-never-sleep/trusted.json as
    {repo realpath: sha256 of a canonical JSON normalization of the config — see
    `config_digest` for why raw bytes are not hashed directly},
  * any SEMANTIC config change invalidates trust (hash mismatch == untrusted); a purely
    cosmetic re-save (whitespace, key order) does not,
  * headless + untrusted == NO-GO; only an interactive human (tty) or an explicit
    `ans-run --trust` records trust,
  * a repo WITHOUT a config file needs no trust: only built-in defaults run.

The trust store lives OUTSIDE the repo on purpose — the repo must not be able to
vouch for itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

TRUST_STORE = os.path.join("~", ".config", "agents-never-sleep", "trusted.json")


def _store_path() -> str:
    # Security (review 2026-06-10): an env override of the trust STORE relocates the very
    # gate it secures — an attacker who can seed the env (cron, sudoers env_keep, .envrc)
    # could point it at a pre-seeded "everything trusted" store. Honor the override ONLY
    # under an explicit test flag; otherwise ignore it and warn loudly if it was set.
    override = os.environ.get("ANS_TRUST_STORE")
    if override and os.environ.get("ANS_TEST_MODE") == "1":
        return os.path.expanduser(override)
    if override:
        print("ans-run: ignoring ANS_TRUST_STORE (only honored with ANS_TEST_MODE=1) — "
              "a trust-store override is a security-sensitive setting", file=sys.stderr)
    return os.path.expanduser(TRUST_STORE)


def config_digest(config_path: str, *, data: bytes | None = None) -> str | None:
    """sha256 of a CANONICAL JSON normalization of the config; None when the file does not
    exist. Pass `data` to canonicalize the exact buffer that was read for parsing (avoids a
    TOCTOU second read of a repo-writable path — the trusted bytes are then the executed
    bytes).

    Scope (consensus T4, deliberately conservative): the config is parsed and re-serialized
    as `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (UTF-8)
    before hashing, instead of hashing raw bytes. This kills COSMETIC re-trust churn —
    whitespace, indentation, key reordering from a re-save — while weakening nothing: every
    key and value (commands, autonomy flags, gate names, agent presets, anything) is still
    part of the canonical form, so any semantic change still yields a different digest.

    Full field-level narrowing (hashing only a curated set of "security-relevant" fields via
    a TRUST_FIELDS allowlist) is deliberately DEFERRED, not built here — the consensus
    flagged it as a maintenance-drift footgun: a future field could silently default OUT of
    an allowlist and stop being covered by trust. If that is ever pursued, it MUST use a
    named, versioned DENY-list of ignorable fields (new fields default IN-scope/hashed) —
    never an allow-list where new fields default out.

    On invalid JSON / a parse error this falls back to the raw-bytes sha256 (same as the
    pre-canonicalization behavior) — an unparseable config still gets a deterministic digest
    and config_digest never raises."""
    if data is not None:
        raw = data
    else:
        try:
            with open(config_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
    try:
        obj = json.loads(raw.decode("utf-8"))
        canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
    except (UnicodeDecodeError, ValueError):
        canonical = raw  # fallback: hash the raw bytes, never crash on unparseable input
    return hashlib.sha256(canonical).hexdigest()


def _load_store() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_trusted(repo: str, config_path: str, *, digest: str | None = None) -> bool:
    """True when the repo has no config (nothing repo-supplied executes) or the recorded
    hash matches the config bytes. Pass `digest` (hash of the buffer actually parsed) to
    avoid a second read of the repo-writable config."""
    digest = digest if digest is not None else config_digest(config_path)
    if digest is None:
        return True
    return _load_store().get(os.path.realpath(repo)) == digest


def record_trust(repo: str, config_path: str, *, digest: str | None = None) -> str | None:
    """Persist the config hash for this repo. Returns the digest, or None when there is no
    config to trust."""
    digest = digest if digest is not None else config_digest(config_path)
    if digest is None:
        return None
    path = _store_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    store = _load_store()
    store[os.path.realpath(repo)] = digest
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return digest
