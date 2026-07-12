"""Shared filesystem helper for the `.unattended/` state tree.

Every writer under `.unattended/` (outcome store, ledger, heartbeat, run-branch/pending/progress
JSON, gate cache, scratchpad notes, the sentinel/session-budget files, the tree lock) holds
ticket context and — as defense-in-depth, since `redact.py` is the precise guarantee — the agent's
own free-text fields, which could carry a pasted credential. `launcher.open_log` already chmods the
run-log directory to 0700 and `scratchpad.append_note` already opens notes at 0600; this closes the
same gap for the directories those writers create under a permissive umask (022), which would
otherwise leave `.unattended/` world-readable.
"""
from __future__ import annotations

import os


def ensure_private_dir(path: str) -> None:
    """mkdir -p `path` at 0700, and chmod it to 0700 even when it already existed — a directory
    created before this hardening (or by an older ANS version) gets tightened retroactively, not
    just directories created fresh. No-op on an empty path (never touches an ambient directory like
    CWD)."""
    if not path:
        return
    abspath = os.path.abspath(path)
    # os.makedirs applies `mode` only to the LEAF; intermediate dirs it creates get the umask
    # default (0755 under umask 022), so a fresh `.unattended` parent would be left world-listable
    # while its secret-bearing children are 0700. Record which ancestors don't exist yet (makedirs
    # will create them) and tighten each, stopping at the first EXISTING ancestor so we never chmod
    # a pre-existing dir like the repo root.
    created = []
    p = abspath
    while p and not os.path.exists(p):
        created.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(abspath, mode=0o700, exist_ok=True)
    # chmod the leaf (retroactive tighten even when it already existed) + every ancestor just created.
    for d in {abspath, *created}:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
