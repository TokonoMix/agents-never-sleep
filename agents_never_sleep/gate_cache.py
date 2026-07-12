"""Gate-baseline reuse cache — a PURE OPTIMIZATION (INT-2330 area, Q&A item 14).

Running the full gate suite twice per ticket (once as "baseline" at `begin_proceed`, once
"after edit" at finalize) is wasteful when the tree the next ticket's baseline would check is
byte-identical to the tree a just-completed ticket's post-edit gate already proved PASS on: the
same command against the same tree can only give the same answer. So a green `complete` writes a
content-addressed receipt (git tree id + the exact gate command), and the next `begin_proceed`
may reuse it instead of re-running the gate.

On ANY doubt this must fall back to running the gate for real — a wrong reuse would poison the
FAIL_INTRODUCED_BY_DIFF / FAIL_PREEXISTING taxonomy gates.py exists to protect. Concretely:
  * the working tree must be CLEAN (a dirty tree isn't the tree the cache describes)
  * the tree id must match EXACTLY (git rev-parse HEAD^{tree} is content-addressed: any file
    byte anywhere flips it)
  * the gate command must match EXACTLY (a config edit invalidates the cache)
  * the cache file must parse and carry `result: PASS` (anything else — missing, corrupt,
    truncated, a stale non-PASS entry — is treated as a miss, never as a crash)

This module never raises: every function degrades to None / a silent no-op on any IO, subprocess,
or parse failure, exactly like the fail-safe conventions in gates.py and state.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from .fsutil import ensure_private_dir

CACHE_FILENAME = "gate-baseline-cache.json"

# Same rationale as gates.py's _NONINTERACTIVE_ENV: these are read-only git probes, but a hung
# credential/terminal prompt must never be possible even for `status`/`rev-parse`.
_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "CI": "1",
    "NO_COLOR": "1",
}


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(_NONINTERACTIVE_ENV)
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
        return proc.returncode, proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def tree_id(repo_dir: str) -> str | None:
    """`git rev-parse HEAD^{tree}` — but ONLY when the working tree is clean (`git status
    --porcelain` empty). A dirty tree means uncommitted edits exist that the tree id would not
    reflect, so reusing a cached baseline against it would be wrong. Any git failure (timeout,
    not a repo, no HEAD yet) -> None: the caller must treat that as "run the gate for real"."""
    rc, out = _run_git(["status", "--porcelain"], repo_dir)
    if rc != 0 or out.strip() != "":
        return None
    rc, out = _run_git(["rev-parse", "HEAD^{tree}"], repo_dir)
    if rc != 0:
        return None
    sha = out.strip()
    return sha or None


def read(path: str) -> dict | None:
    """Fail-safe read: a missing file, unreadable file, or unparsable/malformed JSON all -> None.
    The cache is purely an optimization — a corrupt cache must never block or crash the run."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write(path: str, *, tree_id: str, command: list[str]) -> None:
    """Record a green complete's baseline receipt. Atomic (temp file + fsync + replace), like
    state.py's writes. Fail-safe: any IO error is silently swallowed — a cache-write failure must
    never fail the ticket that just went green."""
    data = {
        "tree_id": tree_id,
        "command": list(command),
        "result": "PASS",
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        ensure_private_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass  # optimization only — never let a cache-write failure surface to the caller


def hit(path: str, *, current_tree_id: str | None, command: list[str]) -> bool:
    """True only when a clean tree_id was computed AND the cache exactly matches it and the gate
    command AND is recorded PASS. Centralizes the match rule so begin_proceed and tests agree on
    exactly what counts as a reuse (bit-exact tree + bit-exact command, nothing looser)."""
    if current_tree_id is None:
        return False
    cached = read(path)
    if not cached:
        return False
    return (cached.get("result") == "PASS"
            and cached.get("tree_id") == current_tree_id
            and cached.get("command") == list(command))
