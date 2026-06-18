"""Parked-WIP protection (INT-1735).

An unattended run usually has intentional working-tree WIP the operator does NOT want the harness
to commit: the harness snapshots with `git add -A` (see `vcs.commit_all`), which would sweep up
those parked edits. This guard, run BEFORE the loop, moves them out of harm's way and, AFTER a
terminal signal (DRAINED/HALTED/...), restores them:

  - configured TRACKED parked paths  -> a single labelled `git stash` (reverts them to HEAD for the
    duration of the run, so no `git add -A` can capture them);
  - configured untracked THROWAWAY globs -> fenced into `.git/info/exclude` (so `git add -A` and the
    revert's `git clean` both ignore them).

It is IDEMPOTENT and RESUME-SAFE: a marker file under the run state-dir records that protection is
active, so a second `protect()` (e.g. a fresh `next` after context compaction) is a no-op rather
than a double-stash, and `restore()` is a no-op when nothing is protected. The whole thing is
default-disabled (empty config = inert): nothing changes for runs that don't opt in.

This is a *helper* the operator/agent can also drive standalone (`harness.run parked protect|
restore`). It deliberately never deletes WIP: a failed `stash pop` leaves the marker + stash in
place for daylight recovery rather than dropping the change.
"""
from __future__ import annotations

import json
import os
import subprocess


class ParkedGuard:
    def __init__(self, repo: str, state_dir: str, *, tracked_paths=None, throwaway_globs=None,
                 label: str = "agents-never-sleep-parked", timeout: int = 60):
        self.repo = repo
        self.state_dir = state_dir
        self.tracked = [p for p in (tracked_paths or []) if p]
        self.globs = [g for g in (throwaway_globs or []) if g]
        self.label = label
        self.timeout = timeout
        self.marker = os.path.join(state_dir, "parked-guard.json")
        self._fence_begin = f"# >>> {label} (agents-never-sleep parked WIP — do not commit) >>>"
        self._fence_end = f"# <<< {label} <<<"

    # ---- internals -------------------------------------------------------------------------
    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              timeout=self.timeout)

    def _configured(self) -> bool:
        return bool(self.tracked or self.globs)

    def _exclude_path(self) -> str:
        return os.path.join(self.repo, ".git", "info", "exclude")

    def _add_fence(self) -> None:
        path = self._exclude_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if self._fence_begin in existing:        # idempotent
            return
        with open(path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(self._fence_begin + "\n")
            for g in self.globs:
                fh.write(g + "\n")
            fh.write(self._fence_end + "\n")

    def _remove_fence(self) -> None:
        path = self._exclude_path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        out, skipping = [], False
        for ln in lines:
            if ln == self._fence_begin:
                skipping = True
                continue
            if ln == self._fence_end:
                skipping = False
                continue
            if not skipping:
                out.append(ln)
        with open(path, "w", encoding="utf-8") as fh:
            if out:
                fh.write("\n".join(out) + "\n")

    def _find_stash_ref(self, sha: str | None) -> str | None:
        """Locate our stash by its COMMIT SHA (pinned at protect-time), never by message substring:
        a shared repo or a manual `git stash` can leave a same-/similar-labelled entry, and popping
        the wrong one loses WIP (council finding). Falls back to a label match only when no SHA was
        recorded (legacy markers)."""
        if sha:
            r = self._git("stash", "list", "--format=%gd %H")
            for ln in r.stdout.splitlines():
                ref, _, h = ln.partition(" ")
                if h.strip() == sha:
                    return ref.strip()
            return None
        r = self._git("stash", "list")
        for ln in r.stdout.splitlines():
            ref, _, rest = ln.partition(":")
            if self.label in rest:
                return ref.strip()
        return None

    # ---- public API ------------------------------------------------------------------------
    def is_active(self) -> bool:
        return os.path.exists(self.marker)

    def protect(self) -> dict:
        """Move parked WIP out of harm's way. No-op if not configured or already active."""
        if not self._configured() or self.is_active():
            return {"protected": False, "active": self.is_active()}
        stashed = False
        stash_sha = None
        if self.tracked:
            r = self._git("stash", "push", "-m", self.label, "--", *self.tracked)
            blob = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "No local changes" not in blob:
                # Pin the just-created stash by its commit SHA (always stash@{0} immediately after a
                # successful push), so restore pops exactly ours even if other stashes pile on top.
                sha = self._git("rev-parse", "stash@{0}").stdout.strip()
                stashed = bool(sha)
                stash_sha = sha or None
        if self.globs:
            self._add_fence()
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"label": self.label, "stashed": stashed, "stash_sha": stash_sha,
                       "globs": self.globs}, fh)
        os.replace(tmp, self.marker)             # atomic
        return {"protected": True, "stashed": stashed, "fenced": bool(self.globs)}

    def restore(self) -> dict:
        """Restore parked WIP after a terminal signal. No-op if nothing is protected. On a stash-pop
        failure (e.g. a conflict), the marker + stash are LEFT IN PLACE for daylight recovery."""
        if not self.is_active():
            return {"restored": False}
        try:
            with open(self.marker, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {"stashed": bool(self.tracked), "globs": self.globs}
        anomaly = None
        if data.get("stashed"):
            ref = self._find_stash_ref(data.get("stash_sha"))
            if ref is not None:
                r = self._git("stash", "pop", ref)
                if r.returncode != 0:
                    # Conflict / dirty pop: leave the stash AND marker in place for daylight recovery
                    # rather than dropping the WIP.
                    return {"restored": False, "error": "stash pop failed; marker kept for recovery"}
            else:
                # The pinned stash is gone (already popped, cleared, or lost). Don't claim success —
                # surface it — but still de-fence + clear the marker so the run isn't wedged.
                anomaly = (f"expected parked stash {data.get('stash_sha')!r} not found; WIP may be "
                           "already applied or lost — verify the working tree")
        if data.get("globs"):
            self._remove_fence()
        os.remove(self.marker)
        out = {"restored": anomaly is None}
        if anomaly:
            out["anomaly"] = anomaly
        return out


def guard_from_config(config: dict, repo: str, state_dir: str) -> ParkedGuard | None:
    """Build a guard from the `autonomy.parked` config block, or None when disabled/absent."""
    cfg = (config.get("autonomy", {}) or {}).get("parked", {}) or {}
    if not cfg.get("enabled"):
        return None
    return ParkedGuard(repo, state_dir,
                       tracked_paths=cfg.get("tracked_paths") or [],
                       throwaway_globs=cfg.get("throwaway_globs") or [],
                       label=cfg.get("label") or "agents-never-sleep-parked")
