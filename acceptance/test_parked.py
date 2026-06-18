#!/usr/bin/env python3
"""INT-1735 — parked-WIP protection helper.

Each unattended run the agent has intentional working-tree WIP it must NOT let the harness's
`git add -A` snapshot commit. This proves a helper that, BEFORE the run, snapshots a configured
list of tracked parked paths to a labelled stash and fences a list of untracked-throwaway globs
into `.git/info/exclude`; and AFTER a terminal signal restores them (stash pop + de-fence). It
must be IDEMPOTENT (double-protect is a no-op) and SAFE ON RESUME (a marker survives so a second
`next` does not re-stash, and restore is a no-op when nothing is protected).

Exit 0 = GREEN.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness.parked import ParkedGuard  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _new_repo():
    repo = tempfile.mkdtemp(prefix="ue-parked-")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("committed\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def _read(repo, name):
    with open(os.path.join(repo, name), encoding="utf-8") as fh:
        return fh.read()


def _guard(repo, state_dir):
    return ParkedGuard(repo, state_dir,
                       tracked_paths=["tracked.txt"],
                       throwaway_globs=["scratch_*", "_tmp/"],
                       label="ans-parked-test")


def test_protect_stashes_tracked_wip(failures):
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    # Mutate the tracked parked file (the intentional WIP).
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("LOCAL WIP — must not be committed\n")
    g = _guard(repo, state)
    g.protect()
    # After protect, the working tree is back to HEAD (WIP is safely stashed away).
    if _read(repo, "tracked.txt") != "committed\n":
        failures.append("[protect] tracked WIP was not stashed away (still in working tree)")
    if not g.is_active():
        failures.append("[protect] guard not marked active after protect()")
    # A harness `git add -A` + commit now must NOT capture the WIP.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "harness snapshot")
    show = _git(repo, "show", "HEAD:tracked.txt").stdout
    if "LOCAL WIP" in show:
        failures.append("[protect] WIP leaked into a harness commit despite protection")
    # Restore brings the WIP back.
    g.restore()
    if "LOCAL WIP" not in _read(repo, "tracked.txt"):
        failures.append("[restore] parked WIP was not restored after the run")
    if g.is_active():
        failures.append("[restore] guard still active after restore()")


def test_throwaway_globs_fenced_in_exclude(failures):
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    g = _guard(repo, state)
    g.protect()
    excl = _read(repo, ".git/info/exclude")
    if "scratch_*" not in excl or "_tmp/" not in excl:
        failures.append(f"[exclude] throwaway globs not fenced into .git/info/exclude: {excl!r}")
    # An untracked throwaway file is now ignored by add -A.
    with open(os.path.join(repo, "scratch_x.json"), "w") as fh:
        fh.write("{}")
    porcelain = _git(repo, "status", "--porcelain").stdout
    if "scratch_x.json" in porcelain:
        failures.append("[exclude] throwaway file still visible to git despite exclude fence")
    g.restore()
    excl2 = _read(repo, ".git/info/exclude")
    if "scratch_*" in excl2 or "ans-parked-test" in excl2:
        failures.append(f"[exclude] fence not removed from exclude on restore: {excl2!r}")


def test_idempotent_and_resume_safe(failures):
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("WIP-A\n")
    g = _guard(repo, state)
    g.protect()
    # A SECOND guard instance (simulating a fresh `next` after context compaction / resume) must
    # see the marker and NOT re-stash an already-clean tree (which would stash nothing / double up).
    g2 = _guard(repo, state)
    before = _git(repo, "stash", "list").stdout.count("\n")
    g2.protect()  # idempotent no-op
    after = _git(repo, "stash", "list").stdout.count("\n")
    if after != before:
        failures.append(f"[idempotent] second protect() changed the stash ({before}->{after})")
    if not g2.is_active():
        failures.append("[resume] resumed guard does not see active protection marker")
    g2.restore()
    if _read(repo, "tracked.txt") != "WIP-A\n":
        failures.append("[resume] WIP not correctly restored by the resumed guard")
    # restore() again is a safe no-op (no marker, no stash).
    g2.restore()


def test_stash_pinned_by_sha_not_label(failures):
    """Council finding (INT-1735): identifying our stash by message-substring can pop the WRONG
    stash when another stash exists (shared repo / manual `git stash`). We must pin by commit SHA."""
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("PARKED WIP\n")
    g = _guard(repo, state)
    g.protect()  # our parked stash created
    # A FOREIGN stash lands on top (becomes stash@{0}); ours is now stash@{1}.
    with open(os.path.join(repo, "foreign.txt"), "w") as fh:
        fh.write("unrelated change\n")
    _git(repo, "add", "foreign.txt")
    _git(repo, "stash", "push", "-m", "some-other-tool work")
    stash_count_before = _git(repo, "stash", "list").stdout.count("\n")
    g.restore()
    # Our WIP must come back...
    if "PARKED WIP" not in _read(repo, "tracked.txt"):
        failures.append("[sha] our parked WIP not restored — popped the wrong stash?")
    # ...and the FOREIGN stash must remain untouched (exactly one stash consumed: ours).
    stash_count_after = _git(repo, "stash", "list").stdout.count("\n")
    if stash_count_after != stash_count_before - 1:
        failures.append(f"[sha] wrong number of stashes consumed ({stash_count_before}->"
                        f"{stash_count_after}); foreign stash may have been popped")
    if "some-other-tool work" not in _git(repo, "stash", "list").stdout:
        failures.append("[sha] foreign stash was popped instead of ours (label-match bug)")


def test_restore_noop_when_never_protected(failures):
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    g = _guard(repo, state)
    # restore without protect must not raise and must not touch the tree.
    g.restore()
    if g.is_active():
        failures.append("[noop] guard active without ever protecting")


def test_disabled_guard_is_inert(failures):
    repo = _new_repo()
    state = tempfile.mkdtemp(prefix="ue-parked-st-")
    g = ParkedGuard(repo, state, tracked_paths=[], throwaway_globs=[], label="x")
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("WIP\n")
    g.protect()  # nothing configured -> inert
    if _read(repo, "tracked.txt") != "WIP\n":
        failures.append("[disabled] empty-config guard touched the working tree")
    if g.is_active():
        failures.append("[disabled] empty-config guard marked itself active")
    g.restore()


def main() -> int:
    failures = []
    test_protect_stashes_tracked_wip(failures)
    test_throwaway_globs_fenced_in_exclude(failures)
    test_idempotent_and_resume_safe(failures)
    test_stash_pinned_by_sha_not_label(failures)
    test_restore_noop_when_never_protected(failures)
    test_disabled_guard_is_inert(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — parked-WIP guard incomplete")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — protect/stash, exclude-fence, idempotent resume, "
          "restore, and inert-when-disabled all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
