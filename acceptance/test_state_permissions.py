#!/usr/bin/env python3
"""`.unattended/` state-directory permission test (post-audit hardening, L1).

Every writer under `.unattended/` (outcome store, ledger, heartbeat, gate cache, run-branch/
pending/progress JSON, the tree lock, scratchpad notes) holds ticket context and — as
defense-in-depth — the agent's own free-text fields, which could carry a pasted credential. Under a
permissive umask (022, the common default), a plain `os.makedirs(path, exist_ok=True)` leaves that
tree world-readable. `agents_never_sleep.fsutil.ensure_private_dir` is now the sole path every one
of those writers uses to create its directory; this test pins that it actually forces 0700 — both on
a fresh directory and, just as important, on one that pre-dates the hardening (an existing, looser
directory left behind by an older ANS version or a permissive umask).

Exit 0 = GREEN.
"""
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.fsutil import ensure_private_dir  # noqa: E402
from agents_never_sleep.heartbeat import Heartbeat  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.state import OutcomeStore, OutcomeState, TicketOutcome  # noqa: E402


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ensure_private_dir_fresh(failures):
    work = tempfile.mkdtemp(prefix="ue-perm-")
    target = os.path.join(work, "fresh")
    ensure_private_dir(target)
    if _mode(target) != 0o700:
        failures.append(f"[fresh] expected 0700, got {oct(_mode(target))}")


def test_ensure_private_dir_retroactive(failures):
    # A directory that pre-dates the hardening (created world-readable by something else, or under
    # a permissive umask) must be TIGHTENED, not left alone — exist_ok=True on a bare os.makedirs
    # would silently skip a pre-existing looser directory.
    work = tempfile.mkdtemp(prefix="ue-perm-")
    target = os.path.join(work, "stale")
    os.makedirs(target, mode=0o777)
    os.chmod(target, 0o755)  # umask can strip makedirs' mode= bits; force the stale state directly
    if _mode(target) == 0o700:
        failures.append("[retro] test setup bug: directory already 0700 before ensure_private_dir")
    ensure_private_dir(target)
    if _mode(target) != 0o700:
        failures.append(f"[retro] pre-existing looser dir was not tightened: {oct(_mode(target))}")


def test_ensure_private_dir_empty_path_is_noop(failures):
    # Must never resolve to CWD and chmod it — os.path.dirname("bare.json") == "".
    cwd_mode_before = _mode(".")
    ensure_private_dir("")
    if _mode(".") != cwd_mode_before:
        failures.append("[empty] an empty path must never touch the ambient CWD")


def test_outcome_store_dir_is_private(failures):
    work = tempfile.mkdtemp(prefix="ue-perm-")
    state_dir = os.path.join(work, ".unattended", "state")
    store = OutcomeStore(state_dir)
    if _mode(state_dir) != 0o700:
        failures.append(f"[outcome-store] state dir not 0700: {oct(_mode(state_dir))}")
    store.write(TicketOutcome(ticket_id="t1", state=OutcomeState.DONE))
    if _mode(state_dir) != 0o700:
        failures.append(f"[outcome-store] state dir drifted after write: {oct(_mode(state_dir))}")


def test_ledger_and_heartbeat_dirs_are_private(failures):
    work = tempfile.mkdtemp(prefix="ue-perm-")
    state_dir = os.path.join(work, ".unattended", "state")
    os.makedirs(state_dir, mode=0o777)
    os.chmod(state_dir, 0o755)  # simulate a pre-hardening directory shared by both writers

    AttemptLedger(os.path.join(state_dir, "ledger.json")).record_attempt("t1")
    if _mode(state_dir) != 0o700:
        failures.append(f"[ledger] shared state dir not tightened: {oct(_mode(state_dir))}")

    hb = Heartbeat(os.path.join(state_dir, "heartbeat.json"))
    hb.beat("t1", "next")
    if _mode(state_dir) != 0o700:
        failures.append(f"[heartbeat] shared state dir not tightened: {oct(_mode(state_dir))}")


def main() -> int:
    failures = []
    test_ensure_private_dir_fresh(failures)
    test_ensure_private_dir_retroactive(failures)
    test_ensure_private_dir_empty_path_is_noop(failures)
    test_outcome_store_dir_is_private(failures)
    test_ledger_and_heartbeat_dirs_are_private(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — .unattended/ permission hardening not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — .unattended/ state directories are created (and retroactively "
          "tightened) at 0700; an empty path never touches the ambient CWD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
