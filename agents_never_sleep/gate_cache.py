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

import hashlib
import json
import os
import subprocess
import time

from .fsutil import ensure_private_dir

CACHE_FILENAME = "gate-baseline-cache.json"

# Execution-relevant IGNORED files (t06): files git ignores but the Python interpreter or a test
# harness auto-loads at runtime, so their content changes what the gate observes WITHOUT changing
# the git tree id. `git status --porcelain` (no --ignored) omits ignored files, so a gitignored
# `sitecustomize.py` / `.env` / `conftest.py` leaves the tree "clean" and its tree id stable — a
# stale green would be reused. We fold a content digest of exactly these files into the cache key
# so any add/change forces a miss. Deliberately NOT a blanket "any ignored file" guard: repos
# routinely ignore `.unattended/`, `__pycache__/`, `node_modules/`, build artifacts, `*.log` —
# hashing those would disable the cache for essentially every real run (a wrong PARK of the
# optimization, not a correctness bug, but pointless). Same "enumerated, not exhaustive" honesty
# as the deny-list: an ignored file the gate happens to read that is not named here is out of
# scope, and an execution-relevant file inside a wholly-ignored directory (git collapses it to
# `dir/`, so no basename match) is likewise not covered.
_EXEC_RELEVANT_IGNORED_NAMES = {"sitecustomize.py", "usercustomize.py", "conftest.py", ".env"}
_EXEC_RELEVANT_IGNORED_SUFFIXES = (".pth",)


def _is_exec_relevant(rel_path: str) -> bool:
    base = os.path.basename(rel_path.rstrip("/"))
    return base in _EXEC_RELEVANT_IGNORED_NAMES or base.endswith(_EXEC_RELEVANT_IGNORED_SUFFIXES)

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


def _exec_relevant_ignored_digest(repo_dir: str) -> str | None:
    """A stable digest of the execution-relevant IGNORED files present (name+content), or "" when
    there are none. None signals "cannot determine" (git probe failed) — the caller must fall back
    to running the gate rather than risk a stale hit. Only reached when the plain tree is already
    clean, so `git status --porcelain --ignored` output is purely `!!` (ignored) lines here."""
    rc, out = _run_git(["status", "--porcelain", "--ignored"], repo_dir)
    if rc != 0:
        return None
    relevant = sorted(
        line[3:] for line in out.splitlines()
        if line.startswith("!! ") and _is_exec_relevant(line[3:])
    )
    if not relevant:
        return ""
    h = hashlib.sha256()
    for rel in relevant:
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        try:
            with open(os.path.join(repo_dir, rel), "rb") as fh:
                h.update(fh.read())
        except OSError:
            # Unreadable but present: still perturb the digest so a hit is never served blind.
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def tree_id(repo_dir: str) -> str | None:
    """`git rev-parse HEAD^{tree}` — but ONLY when the working tree is clean (`git status
    --porcelain` empty). A dirty tree means uncommitted edits exist that the tree id would not
    reflect, so reusing a cached baseline against it would be wrong. Any git failure (timeout,
    not a repo, no HEAD yet) -> None: the caller must treat that as "run the gate for real".

    The key also folds a digest of execution-relevant IGNORED files (t06): those never move the
    git tree id but do change runtime behaviour, so an agent adding a gitignored `sitecustomize.py`
    would otherwise get a false green. When such files are present the returned key is
    `<sha>:<digest>` (still a stable, content-addressed string — an identical future tree hits);
    with none present the key is the bare `<sha>` (byte-identical to pre-t06, so existing receipts
    still match)."""
    rc, out = _run_git(["status", "--porcelain"], repo_dir)
    if rc != 0 or out.strip() != "":
        return None
    rc, out = _run_git(["rev-parse", "HEAD^{tree}"], repo_dir)
    if rc != 0:
        return None
    sha = out.strip()
    if not sha:
        return None
    digest = _exec_relevant_ignored_digest(repo_dir)
    if digest is None:
        return None  # cannot rule out a poisoning ignored file -> fall back to the real gate
    return f"{sha}:{digest}" if digest else sha


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
