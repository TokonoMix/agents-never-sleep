"""TOFU trust for repo-supplied launcher config (security review 2026-06-10).

The threat: `.claude/agents-never-sleep.json` travels WITH the repo, and the launcher
executes what it says (agent_cmd argv, preflight check commands) BEFORE any agent-level
permission system boots. A cloned third-party repo must therefore not get silent pre-boot
code execution. Model: trust-on-first-use, like direnv `allow` / ssh known_hosts —

  * trust is recorded PER USER in ~/.config/agents-never-sleep/trusted.json as
    {repo realpath: sha256 of the config bytes},
  * any config change invalidates trust (hash mismatch == untrusted),
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
    """sha256 of the config bytes; None when the file does not exist. Pass `data` to hash
    the exact buffer that was read for parsing (avoids a TOCTOU second read of a
    repo-writable path — the trusted bytes are then the executed bytes)."""
    if data is not None:
        return hashlib.sha256(data).hexdigest()
    try:
        with open(config_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


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
