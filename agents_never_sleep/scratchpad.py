"""Per-ticket revert-surviving scratchpad + do-not-repeat digest (ticket 04).

Two learnings from the external "endless-loop" skill (Mes review 2026-06-15) that ANS did not
yet cover:

  1. A crash mid-ticket reverts the CODE to last-green (correct) but the agent's reasoning /
     design decisions are lost — on resume it re-derives from scratch. A per-ticket
     `<ticket>.notes.md` under the state dir persists that reasoning: it lives under
     `.unattended/`, which is gitignored (never committed) AND in the git wrapper's protect set
     (revert's `git clean` never deletes it, see vcs.py) — so the notes survive a revert while
     the code is correctly rolled back to green. The file is re-injected into the PROCEED payload
     so a resumed/fresh agent CONTINUES its reasoning instead of re-deriving.

  2. A compact "tried/failed -> do not repeat" digest handed to the next (fresh or resumed) agent
     session avoids re-attempting the same dead ends.

Everything here is inert unless `autonomy.scratchpad.enabled` is set (default off): with the flag
off the PROCEED payload is byte-identical to today's.

Redaction: note text and digest fields pass through the SAME shape-anchored redactor
OutcomeStore.write uses, so a credential pasted into the agent's notes never lands on disk or in
the payload.
"""
from __future__ import annotations

import os
import time

from .fsutil import ensure_private_dir
from .redact import redact


def _safe(ticket_id: str) -> str:
    # Mirror OutcomeStore._path so a ticket's notes sit next to its .json outcome.
    return (ticket_id or "").replace("/", "_")


def notes_path(state_dir: str, ticket_id: str) -> str:
    return os.path.join(state_dir, f"{_safe(ticket_id)}.notes.md")


def append_note(state_dir: str, ticket_id: str, text: str, *, clock=time.time) -> str:
    """Append a redacted, UTC-timestamped note for a ticket; returns the notes-file path.

    O_APPEND write (atomic for the small sizes involved); 0600 because the notes may echo the
    agent's free text. Redaction mirrors OutcomeStore.write so a pasted key never lands here."""
    if not ticket_id:
        raise ValueError("append_note requires a ticket_id")
    ensure_private_dir(state_dir)
    path = notes_path(state_dir, ticket_id)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    body = redact((text or "").strip())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"\n## {stamp}\n{body}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return path


def read_notes(state_dir: str, ticket_id: str) -> str:
    """The persisted notes for a ticket, or "" if none. Read-only; never creates the file."""
    try:
        with open(notes_path(state_dir, ticket_id), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def do_not_repeat_digest(store, skip_ids, *, current_ticket_id: str = "", limit: int = 12):
    """Compact "tried -> blocked, do not repeat" list from the per-ticket outcomes of the tickets
    already attempted-and-set-aside THIS run (the skip set). Each entry: {ticket, attempted?,
    blocker?}. Redacted + length-capped so it never bloats the payload. The ticket being handed
    out is skipped — its own continuation lives in its notes file, not the digest."""
    out: list = []
    for tid in sorted(skip_ids or []):
        if tid == current_ticket_id or len(out) >= limit:
            continue
        outcome = store.read(tid)
        if outcome is None:
            continue
        entry = {"ticket": tid}
        if outcome.attempted:
            entry["attempted"] = redact(outcome.attempted)[:400]
        blocker = outcome.exact_blocker or outcome.why
        if blocker:
            entry["blocker"] = redact(blocker)[:300]
        if len(entry) > 1:  # only surface a ticket that actually carries a tried/blocked story
            out.append(entry)
    return out
