"""Out-of-repo consent store — Part B of the unattended safety model (spec
docs/superpowers/specs/2026-07-14-ans-unattended-safety-model.md).

The threat this closes: the deny-list floor (enforcement._IRREVERSIBLE) is the safety floor for
an unattended run, but a human may legitimately want to pre-authorize a specific action CLASS
(e.g. redis_flush) for a run scoped to a non-critical environment. That pre-authorization must be
recorded somewhere the UNATTENDED AGENT cannot itself write — otherwise the agent could simply
grant itself consent mid-run. Same TOFU threat model as trust.py: the consent gate lives OUTSIDE
the repo, keyed by an out-of-repo path, so the repo (and anything running inside it) cannot
relocate or author its own consent record. Only an interactive human, via the config wizard
(config.py, past the is_interactive() bail), ever writes here.

Pure, import-safe module: no repo reads, no repo writes — the repo_dir argument is used ONLY to
derive a filename (a hash of its realpath), never to locate or influence where the store itself
lives.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from . import fsutil

CONSENT_STORE_DIR = os.path.join("~", ".config", "agents-never-sleep", "consent")


def _store_dir() -> str:
    # Security (mirrors trust._store_path): an env override of the consent STORE relocates the
    # very gate it secures — an attacker who can seed the env could point it at a pre-seeded
    # "everything pre-authorized" store. Honor the override ONLY under an explicit test flag;
    # otherwise ignore it and warn loudly if it was set. The store dir is ALWAYS derived from
    # ~/.config (or this test override) — NEVER from repo_dir contents.
    override = os.environ.get("ANS_CONSENT_STORE")
    if override and os.environ.get("ANS_TEST_MODE") == "1":
        return os.path.expanduser(override)
    if override:
        print("ans-run: ignoring ANS_CONSENT_STORE (only honored with ANS_TEST_MODE=1) — "
              "a consent-store override is a security-sensitive setting", file=sys.stderr)
    return os.path.expanduser(CONSENT_STORE_DIR)


def _repo_id(repo_dir: str) -> str:
    """Filesystem-safe per-repo key — a hash, never the path itself, so the store dir's
    directory listing does not leak repo locations and the filename can't escape the store."""
    return hashlib.sha256(os.path.realpath(repo_dir).encode()).hexdigest()[:16]


def _store_path(repo_dir: str) -> str:
    return os.path.join(_store_dir(), f"{_repo_id(repo_dir)}.json")


def read(repo_dir: str) -> dict:
    """Fail-safe — missing file / unreadable / non-dict / bad JSON all read as {} (exactly like
    trust._load_store; a corrupt or absent consent store must never raise or wedge a run)."""
    try:
        with open(_store_path(repo_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def audit_path(repo_dir: str) -> str:
    """Where consent-upgraded actions for this repo get logged (t04). Same per-repo keying as
    `_store_path` (a hash of the repo's realpath, never the path itself), so the audit trail lives
    beside the consent grant it records — out-of-repo, for the same TOFU reason `_store_dir`
    documents above."""
    return os.path.join(_store_dir(), f"{_repo_id(repo_dir)}.audit.jsonl")


def append_audit(path: str, event: dict) -> None:
    """Append ONE json-line audit event. Best-effort: an audit-write failure must never block or
    slow the enforcement hook it's called from — enforcement itself is NOT best-effort, only this
    trail is. Swallow every OSError (missing parent, permissions, full disk, ...)."""
    try:
        fsutil.ensure_private_dir(os.path.dirname(path))
        is_new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        if is_new:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except OSError:
        pass


def read_audit(path: str) -> list[dict]:
    """Fail-safe read of the audit trail — missing/unreadable file -> []; a malformed line is
    skipped (never raises), so one corrupt line can't hide the rest of the trail."""
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def write(repo_dir: str, *, actions: dict, by: str, backlog_fingerprint: str | None = None) -> None:
    """Persist which deny-list action classes are pre-authorized for this repo. Atomic
    (tempfile + fsync + os.replace), store dir created private (0700) via fsutil, file
    chmod'd 0600 — same durability/privacy posture as trust.record_trust."""
    store_dir = _store_dir()
    fsutil.ensure_private_dir(store_dir)
    payload = {
        "actions": actions,
        "captured": {
            "by": by,
            "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "backlog_fingerprint": backlog_fingerprint,
        },
    }
    path = _store_path(repo_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
