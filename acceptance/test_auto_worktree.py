#!/usr/bin/env python3
"""G4b — auto-worktree isolation (launcher-owned lifecycle).

When `autonomy.live_tree: auto_worktree`, the launcher runs the whole session in a dedicated,
EXTERNAL `git worktree` so the primary/live tree is never touched. driver/vcs are unchanged (they
already run correctly in a linked worktree). These tests exercise the launcher's lifecycle seams
against a real temp git repo — no agent spawn needed.

Exit 0 = GREEN.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.launcher import (  # noqa: E402
    WorktreeError, ans_worktree_root, cleanup_isolated_worktree,
    create_isolated_worktree, should_isolate,
)


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _repo():
    repo = tempfile.mkdtemp(prefix="ue-g4b-")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "app.py"), "w") as fh:
        fh.write("print('hi')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_root_is_external_and_stable(failures):
    repo = _repo()
    root = ans_worktree_root(repo)
    rp = os.path.realpath(repo)
    if os.path.realpath(root).startswith(rp + os.sep):
        failures.append(f"[g4b] worktree root must be OUTSIDE the primary tree, got {root!r}")
    if ans_worktree_root(repo) != root:
        failures.append("[g4b] worktree root must be stable across calls")
    # repo-id scoped: a different path with the same basename must not collide
    other = tempfile.mkdtemp(prefix="ue-g4b-")
    os.makedirs(os.path.join(other, os.path.basename(repo)))
    twin = os.path.join(other, os.path.basename(repo))
    _git(twin, "init", "-q")
    if os.path.basename(ans_worktree_root(twin)) == os.path.basename(root):
        failures.append("[g4b] two repos sharing a basename must not share a worktree root")


def test_should_isolate(failures):
    repo = _repo()
    if not should_isolate({"autonomy": {"live_tree": "auto_worktree"}}, repo):
        failures.append("[g4b] should_isolate must be True for auto_worktree on a primary tree")
    if should_isolate({"autonomy": {"live_tree": "warn"}}, repo):
        failures.append("[g4b] should_isolate must be False when not auto_worktree")
    if should_isolate({}, repo):
        failures.append("[g4b] should_isolate must be False with no config")
    # already inside a linked worktree -> never nest
    wt = create_isolated_worktree(repo, "stampX")
    if should_isolate({"autonomy": {"live_tree": "auto_worktree"}}, wt):
        failures.append("[g4b] should_isolate must be False when the target is already a linked worktree")
    cleanup_isolated_worktree(repo, wt)


def test_create_is_linked_external_and_isolates(failures):
    repo = _repo()
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wt = create_isolated_worktree(repo, "stamp1")
    # it is a registered linked worktree at the external root
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    if os.path.realpath(wt) not in listing and wt not in listing:
        failures.append("[g4b] created worktree is not registered with git")
    if not os.path.realpath(wt).startswith(os.path.realpath(ans_worktree_root(repo))):
        failures.append("[g4b] worktree not created under the external root")
    # base is the primary HEAD
    if _git(wt, "rev-parse", "HEAD").stdout.strip() != head_before:
        failures.append("[g4b] worktree HEAD is not the primary HEAD")
    # isolation: commit in the worktree must not move the primary
    with open(os.path.join(wt, "isolated.txt"), "w") as fh:
        fh.write("only in the worktree\n")
    _git(wt, "checkout", "-q", "-b", "ans/run-stamp1")
    _git(wt, "add", "isolated.txt")
    _git(wt, "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "work")
    if _git(repo, "rev-parse", "HEAD").stdout.strip() != head_before:
        failures.append("[g4b] primary HEAD moved — isolation breached")
    if os.path.exists(os.path.join(repo, "isolated.txt")):
        failures.append("[g4b] worktree file leaked into the primary tree")
    cleanup_isolated_worktree(repo, wt)


def test_create_force_removes_a_leftover(failures):
    repo = _repo()
    first = create_isolated_worktree(repo, "old")
    # leave it "dirty" (untracked harness state) as a crash would
    os.makedirs(os.path.join(first, ".unattended", "state"), exist_ok=True)
    with open(os.path.join(first, ".unattended", "state", "heartbeat.json"), "w") as fh:
        fh.write("{}")
    second = create_isolated_worktree(repo, "new")  # must prune+force-remove the leftover, fresh
    if os.path.exists(first):
        failures.append("[g4b] a leftover worktree was not force-removed on the next create")
    if not os.path.isdir(second):
        failures.append("[g4b] fresh worktree not created")
    cleanup_isolated_worktree(repo, second)


def test_cleanup_removes_dirty_worktree_but_keeps_branch(failures):
    repo = _repo()
    wt = create_isolated_worktree(repo, "stamp2")
    # a run branch created in the worktree (as the driver would) must survive cleanup
    _git(wt, "checkout", "-q", "-b", "ans/run-stamp2")
    with open(os.path.join(wt, "x.txt"), "w") as fh:
        fh.write("work\n")
    _git(wt, "add", "x.txt")
    _git(wt, "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-qm", "run work")
    # dirty untracked state (the #1 leak source: plain `remove` would refuse)
    os.makedirs(os.path.join(wt, ".unattended"), exist_ok=True)
    with open(os.path.join(wt, ".unattended", "sentinel"), "w") as fh:
        fh.write("x")
    removed = cleanup_isolated_worktree(repo, wt)
    if not removed or os.path.exists(wt):
        failures.append("[g4b] cleanup did not force-remove a dirty worktree")
    if "ans/run-stamp2" not in _git(repo, "branch", "--list", "ans/run-stamp2").stdout:
        failures.append("[g4b] cleanup destroyed the run branch — the night's work is gone")


def test_report_path_honors_env(failures):
    """G4b: an isolated run must write its morning report to the PRIMARY tree, not the (soon-removed)
    worktree. run.py resolves the report path from UE_REPORT_PATH first (set by the launcher when
    isolating), exactly like UE_HEARTBEAT — so the deliverable survives worktree cleanup."""
    from agents_never_sleep.run import _resolve_report_path
    # env unset -> repo-relative (unchanged default)
    os.environ.pop("UE_REPORT_PATH", None)
    got = _resolve_report_path("/repo", {}, "night-report.md")
    if got != os.path.join("/repo", "night-report.md"):
        failures.append(f"[g4b] default report path wrong: {got!r}")
    # env set -> absolute primary path wins
    os.environ["UE_REPORT_PATH"] = "/primary/night-report.md"
    try:
        got = _resolve_report_path("/worktree", {"report": {"local_path": "x.md"}}, "night-report.md")
        if got != "/primary/night-report.md":
            failures.append(f"[g4b] UE_REPORT_PATH not honored: {got!r}")
    finally:
        os.environ.pop("UE_REPORT_PATH", None)


def test_create_on_non_repo_raises(failures):
    junk = tempfile.mkdtemp(prefix="ue-g4b-nonrepo-")
    try:
        wt = create_isolated_worktree(junk, "stamp")
        failures.append(f"[g4b] create on a non-repo must raise, returned {wt!r}")
    except WorktreeError:
        pass


def main() -> int:
    failures = []
    test_root_is_external_and_stable(failures)
    test_should_isolate(failures)
    test_create_is_linked_external_and_isolates(failures)
    test_create_force_removes_a_leftover(failures)
    test_cleanup_removes_dirty_worktree_but_keeps_branch(failures)
    test_report_path_honors_env(failures)
    test_create_on_non_repo_raises(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — auto-worktree lifecycle not implemented")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — external, isolated, fresh-each-run worktree; cleanup keeps the branch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
