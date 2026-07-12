"""Git-based reversibility substrate.

The whole "assume + continue" autonomy rests on being able to cheaply undo. Thread 6: the
agent MEASURES whether a VCS exists and, if not, tries to establish a safety net before doing
risky work. Here we wrap the minimum git operations the Slice-1 loop needs: detect a clean
repo, snapshot before an edit, revert on a hard-block, commit to keep a good change.

Slice-2 hardening (branch-per-ticket, idempotency keys) layers on top of this.
"""
from __future__ import annotations

import os
import subprocess
import time


class GitError(Exception):
    """A git operation could not complete (timeout / git binary missing / hung lock). Raised by the
    snapshot/commit/revert ops so the orchestrator can map it to a BLOCKED_ENV outcome instead of
    letting it crash the whole overnight run with an unrecorded ticket."""


class Git:
    def __init__(self, cwd: str, timeout: int = 60, protect: list | None = None):
        self.cwd = cwd
        self.timeout = timeout
        # Paths the harness owns (its own state/artifacts/sentinel). They must NEVER be committed
        # into a snapshot or deleted by a revert's `git clean` — otherwise reverting a bad ticket
        # would wipe the durable run state and the failing-diff artifacts. Default protects the
        # conventional home; run.py threads the actual configured dirs.
        self.protect = [p.rstrip("/") for p in (protect or [".unattended"]) if p and p != "."]

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args], cwd=self.cwd, capture_output=True, text=True,
                timeout=self.timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {' '.join(args)}: {type(exc).__name__}") from exc

    def is_repo(self) -> bool:
        try:
            r = self._run("rev-parse", "--is-inside-work-tree")
            return r.returncode == 0 and r.stdout.strip() == "true"
        except GitError:
            return False

    def is_clean(self, *, exclude=()) -> bool:
        """True if the working tree has no changes. `exclude` drops any porcelain entry UNDER the
        given paths (e.g. the harness's own `.unattended/` bookkeeping, which is untracked until
        ensure_safety_net gitignores it) so a caller measuring HUMAN dirt is not fooled by harness
        artifacts. Without `exclude` this is the plain 'porcelain empty' check (unchanged)."""
        r = self._run("status", "--porcelain")
        if r.returncode != 0:
            return False
        if not exclude:
            return r.stdout.strip() == ""
        excl = tuple(e.rstrip("/") for e in exclude if e)
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip()                    # porcelain: 2 status chars + space, then path
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            if " -> " in path:                         # rename: "old -> new"
                path = path.split(" -> ", 1)[1]
            p = path.rstrip("/")
            if any(p == e or p.startswith(e + "/") for e in excl):
                continue                               # under an excluded (harness-owned) path
            return False                               # a real, non-excluded change
        return True

    def _ensure_gitignore(self) -> None:
        """Make sure the harness's own dirs are gitignored, so `git add -A` never tracks them
        (and therefore a snapshot never carries run state, and reset --hard never reverts it).

        Raises GitError (never a raw OSError) when .gitignore cannot be read/appended — e.g. a
        read-only working tree. Callers already route GitError to their degrade paths
        (ensure_safety_net -> no net -> clean HALT; commit_all -> BLOCKED_ENV); a raw
        PermissionError here crashed `next` with a traceback instead (2026-07-08 E2E, 2.1)."""
        if not self.protect:
            return
        gi = os.path.join(self.cwd, ".gitignore")
        try:
            existing = ""
            if os.path.exists(gi):
                with open(gi, "r", encoding="utf-8") as fh:
                    existing = fh.read()
            lines = set(existing.splitlines())
            want = [f"{p}/" for p in self.protect]
            missing = [w for w in want if w not in lines]
            if not missing:
                return
            with open(gi, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write("# agents-never-sleep harness bookkeeping (do not commit)\n")
                for w in missing:
                    fh.write(w + "\n")
        except OSError as exc:
            raise GitError(f"cannot update .gitignore to protect harness dirs "
                           f"({type(exc).__name__}: {exc})") from exc

    def ensure_safety_net(self) -> bool:
        """Return True if a reversibility safety net exists or could be created.

        If there is no repo we try `git init` + an initial commit so later edits are
        revertible. If even that fails (e.g. read-only fs) we return False and the caller
        must HALT or restrict to non-destructive work.
        """
        if self.is_repo():
            try:
                self._ensure_gitignore()
            except GitError:
                # The protection cannot be established (typically a read-only working tree, where
                # snapshot/commit/revert cannot work either): without it a snapshot would sweep the
                # harness's own state dirs into commits and a revert would wipe them. Report "no
                # safety net" so classify HALTs destructive work cleanly instead of proceeding on a
                # net that is not actually there.
                return False
            return True
        try:
            if self._run("init").returncode != 0:
                return False
            self._ensure_gitignore()
            self._run("add", "-A")
            self._run("-c", "user.email=agent@local", "-c", "user.name=unattended",
                      "commit", "-m", "agents-never-sleep: safety-net baseline",
                      "--allow-empty")
            return self.is_repo()
        except GitError:
            return False

    def head(self) -> str:
        r = self._run("rev-parse", "HEAD")
        return r.stdout.strip()

    def current_ref(self) -> str:
        """The checked-out branch name, or the commit SHA when HEAD is detached (so it can be
        checked back out later)."""
        r = self._run("rev-parse", "--abbrev-ref", "HEAD")
        name = r.stdout.strip()
        if name and name != "HEAD":
            return name
        return self._run("rev-parse", "HEAD").stdout.strip()

    def create_run_branch(self, name: str) -> None:
        """Create + switch to a new branch (carrying any uncommitted WIP onto it). Raises GitError on
        a git failure so the caller can degrade rather than silently committing onto the wrong branch."""
        r = self._run("checkout", "-b", name)
        if r.returncode != 0:
            raise GitError(f"git checkout -b {name}: {r.stderr.strip()}")

    def checkout(self, ref: str) -> None:
        r = self._run("checkout", ref)
        if r.returncode != 0:
            raise GitError(f"git checkout {ref}: {r.stderr.strip()}")

    def is_linked_worktree(self) -> bool:
        """True if this working tree is a LINKED `git worktree` — its per-worktree git dir differs
        from the shared common dir. False for the primary worktree. Any git error → False (assume
        primary, the conservative choice for the isolation gate: better to warn than miss it)."""
        gd = self._run("rev-parse", "--git-dir")
        cd = self._run("rev-parse", "--git-common-dir")
        if gd.returncode != 0 or cd.returncode != 0:
            return False

        def _abs(p: str) -> str:
            p = p.strip()
            return p if os.path.isabs(p) else os.path.join(self.cwd, p)
        return os.path.realpath(_abs(gd.stdout)) != os.path.realpath(_abs(cd.stdout))

    def branch_exists(self, name: str) -> bool:
        """True if a local branch `name` currently exists. Non-zero rc (absent/deleted) → False."""
        return self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0

    def object_exists(self, ref: str) -> bool:
        """True if `ref` resolves to a present commit object. False when it was gc'd / never existed —
        used so an unresolvable recorded base is treated as stale rather than silently trusted."""
        return self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True iff `ancestor` is an ancestor of `descendant` (a commit is its own ancestor). A
        non-ancestor OR any resolve error yields False — the SAFE direction here: an unverifiable
        lineage is treated as 'not an ancestor' so the caller HALTs rather than resuming blindly.
        (`--is-ancestor` never throws on a normal false; only a missing/hung git binary raises.)"""
        return self._run("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def list_backup_refs(self) -> list:
        """Enumerate durable pre-revert WIP backups (`refs/ans-backup/*`) so the morning report can
        point an operator at recoverable working-tree state a `revert_to` set aside. Read-only,
        best-effort: any git failure yields [] (the report simply omits the section)."""
        r = self._run("for-each-ref", "--format=%(refname) %(objectname:short)", "refs/ans-backup/")
        if r.returncode != 0:
            return []
        out = []
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if len(parts) == 2:
                out.append((parts[0], parts[1]))
        return out

    def commit_all(self, message: str) -> str:
        self._ensure_gitignore()
        self._run("add", "-A")
        self._run("-c", "user.email=agent@local", "-c", "user.name=unattended",
                  "commit", "-m", message, "--allow-empty")
        return self.head()

    def diff_files(self, ref: str, *, cap: int = 20000) -> tuple[list, str]:
        """Changed files (tracked + new untracked) and a capped unified diff vs `ref`, for council
        risk-routing. Best-effort: any git failure yields ([], "") so routing simply can't escalate
        rather than crashing the run."""
        try:
            names = self._run("diff", "--name-only", ref)
            untracked = self._run("ls-files", "--others", "--exclude-standard")
            text = self._run("diff", ref)
        except GitError:
            return [], ""
        files = set()
        for out in (names.stdout, untracked.stdout):
            files.update(ln.strip() for ln in out.splitlines() if ln.strip())
        return sorted(files), (text.stdout or "")[:cap]

    def _backup_wip(self) -> str | None:
        """Anchor a RECOVERABLE backup of the current working tree (tracked modifications AND
        untracked files) into a durable ref BEFORE any destructive reset, then return its commit
        SHA. Returns None when the tree is clean (nothing to lose).

        The backup is built in a TEMP index (GIT_INDEX_FILE) seeded from HEAD, so the real index
        and working tree are never touched; `commit-tree` makes a commit and `update-ref` pins it
        under `refs/ans-backup/<ts>` so gc cannot reap it before daylight recovery (INT-1825 bug 3:
        `git reset --hard` + `git clean -fd` would otherwise obliterate live WIP irrecoverably,
        and a run-branch does NOT protect the shared working tree)."""
        status = self._run("status", "--porcelain")
        if status.returncode != 0 or not status.stdout.strip():
            return None  # clean tree — nothing to back up
        # Past this point the tree is DIRTY: ANY failure to build/anchor the backup must RAISE so
        # revert_to aborts the destructive reset rather than discarding WIP with no backup (code-
        # review: a git step that exits non-zero — full disk, ref lock, RO object store — does not
        # throw on its own, so we must check returncodes explicitly and never fall through to reset).
        head = self._run("rev-parse", "HEAD")
        parent = head.stdout.strip() if head.returncode == 0 else ""
        # Resolve the real git dir so this works in a linked worktree, where `<cwd>/.git` is a FILE
        # (a `gitdir:` pointer), not a directory. `--git-dir` returns the per-worktree, writable dir
        # (main/.git/worktrees/<name>), avoiding cross-worktree temp-index collisions; in a normal
        # repo it is just `.git`, so this is behaviour-identical for main-tree runs.
        gd = self._run("rev-parse", "--git-dir")
        git_dir = gd.stdout.strip() if gd.returncode == 0 and gd.stdout.strip() else ".git"
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(self.cwd, git_dir)
        tmp_index = os.path.join(git_dir, f"ans-backup-index.{os.getpid()}.{time.time_ns()}")
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)

        def _g(*args) -> str:
            try:
                r = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True,
                                   text=True, timeout=self.timeout, env=env)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise GitError(f"git {' '.join(args)}: {type(exc).__name__}") from exc
            if r.returncode != 0:
                raise GitError(f"WIP backup step `git {args[0]}` failed (rc={r.returncode}): "
                               f"{(r.stderr or r.stdout).strip()} — aborting reset to preserve WIP")
            return r.stdout.strip()

        try:
            if parent:
                _g("read-tree", parent)        # seed temp index from HEAD
            _g("add", "-A")                    # stage tracked + untracked into temp index
            tree = _g("write-tree")
            if not tree:
                raise GitError("WIP backup produced no tree object — aborting reset to preserve WIP")
            # commit-tree needs an author/committer identity; unlike commit_all() and
            # ensure_safety_net() this path lacked the inline `-c` flags, so in an
            # identity-less env (cron / CI / claude-run) the backup commit failed →
            # GitError → the whole reset aborted on BLOCKED_ENV. Inject the same
            # identity (only commit-tree needs it; harmless on the tree ops above).
            commit_args = ["-c", "user.email=agent@local", "-c", "user.name=unattended",
                           "commit-tree"]
            if parent:
                commit_args += ["-p", parent]
            commit_args += [tree, "-m", "ans-backup: pre-revert WIP (INT-1825)"]
            sha = _g(*commit_args)
        finally:
            try:
                os.remove(tmp_index)
            except OSError:
                pass
        if not sha:
            raise GitError("WIP backup produced no commit — aborting reset to preserve WIP")
        ref = f"refs/ans-backup/{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        r = self._run("update-ref", ref, sha)
        if r.returncode != 0:
            # The commit exists but isn't anchored under a ref => gc could reap it before recovery.
            raise GitError(f"could not anchor WIP backup ref {ref} ({r.stderr.strip()}) — "
                           "aborting reset to preserve WIP")
        return sha

    def revert_to(self, ref: str) -> str | None:
        """Discard all working-tree changes back to `ref` (revert-to-last-green) WITHOUT touching
        the harness's own dirs: reset only affects tracked files (the protected dirs are gitignored,
        so untracked), and `git clean` is told to leave them alone.

        Returns the SHA of a recoverable backup of the pre-revert WIP (or None if the tree was
        clean): the reset+clean below would otherwise destroy uncommitted tracked edits and
        untracked files irrecoverably (INT-1825 bug 3)."""
        backup = self._backup_wip()
        self._run("reset", "--hard", ref)
        clean = ["clean", "-fd"]
        for p in self.protect:
            clean += ["-e", p]
        self._run(*clean)
        return backup
