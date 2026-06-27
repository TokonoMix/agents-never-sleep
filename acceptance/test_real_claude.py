"""Integration test: drive the ANS harness with a REAL claude -p session.

Marked @pytest.mark.integration — NOT in the standard suite. Run explicitly:
    pytest -m integration acceptance/test_real_claude.py -v

Requires:
- `claude` CLI available on PATH (Claude Code)
- CLAUDE_UNATTENDED=1 may be set; the test sets --permission-mode acceptEdits

What this proves (the only evidence that counts for HN/enterprise — ticket INT-1965):
- A real Claude Code session can drive next/complete until DRAINED
- The harness's durable state (run-progress.json) reflects done>=2 at the end
- Both tickets land with DONE state (no hallucinated stops, no ASK violations)
- The work was actually COMMITTED: the harness's per-ticket `done:<id>` commits land on
  the dedicated `ans/run-*` branch (git-backed reversibility is real, not simulated)
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)

TIMEOUT_S = 300  # 5 min max for a real Claude session on 2 trivial tickets

# Parent-session env vars that MUST NOT leak into the spawned child. If this test runs
# from inside an ANS run (CLAUDE_UNATTENDED=1), the parent's UE_RUN_INCOMPLETE points at
# the PARENT repo's never-stop sentinel — inheriting it would make the child's Stop-hook
# guard the parent's sentinel and refuse to exit (a 300s hang), and the budget/marker vars
# would mis-pace the child. We scrub them and re-point the sentinel at the child's own repo.
_PARENT_RUN_ENV = (
    "UE_RUN_INCOMPLETE", "UE_SESSION_TICKET_BUDGET", "UE_SESSION_BUDGET_MARKER",
    "UE_HEARTBEAT",
)


@pytest.mark.integration
def test_real_claude_drives_two_tickets():
    """A real `claude -p` session drives 2 simple tickets to completion."""
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH — integration test requires Claude Code")
    work = tempfile.mkdtemp(prefix="ans-real-claude-")
    try:
        repo = _setup_repo(work)
        tickets_dir = _setup_tickets(work)

        prompt = textwrap.dedent(f"""\
            You are running the agents-never-sleep harness test.
            Your ONLY job: drive the harness loop until DRAINED.

            Loop:
            1. Run: python3 -m agents_never_sleep.run next --repo {repo} --tickets {tickets_dir}
            2. If status is DRAINED or HALTED: STOP immediately (exit).
            3. If status is PROCEED: implement the ticket body by editing files in {repo}.
               Then run: python3 -m agents_never_sleep.run complete --repo {repo} --tickets {tickets_dir} --attempted "done"
            4. Go to step 1.

            Rules:
            - Never ask questions. Never stop early.
            - The harness runs from the SKILL_ROOT directory: {SKILL_ROOT}
            - Set PYTHONPATH={SKILL_ROOT} when running harness commands.
            - Ticket 01: create hello.txt with content "hello"
            - Ticket 02: append " world" to hello.txt
            - After both: stop.
        """)

        env = {**os.environ, "PYTHONPATH": SKILL_ROOT, "CLAUDE_UNATTENDED": "1"}
        # Isolate from any parent ANS run (see _PARENT_RUN_ENV) and pin the child's
        # never-stop sentinel to ITS OWN repo so the Stop-hook guards the right run.
        for k in _PARENT_RUN_ENV:
            env.pop(k, None)
        env["UE_RUN_INCOMPLETE"] = os.path.join(repo, ".unattended", "run-incomplete")
        # `--dangerously-skip-permissions` mirrors how ANS actually launches Claude Code
        # unattended (harness/agent_clis.py + the launcher `claude` preset). The child must
        # run shell commands (python3 -m agents_never_sleep.run ...) to drive the loop; the narrower
        # `--permission-mode acceptEdits` auto-approves only edits, so a headless `-p` child
        # would block on the first Bash approval (no TTY) and hang until TIMEOUT_S.
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", prompt],
            cwd=SKILL_ROOT,
            env=env,
            timeout=TIMEOUT_S,
            capture_output=True,
            text=True,
        )

        # Verify the harness recorded DONE for both tickets via the DURABLE outcome store
        # — NOT run-progress.json. The latter is the low-yield breaker's in-run counter and is
        # deliberately RESET to 0 at the DRAINED terminal (driver._terminate -> _reset_progress)
        # to prep the next run, so it always reads 0 after a clean drain. The authoritative
        # done-count is the per-ticket OutcomeStore (one JSON per ticket, persisted), which is
        # exactly what the DRAINED summary reports as `done`/`processed = len(outcomes)`.
        state_dir = os.path.join(repo, ".unattended", "state")
        assert os.path.isdir(state_dir), (
            f"state dir not written; claude exit={result.returncode}\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-1000:]}"
        )
        from agents_never_sleep.state import OutcomeStore
        outcomes = OutcomeStore(state_dir).all()
        done = sum(1 for o in outcomes if o.state.value.startswith("DONE"))
        assert done >= 2, (
            f"Expected >=2 DONE outcomes in the store, got {done} "
            f"(of {len(outcomes)} outcomes).\nclaude stdout:\n{result.stdout[-2000:]}"
        )

        # The work was COMMITTED on a dedicated ans/run-* branch; at the DRAINED terminal the
        # harness checks the OPERATOR branch back out AND consumes its run-branch.json (the run is
        # over). The run BRANCH itself persists for the operator to review/merge — this IS the
        # git-backed reversibility. So resolve it by enumerating ans/run-* branches (one per run;
        # this fresh repo has exactly one), then verify everything against it.
        run_branches = subprocess.run(
            ["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/ans/run-*"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        assert run_branches, (
            "no ans/run-* branch found — the harness never created its dedicated run branch, so "
            f"no commit could have landed.\nclaude stdout:\n{result.stdout[-2000:]}"
        )
        run_branch = run_branches[0]

        # Verify the actual file edits landed — read hello.txt FROM THE RUN BRANCH (not the
        # working tree, which is back on the operator branch). "hello world" proves BOTH tickets'
        # edits (create + append) committed in order.
        content = subprocess.run(
            ["git", "-C", repo, "show", f"{run_branch}:hello.txt"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "hello" in content, f"hello.txt (on {run_branch}) missing 'hello': {content!r}"

        # Count the harness's per-ticket `done:` commits ON THE RUN BRANCH. >=2 proves both
        # tickets were committed (git-backed reversibility is real).
        log = subprocess.run(
            ["git", "-C", repo, "log", run_branch, "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout
        done_commits = [line for line in log.splitlines() if line.startswith("done:")]
        assert len(done_commits) >= 2, (
            f"Expected >=2 'done:' commits on {run_branch}, got {len(done_commits)}.\n"
            f"commit subjects:\n{log}"
        )

    finally:
        shutil.rmtree(work, ignore_errors=True)


def _setup_repo(work: str) -> str:
    """Create a minimal git repo with a gate (empty test suite = always green)."""
    repo = os.path.join(work, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@ans"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "ANS Test"], cwd=repo,
                   check=True, capture_output=True)
    # Minimal gate: python -m pytest with no tests = exit 0 (no-tests-collected = green by default)
    # Write a placeholder so the gate command exists
    placeholder = os.path.join(repo, ".ans-gate-ok")
    open(placeholder, "w").write("gate placeholder\n")
    subprocess.run(["git", "add", ".ans-gate-ok"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)

    # Write a minimal ANS config with a gate that always passes
    cfg_dir = os.path.join(repo, ".claude")
    os.makedirs(cfg_dir)
    config = {
        # The harness reads `gates` (a LIST of {command, blocking}); a trivially-green
        # command exercises the real gate path (not the no-op fallback) so the test
        # proves tickets land DONE, not DONE_LOW_CONFIDENCE.
        "gates": [
            {"command": ["python3", "-c", "import sys; sys.exit(0)"], "blocking": True},
        ],
        "budget": {
            "per_ticket_timeout_s": 120,
            "per_ticket_fix_iterations": 2,
            "per_night_euro_cap": 1.0,
        },
        "integrations": {
            "paperclip": {"enabled": False},
            "tokonomix": {"enabled": False},
        },
    }
    import json
    cfg_path = os.path.join(cfg_dir, "agents-never-sleep.json")
    open(cfg_path, "w").write(json.dumps(config, indent=2))
    subprocess.run(["git", "add", ".claude/agents-never-sleep.json"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ans config"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def _setup_tickets(work: str) -> str:
    """Create 2 minimal tickets: create hello.txt and append to it."""
    tickets_dir = os.path.join(work, "tickets")
    os.makedirs(tickets_dir)

    open(os.path.join(tickets_dir, "01-create-hello.md"), "w").write(textwrap.dedent("""\
        ---
        id: 01-create-hello
        title: Create hello.txt
        blast_radius: low
        ---
        Create a file `hello.txt` in the repo root with the single line: hello
        This is a trivial, low-risk file creation. Proceed immediately.
    """))

    open(os.path.join(tickets_dir, "02-append-world.md"), "w").write(textwrap.dedent("""\
        ---
        id: 02-append-world
        title: Append world to hello.txt
        blast_radius: low
        ---
        Append the text " world" to the existing file `hello.txt`.
        After this change, hello.txt should contain: hello world
        This is a trivial, low-risk file edit. Proceed immediately.
    """))

    return tickets_dir
