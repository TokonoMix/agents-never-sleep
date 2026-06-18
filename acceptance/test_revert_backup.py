#!/usr/bin/env python3
"""INT-1825 bug 3 — revert_to must NEVER destroy WIP irrecoverably.

`Git.revert_to` does `git reset --hard` + `git clean -fd`, which obliterates any uncommitted
tracked edit AND any untracked file in the shared working tree (a run-branch does NOT protect the
tree — `reset --hard` resets the tree regardless of branch). During the S2 run this reset reverted
a live working edit = real data loss.

The fix: before the destructive reset, anchor a RECOVERABLE backup commit capturing tracked
modifications *and* untracked files into a durable ref, so a wrong/unexpected revert is always
recoverable from git (reflog/ref) the next morning. A clean tree needs no backup.

Exit 0 = GREEN.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness.vcs import Git  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _new_repo():
    repo = tempfile.mkdtemp(prefix="ue-revertbk-")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("committed\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_backup_captures_tracked_and_untracked_before_reset(failures):
    repo = _new_repo()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Simulate live, uncommitted work the operator wants to keep.
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("LIVE EDIT — must be recoverable\n")
    with open(os.path.join(repo, "untracked.txt"), "w") as fh:
        fh.write("new untracked content\n")

    backup = Git(repo).revert_to(head)

    # The revert itself must still have happened (that is its job).
    if _git(repo, "rev-parse", "HEAD").stdout.strip() != head:
        failures.append("[bug3] revert_to did not reset HEAD to the snapshot")
    cur = open(os.path.join(repo, "tracked.txt"), encoding="utf-8").read()
    if cur != "committed\n":
        failures.append(f"[bug3] working tree not reverted (tracked.txt={cur!r})")
    if os.path.exists(os.path.join(repo, "untracked.txt")):
        failures.append("[bug3] untracked file not cleaned by revert")

    # ...but the destroyed WIP must be RECOVERABLE from the returned backup ref/sha.
    if not backup:
        failures.append("[bug3] revert_to returned no backup ref for a DIRTY tree — WIP is lost")
        return
    r1 = _git(repo, "show", f"{backup}:tracked.txt")
    if r1.returncode != 0 or r1.stdout != "LIVE EDIT — must be recoverable\n":
        failures.append(f"[bug3] tracked live edit not recoverable from backup (got {r1.stdout!r})")
    r2 = _git(repo, "show", f"{backup}:untracked.txt")
    if r2.returncode != 0 or r2.stdout != "new untracked content\n":
        failures.append(f"[bug3] untracked file not recoverable from backup (got {r2.stdout!r})")


def test_clean_tree_needs_no_backup(failures):
    repo = _new_repo()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    backup = Git(repo).revert_to(head)  # nothing dirty
    if backup:
        failures.append(f"[bug3] clean-tree revert created a needless backup ref ({backup})")


def test_backup_ref_is_anchored_not_dangling(failures):
    """A bare `git stash create` commit is reapable by gc. The backup must be pinned under a ref so
    it survives until daylight recovery."""
    repo = _new_repo()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("dirty\n")
    backup = Git(repo).revert_to(head)
    refs = _git(repo, "for-each-ref", "--format=%(objectname)").stdout.split()
    if backup and backup not in refs:
        failures.append("[bug3] backup commit is not anchored under any ref (gc could reap it)")


def test_backup_failure_on_dirty_tree_aborts_reset(failures):
    """Code-review finding: if the tree is DIRTY but the backup cannot be created/anchored (git fails
    for an environmental reason — full disk, lock, RO object store), revert_to must RAISE and must
    NOT proceed to the destructive reset. Silently returning None and resetting = data loss with no
    backup, the exact bug being fixed."""
    from harness.vcs import GitError
    repo = _new_repo()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(repo, "tracked.txt"), "w") as fh:
        fh.write("LIVE EDIT — must survive a failed backup\n")
    with open(os.path.join(repo, "untracked.txt"), "w") as fh:
        fh.write("untracked — must survive\n")
    objects = os.path.join(repo, ".git", "objects")
    subprocess.run(["chmod", "-R", "555", objects])   # block object writes => backup cannot be built
    raised = False
    try:
        Git(repo).revert_to(head)
    except GitError:
        raised = True
    finally:
        subprocess.run(["chmod", "-R", "755", objects])
    if not raised:
        failures.append("[bug3] revert_to did NOT raise when the dirty-tree backup failed")
    cur = open(os.path.join(repo, "tracked.txt"), encoding="utf-8").read()
    if cur != "LIVE EDIT — must survive a failed backup\n":
        failures.append(f"[bug3] WIP destroyed despite a failed backup (tracked.txt={cur!r})")
    if not os.path.exists(os.path.join(repo, "untracked.txt")):
        failures.append("[bug3] untracked WIP destroyed despite a failed backup")


def main() -> int:
    failures = []
    test_backup_captures_tracked_and_untracked_before_reset(failures)
    test_clean_tree_needs_no_backup(failures)
    test_backup_ref_is_anchored_not_dangling(failures)
    test_backup_failure_on_dirty_tree_aborts_reset(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — revert_to can still lose WIP irrecoverably")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — revert_to backs up tracked+untracked to an anchored ref before reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
