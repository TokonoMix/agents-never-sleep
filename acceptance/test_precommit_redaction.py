#!/usr/bin/env python3
"""Pre-commit redaction hook — proves the LOCAL git hook actually blocks a leak at commit time.

CI already runs `scripts/redact_lint.sh` on push (surface-parity workflow). The local pre-commit
hook is the earlier catch: a forbidden token must be stopped BEFORE it enters local history, not
only after push. This test exercises the real installer + real hook + real lint end-to-end in a
throwaway git repo:

  1. INSTALL:      `scripts/install-git-hooks.sh` wires .git/hooks/pre-commit (idempotent).
  2. BLOCK:        staging a file with an internal token -> `git commit` FAILS, nothing lands.
  3. PASS:         staging a clean file -> `git commit` SUCCEEDS.
  4. IDEMPOTENT:   re-running the installer is a no-op success.

The forbidden token is assembled from fragments at runtime, so THIS test file never contains the
literal pattern — the real repo's redact_lint.sh therefore does not need a fixture exclusion for
it (the same discipline the existing token fixtures document).

Exit 0 = GREEN. Skips (exit 0) if `git` is unavailable.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)

# Forbidden token built from fragments so the literal never appears in this source file.
# At runtime this is an internal server path that redact_lint.sh flags as "internal-server-path".
FORBIDDEN = "/home/" + "claude"


def _run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git_env():
    """Hermetic git env: no global/system config (so a stray core.hooksPath can't interfere),
    no gpg signing, and the unattended-run vars scrubbed (mirrors acceptance/run_all.sh)."""
    env = dict(os.environ)
    for k in ("CLAUDE_UNATTENDED", "UE_RUN_INCOMPLETE", "UE_HEARTBEAT",
              "UE_SESSION_TICKET_BUDGET", "UE_SESSION_BUDGET_MARKER"):
        env.pop(k, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
    return env


def _setup_repo(work, env):
    """A fresh repo carrying the three real scripts, with the scripts committed (unhooked)."""
    _run(["git", "init", "-q", "-b", "main"], work, env)
    _run(["git", "config", "commit.gpgsign", "false"], work, env)
    scripts = os.path.join(work, "scripts")
    os.makedirs(os.path.join(scripts, "hooks"), exist_ok=True)
    for rel in ("scripts/redact_lint.sh", "scripts/hooks/pre-commit", "scripts/install-git-hooks.sh"):
        dst = os.path.join(work, rel)
        shutil.copy2(os.path.join(SKILL_ROOT, rel), dst)
        os.chmod(dst, 0o755)
    # Commit the scripts WITHOUT the hook (it isn't installed yet) so setup is never gated.
    _run(["git", "add", "scripts"], work, env)
    _run(["git", "commit", "-q", "-m", "seed scripts"], work, env)


def test_hook_blocks_and_passes(failures):
    if shutil.which("git") is None:
        print("SKIP: git not on PATH")
        return
    work = tempfile.mkdtemp(prefix="ans-precommit-")
    try:
        env = _git_env()
        _setup_repo(work, env)

        # 1. INSTALL
        r = _run(["bash", "scripts/install-git-hooks.sh"], work, env)
        if r.returncode != 0:
            failures.append(f"[install] installer failed: {r.returncode}\n{r.stderr}")
            return
        hookpath = os.path.join(work, ".git", "hooks", "pre-commit")
        if not os.path.islink(hookpath) and not os.path.isfile(hookpath):
            failures.append("[install] .git/hooks/pre-commit was not created")
            return

        # 2. BLOCK: a staged internal token must abort the commit.
        with open(os.path.join(work, "leak.txt"), "w") as fh:
            fh.write(f"config path = {FORBIDDEN}/project\n")
        _run(["git", "add", "leak.txt"], work, env)
        r = _run(["git", "commit", "-m", "should be blocked"], work, env)
        if r.returncode == 0:
            failures.append("[block] commit with an internal token SUCCEEDED — hook did not block")
        # the leaking commit must not exist
        log = _run(["git", "log", "--oneline"], work, env).stdout
        if "should be blocked" in log:
            failures.append("[block] the blocked commit landed in history anyway")

        # clear the staged leak before the clean-commit test
        _run(["git", "rm", "-f", "leak.txt"], work, env)

        # 3. PASS: a clean file must commit fine.
        with open(os.path.join(work, "clean.txt"), "w") as fh:
            fh.write("just an ordinary line, nothing internal here\n")
        _run(["git", "add", "clean.txt"], work, env)
        r = _run(["git", "commit", "-m", "clean change"], work, env)
        if r.returncode != 0:
            failures.append(f"[pass] clean commit was blocked (returncode {r.returncode})\n{r.stderr}")
        log = _run(["git", "log", "--oneline"], work, env).stdout
        if "clean change" not in log:
            failures.append("[pass] clean commit did not land")

        # 4. IDEMPOTENT
        r = _run(["bash", "scripts/install-git-hooks.sh"], work, env)
        if r.returncode != 0 or "already installed" not in r.stdout:
            failures.append(f"[idempotent] re-install not a clean no-op: rc={r.returncode}\n{r.stdout}{r.stderr}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    failures = []
    test_hook_blocks_and_passes(failures)
    if failures:
        print("RED — pre-commit redaction hook:")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("GREEN — pre-commit redaction hook blocks leaks, passes clean commits, installs idempotently")


if __name__ == "__main__":
    main()
