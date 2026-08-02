"""ANS launcher implementation — generic pre-token GO/NO-GO preflight + atomic
working-tree lock, importable as `harness.launcher`.

This module holds the full launcher logic. `bin/ans-run` is a thin shim that calls
`main()` here, and the packaged console-script `ans-run` (see pyproject.toml) points at
`harness.launcher:main`. Keeping the body in the package means the same code runs whether
ANS is used from a checkout (via `bin/ans-run`) or pip-installed (via the `ans-run`
entry point) — single source of truth.

Why this exists (and why harness/preflight.py is not enough): the harness preflight runs
AFTER the agent session has booted — by then the first tokens are already spent and a doomed
run has already cost money. This launcher is the pre-boot gate: it refuses to start when the
environment cannot support an unattended run (wrong user, missing CLI credentials, broken
git, unwritable repo, full disk, host services down) and it guarantees at most ONE run per
working tree.

Security posture (review 2026-06-10): the config travels with the repo and describes code the
launcher executes pre-boot, so three independent gates apply before anything runs:
  1. TOFU trust — a repo-supplied config must be trusted once per user (`ans-run --trust`); any
     config change invalidates trust. Headless + untrusted = NO-GO.
  2. argv[0] allowlist — the spawned CLI must be a known agent CLI (claude/codex/gemini/copilot)
     unless the trusted config explicitly sets `launcher.allow_custom_agent`.
  3. Autonomy is explicit — a preset launches only when a human confirmed its autonomy flag.

Locking: an advisory flock(2) on `<repo>/.unattended/ans-run.lock`, acquired non-blocking BEFORE
the expensive probes and handed (as an open FD) to the agent process so the kernel releases it on
any crash/kill. No pidfiles (TOCTOU-racy).

Usage:
    ans-run [--repo DIR] [--agent NAME] [--fg] [--check] [--trust] [--] PROMPT...
Exit codes: 0 started/GO/trust-recorded (with --fg the agent's rc); 64 NO-GO; 65 tree busy.
Env: ANS_RUN_NO_LOCK=1 skip the working-tree lock; ANS_TRUST_STORE override the trust-store path.

See SKILL.md for the launch recipe, the sudoers note, and the full design rationale.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .agent_clis import (AGENT_CLIS, cli_for_argv, is_allowlisted,
                         is_noninteractive_permission)
from .fsutil import ensure_private_dir
from . import init_cmd   # top-level; launch-time-only module, see subcommand dispatch below
from .keysource import resolve_ref
from .redact import register_secret
from .trust import config_digest, is_trusted, record_trust

EX_NOGO = 64
EX_BUSY = 65
CONFIG_REL = os.path.join(".claude", "agents-never-sleep.json")
LOCK_REL = os.path.join(".unattended", "ans-run.lock")

DEFAULT_LAUNCHER = {
    "target_user": None,        # re-exec (root) / require this user; None = any user
    "default_agent": None,      # preset name under `agents` used when --agent is absent
    "agents": {},               # {name: {cmd: [...], autonomy_confirmed: bool, env: {}}}
    "agent_cmd": None,          # legacy flat form (pre-preset configs)
    "allow_custom_agent": False,  # permit argv[0] outside the known-CLI allowlist
    "credentials_paths": None,  # None = best-effort default probe (warn-only)
    "min_disk_mb": 1000,
    "log_dir": None,            # None = <repo>/.unattended/logs
    "checks": [],               # [{"name": ..., "command": ..., "blocking": true}]
    "fresh_session_every": 0,   # opt-in: respawn a FRESH agent every N completed tickets (0 = off,
                                # one accumulating session as before). See SKILL.md "Context strategy".
}
# Safety cap on the fresh-session respawn loop, so a stuck sentinel can never spin forever.
SESSION_RESPAWN_CAP = 500

# ---- G4b: auto-worktree isolation (launcher-owned lifecycle) ----------------------------------
# When autonomy.live_tree == "auto_worktree", the launcher runs the whole session inside a dedicated
# EXTERNAL `git worktree` so the primary/live tree is never touched. driver/vcs are UNCHANGED — they
# already run correctly in a linked worktree, and isolation propagates via cwd inheritance to every
# re-invoked `ans next`/`ans complete`. The run's `ans/run-*` branch lives in the shared object store,
# so it survives worktree cleanup and stays mergeable from the primary tree.

WORKTREE_MIN_FREE_MB = 200  # refuse (HALT) rather than create a worktree with too little free disk


class WorktreeError(Exception):
    """Creating the isolated worktree failed. The caller HALTs — it must NEVER silently fall back to
    running in-place, which would reintroduce the shared-live-tree hazard auto_worktree exists to avoid."""


def _wt_git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)


def _is_linked_worktree(repo: str) -> bool:
    """True if `repo` is a LINKED worktree (per-worktree git dir != shared common dir); False for the
    primary tree or on any git error (conservative: don't treat an unknown state as already-isolated)."""
    gd = _wt_git(repo, "rev-parse", "--git-dir")
    cd = _wt_git(repo, "rev-parse", "--git-common-dir")
    if gd.returncode != 0 or cd.returncode != 0:
        return False

    def _abs(p: str) -> str:
        p = p.strip()
        return p if os.path.isabs(p) else os.path.join(repo, p)
    return os.path.realpath(_abs(gd.stdout)) != os.path.realpath(_abs(cd.stdout))


def tracked_dirt_count(repo: str) -> int:
    """Number of TRACKED working-tree/index changes (modified, staged, or deleted) — the state a
    drain-restore (git revert/checkout to a ref) would CLOBBER. UNTRACKED files (`??`) are excluded on
    purpose: a git revert never touches them, so they carry no clobber risk and must not force
    isolation (else any repo with a stray log/build artifact would be treated as 'dirty'). This is the
    signal that drives the auto-isolation default — it keys precisely on the field incident (a human's
    modified, uncommitted tracked file being reverted). Returns 0 on any git error (fail toward the
    non-upgrading default; the explicit live_tree policy still governs)."""
    r = _wt_git(repo, "status", "--porcelain")
    if r.returncode != 0:
        return 0
    # A valid porcelain entry is "XY PATH" (>=4 chars); require >=3 so a truncated/blank line can never
    # be misread as tracked dirt. Untracked ("??") is the only 2-char status we deliberately exclude.
    return sum(1 for line in r.stdout.splitlines() if len(line) >= 3 and line[:2] != "??")


def resolve_live_tree_policy(configured, *, is_dirty: bool, is_foreground: bool,
                             is_linked: bool) -> str:
    """Resolve the EFFECTIVE autonomy.live_tree policy, applying the default-upgrade that protects a
    human's uncommitted work. An explicit operator choice (auto_worktree / ack / require_isolation) is
    honored VERBATIM — never silently overridden. Otherwise (unset / 'warn' / unknown), a DETACHED run
    on a DIRTY, non-linked primary tree defaults to auto_worktree so an unattended run can never revert
    the human's changes in place (the 2026-07-19 incident). A clean tree, a foreground (--fg) run
    (where auto_worktree is unsupported — this process execs into the agent), or an already-linked
    worktree are left at 'warn' (no upgrade)."""
    p = configured.strip().lower() if isinstance(configured, str) else ""
    if p in ("auto_worktree", "ack", "require_isolation"):
        return p                       # explicit operator choice — honor verbatim
    if is_dirty and not is_foreground and not is_linked:
        return "auto_worktree"         # detached + dirty default -> isolate (protect uncommitted work)
    return "warn"


def ans_worktree_root(primary_repo: str) -> str:
    """The EXTERNAL directory (never nested under the primary tree) that holds this repo's isolated
    worktrees. Keyed by a hash of the primary's realpath so two repos sharing a basename don't
    collide, and so it survives a `.unattended` wipe."""
    rp = os.path.realpath(primary_repo)
    h = hashlib.sha1(rp.encode("utf-8")).hexdigest()[:8]
    return os.path.join(os.path.dirname(rp), ".ans-worktrees", f"{os.path.basename(rp)}-{h}")


def should_isolate(ans_config: dict, primary_repo: str) -> bool:
    """True iff autonomy.live_tree == 'auto_worktree' AND the target is not already a linked worktree
    (never nest). Deliberately NOT gated on 'dirty': the concurrency hazard is ongoing and the operator
    opted in explicitly."""
    if ((ans_config or {}).get("autonomy", {}) or {}).get("live_tree") != "auto_worktree":
        return False
    return not _is_linked_worktree(primary_repo)


def _remove_worktrees_under(primary_repo: str, root: str) -> None:
    """Force-remove every git-registered worktree whose path is under `root`, then prune admin entries.
    Best-effort — a leftover from a crash must not block a fresh run."""
    r = _wt_git(primary_repo, "worktree", "list", "--porcelain")
    if r.returncode == 0:
        rootrp = os.path.realpath(root)
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                p = line[len("worktree "):].strip()
                prp = os.path.realpath(p)
                if prp == rootrp or prp.startswith(rootrp + os.sep):
                    _wt_git(primary_repo, "worktree", "remove", "--force", p)
    _wt_git(primary_repo, "worktree", "prune")


def create_isolated_worktree(primary_repo: str, stamp: str) -> str:
    """Create a FRESH detached worktree under the external root and return its path. Always fresh:
    any leftover ANS worktree for this repo is force-removed first (the run branch, which lives in the
    shared store, is untouched). Raises WorktreeError on failure so the caller HALTs (never in-place)."""
    root = ans_worktree_root(primary_repo)
    _remove_worktrees_under(primary_repo, root)
    shutil.rmtree(root, ignore_errors=True)  # nuke any residue (hand-deleted admin, orphan dirs)
    parent = os.path.dirname(root)
    try:
        os.makedirs(parent, exist_ok=True)
        if shutil.disk_usage(parent).free < WORKTREE_MIN_FREE_MB * 1024 * 1024:
            raise WorktreeError(
                f"insufficient free disk (<{WORKTREE_MIN_FREE_MB} MB) for an isolated worktree at {parent}")
    except OSError:
        pass  # can't measure -> proceed; `worktree add` will fail loudly if the fs is truly unusable
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"run-{stamp}")
    # --detach: avoids the "branch already checked out in the primary" refusal and handles a detached
    # primary HEAD; the driver then creates its own ans/run-* inside the worktree exactly as usual.
    r = _wt_git(primary_repo, "worktree", "add", "--detach", path, "HEAD")
    if r.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {(r.stderr or r.stdout).strip()}")
    return path


def cleanup_isolated_worktree(primary_repo: str, worktree_path: str) -> bool:
    """Remove the isolated worktree on a normal terminal. Returns True if it is gone, False if it was
    left in place (removal refused for a reason other than the expected untracked-.unattended dirt —
    then we do NOT destroy possible evidence). The ans/run-* branch is never touched: it lives in the
    shared store and is the operator's to merge."""
    try:
        if os.path.realpath(os.getcwd()).startswith(os.path.realpath(worktree_path)):
            os.chdir(primary_repo)  # can't remove the cwd on some platforms
    except OSError:
        pass
    # --force is REQUIRED: the untracked .unattended/ state dir makes the worktree "dirty" and a plain
    # `remove` would refuse — the #1 worktree-leak source.
    r = _wt_git(primary_repo, "worktree", "remove", "--force", worktree_path)
    _wt_git(primary_repo, "worktree", "prune")
    if r.returncode != 0 and os.path.exists(worktree_path):
        return False
    return True
# Probed when credentials_paths is not configured. Absence is a WARNING only: platforms
# like macOS keep Claude Code credentials in the keychain, and API-key setups have no file.
DEFAULT_CREDENTIAL_PROBES = ["~/.claude/.credentials.json"]


class Report:
    """Collects preflight results; blocking failures decide GO vs NO-GO."""

    def __init__(self) -> None:
        self.failed = False
        self.warned = False

    def ok(self, msg: str) -> None:
        print(f"  ✓ {msg}")

    def bad(self, msg: str) -> None:
        print(f"  ✗ {msg}")
        self.failed = True

    def note(self, msg: str) -> None:
        print(f"  ⚠ {msg}")
        self.warned = True


def _is_interactive() -> bool:
    return (sys.stdin.isatty() and sys.stdout.isatty()
            and not os.environ.get("CLAUDE_UNATTENDED"))


def load_launcher_config(repo: str, rep: Report) -> tuple:
    """Returns (cfg, config_exists, digest, full_config). The config is read ONCE: the
    digest is the hash of the exact bytes parsed here, so the trust decision and the
    executed argv come from the same read (no TOCTOU second read of the repo-writable
    path). Invalid JSON is blocking — a half-read config must never fall back to defaults
    that launch something else. `full_config` is the whole parsed document (not just the
    launcher section): the keysource resolver reads `integrations.vault` from it."""
    cfg = dict(DEFAULT_LAUNCHER)
    path = os.path.join(repo, CONFIG_REL)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return cfg, False, None, {}
    digest = config_digest(path, data=raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        rep.bad(f"{CONFIG_REL} unreadable/invalid JSON ({exc}) — fix it before launching")
        return cfg, True, digest, {}
    if not isinstance(data, dict):
        rep.bad(f"{CONFIG_REL} is not a JSON object — fix it before launching")
        return cfg, True, digest, {}
    launcher = data.get("launcher")
    if launcher is None:
        rep.note("config has no `launcher` section — using launcher defaults")
        return cfg, True, digest, data
    if not isinstance(launcher, dict):
        rep.bad("`launcher` section is not an object — fix the config before launching")
        return cfg, True, digest, data
    cfg.update(launcher)
    return cfg, True, digest, data


def check_trust(repo: str, digest: str, rep: Report) -> bool:
    """TOFU gate. True = proceed. The config is executable input (agent argv + shell
    checks), so an untrusted/changed config never runs headless. `digest` is the hash of
    the same bytes load_launcher_config parsed."""
    if digest is None:
        return True  # no config -> only built-in defaults run -> nothing to trust
    config_path = os.path.join(repo, CONFIG_REL)
    if is_trusted(repo, config_path, digest=digest):
        rep.ok("config trusted (hash matches the recorded trust)")
        return True
    if _is_interactive():
        print(f"  ⚠ {CONFIG_REL} is NEW or CHANGED for this repo.")
        print("    It defines commands ans-run will execute (agent argv, host checks).")
        try:
            answer = input("    Trust this config? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer.startswith("y"):
            record_trust(repo, config_path, digest=digest)
            rep.ok("config trusted (recorded for this user)")
            return True
        rep.bad("config not trusted — refused")
        return False
    rep.bad("config is new/changed and this is a non-interactive session — review the "
            f"config, then run: ans-run --trust --repo {repo}")
    return False


def _user_is_root(username: str) -> bool:
    if username == "root":
        return True
    try:
        import pwd
        return pwd.getpwnam(username).pw_uid == 0
    except (ImportError, KeyError):
        return False  # unknown user: sudo itself will refuse with a clear error


def trusted_target_user(repo: str, config_path: str) -> str | None:
    """The `launcher.target_user` from a TRUSTED config, or None — missing config, untrusted
    config, unparseable config, or no target_user set all return None alike (L3, post-audit).

    The config is untrusted, repo-writable input; reading target_user from it before TOFU
    verification would let a malicious repo pick which user identity a root-launched process
    assumes, ahead of (and regardless of) the ordinary trust gate later in `main()`. Reads the
    config bytes ONCE so the trust check and the value it gates come from the identical buffer
    (`config_digest`'s own "hash the buffer you parsed" rule) — no second, TOCTOU-able read of a
    repo-writable path between the check and the value it's supposed to protect."""
    try:
        with open(config_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not is_trusted(repo, config_path, digest=config_digest(config_path, data=raw)):
        return None
    try:
        target = (json.loads(raw.decode("utf-8")).get("launcher") or {}).get("target_user")
    except ValueError:
        return None
    return str(target) if target else None


def reexec_as_target_user(target_user: str) -> int:
    """Root-guard: unattended runs must not run as root (credentials, settings and repo
    ownership live in the target user's HOME). Re-exec keeps the launch one command.
    For unattended use prefer running the cron/systemd job AS the target user; where
    re-exec is needed, use a command-scoped sudoers rule (see SKILL.md) — never
    NOPASSWD: ALL."""
    if _user_is_root(target_user):
        print("ans-run: launcher.target_user must not be root — refusing (would re-exec "
              "forever)", file=sys.stderr)
        return EX_NOGO
    print(f"ans-run: started as root — re-exec as user '{target_user}'...", file=sys.stderr)
    # Re-exec the SHIM (bin/ans-run), not this module: the shim self-bootstraps sys.path
    # (it prepends the skill root before importing harness), so it works from any cwd. We must
    # NOT re-exec `python -m agents_never_sleep.launcher` — `sudo -H` env-resets PYTHONPATH and the re-exec
    # happens before chdir(repo), so `harness` would be unimportable for the target user.
    # When ANS is pip-installed (no checkout shim on disk), fall back to the `ans-run` console
    # script, which resolves the package the normal site-packages way.
    shim = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bin", "ans-run")
    relaunch = ([sys.executable, shim] if os.path.exists(shim) else ["ans-run"])
    try:
        os.execvp("sudo", ["sudo", "-H", "-u", target_user, *relaunch] + sys.argv[1:])
    except OSError as exc:
        print(f"ans-run: re-exec via sudo failed ({exc}) — is sudo installed and is there "
              "a command-scoped NOPASSWD rule for unattended use?", file=sys.stderr)
        return EX_NOGO
    return EX_NOGO  # unreachable


def check_identity(cfg: dict, rep: Report) -> None:
    import getpass
    me = getpass.getuser()
    target = cfg.get("target_user")
    if target and _user_is_root(str(target)):
        rep.bad("launcher.target_user must not be root")
    elif target and me != target:
        rep.bad(f"running as '{me}', config requires '{target}' — start as that user")
    elif os.geteuid() == 0:
        rep.bad("running as root with no launcher.target_user configured — refusing; "
                "set launcher.target_user or start as the intended user")
    else:
        rep.ok(f"running as user '{me}'" + (" (matches target_user)" if target else ""))


def _as_argv(raw) -> list:
    return shlex.split(raw) if isinstance(raw, str) else [str(a) for a in (raw or [])]


def resolve_agent(cfg: dict, config_exists: bool, agent_flag: str, rep: Report) -> tuple:
    """Pick the agent invocation: (argv, env, preset_name). Selection NEVER constructs
    argv on the fly — it chooses among presets a human confirmed (or the legacy flat
    agent_cmd from a trusted config). Returns ([], {}, "") on NO-GO."""
    known = ", ".join(sorted(AGENT_CLIS))
    agents = cfg.get("agents") or {}
    if agents:
        name = agent_flag or cfg.get("default_agent")
        if not name and len(agents) == 1:
            name = next(iter(agents))
        if not name:
            rep.bad("multiple agent presets but no --agent and no launcher.default_agent "
                    f"(available: {', '.join(sorted(agents))})")
            return [], {}, ""
        preset = agents.get(name)
        if not isinstance(preset, dict):
            rep.bad(f"agent preset '{name}' not found "
                    f"(available: {', '.join(sorted(agents))})")
            return [], {}, ""
        argv = _as_argv(preset.get("cmd"))
        if not argv:
            rep.bad(f"agent preset '{name}' has an empty cmd")
            return [], {}, ""
        if not preset.get("autonomy_confirmed"):
            rep.bad(f"agent preset '{name}' has no confirmed autonomy decision — a "
                    "detached run would stall on its first permission prompt (or you must "
                    "explicitly accept its autonomy flag). Run the wizard, review the "
                    "flag, then set autonomy_confirmed=true")
            return [], {}, ""
        # Ticket 05-B: opt-in RUN-LEVEL capability restriction. When a preset declares a
        # `capabilities` list (e.g. ["--strict-mcp-config", "--mcp-config", "/path/min.json"]),
        # append it so the agent loads only the declared MCP/tool set — smaller memory footprint +
        # attack surface. Absent/empty → today's FULL set (no-op). NB: ANS launches ONCE for the
        # whole backlog, so this is per-RUN, not per-ticket (true per-ticket would need per-ticket
        # respawn); the "inferred" variant is intentionally out of scope. Validated to non-empty str.
        caps = preset.get("capabilities")
        if caps:
            if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
                rep.bad(f"agent preset '{name}': capabilities must be a list of non-empty strings "
                        "(e.g. [\"--strict-mcp-config\", \"--mcp-config\", \"/path/min.json\"])")
                return [], {}, ""
            argv = argv + [str(c) for c in caps]
            rep.note(f"capability restriction: appended {len(caps)} declared token(s) to preset "
                     f"'{name}' (agent loads only the declared set)")
        env = {str(k): str(v) for k, v in (preset.get("env") or {}).items()}
        rep.ok(f"agent preset '{name}': {' '.join(argv)}")
        return argv, env, name
    if agent_flag:
        rep.bad(f"--agent '{agent_flag}' given but the config defines no agent presets")
        return [], {}, ""
    legacy = cfg.get("agent_cmd")
    if legacy:
        # Legacy flat form: the (TOFU-trusted) config carries the full argv; the human
        # vouched for it when trusting the config.
        argv = _as_argv(legacy)
        if not argv:
            rep.bad("launcher.agent_cmd is empty")
            return [], {}, ""
        rep.ok(f"agent (legacy agent_cmd): {' '.join(argv)}")
        return argv, {}, "agent_cmd"
    if config_exists:
        rep.bad(f"config has no agent presets and no agent_cmd — run the wizard "
                f"(known CLIs: {known})")
    else:
        rep.bad(f"no {CONFIG_REL} — an unattended launch needs an explicit, human-"
                f"confirmed agent choice. Run the wizard first (known CLIs: {known})")
    return [], {}, ""


def check_enforcement_hooks_wired(agent_argv: list, rep: Report) -> None:
    """Non-blocking preflight NOTE (consensus T1): `ans-run init` -> skip `install-hooks` ->
    a detached run starts with the ANS deny-hooks NOT wired into the resolved harness's host
    config, and nothing surfaces this today (the morning-report blind_spot machinery does not
    cover it). This is deliberately a NOTE, never a NO-GO — the trusted-operator model rules
    out blocking on it; the operator just needs the state named, with the remedy. Reuses
    init_cmd.hooks_wired, which also treats a referenced-but-missing hook script (moved/deleted
    install) as unwired. No harness resolved (empty/custom argv) -> generic note, never a
    fabricated harness identity."""
    if not agent_argv:
        return  # resolve_agent already recorded the NO-GO for this
    cli = cli_for_argv(agent_argv)
    if not cli:
        rep.note("no recognized agent CLI resolved — cannot verify enforcement hooks are "
                 "wired; this run proceeds on the prose-contract only")
        return
    if init_cmd.hooks_wired(cli):
        return
    rep.note(f"enforcement hooks not detected for '{cli}' — run `ans-run install-hooks "
             f"--harness {cli}` to wire them; this run proceeds on the prose-contract only")


def check_detached_permission_mode(agent_argv, foreground: bool, rep: Report) -> None:
    """A detached run has stdin closed (Popen stdin=DEVNULL / no --fg). If the agent CLI's
    OWN permission system is still interactive, the first tool-approval prompt HANGS the run
    silently — a gate ABOVE the agent that no prompt-instruction can bypass. resolve_agent
    already checks the preset's autonomy_confirmed BOOLEAN, but that is a human-set flag a
    hand-edited config can desync from the actual cmd; here we inspect the RESOLVED argv for
    the CLI's real non-interactive permission flag. Blocking only for a detached launch
    (foreground = a human is attached and interactive approvals are their explicit choice)."""
    if not agent_argv:
        return  # resolve_agent already recorded a NO-GO
    verdict = is_noninteractive_permission(agent_argv)
    cli = cli_for_argv(agent_argv) or os.path.basename(str(agent_argv[0]))
    if verdict is True:
        # "won't hang" is NOT "fully autonomous": claude --permission-mode acceptEdits
        # auto-approves FILE EDITS only; in headless -p a gated Bash/network tool is
        # auto-DENIED (not hung), so a bash-heavy backlog run makes no progress. Only a
        # FULL-autonomy flag actually lets a detached run function end-to-end.
        tokens = [str(a) for a in agent_argv]

        def _pm_is(values: set) -> bool:
            for i, tok in enumerate(tokens):
                if tok == "--permission-mode" and i + 1 < len(tokens) and tokens[i + 1] in values:
                    return True
                if tok.startswith("--permission-mode=") and tok.split("=", 1)[1] in values:
                    return True
            return False

        edits_only = (cli == "claude" and "--dangerously-skip-permissions" not in tokens
                      and not _pm_is({"bypassPermissions"}) and _pm_is({"acceptEdits"}))
        if edits_only:
            rep.note(f"permission mode: '{cli}' auto-approves FILE EDITS only (won't hang), but "
                     "shell/network tools stay gated → auto-denied in a detached run; a bash-"
                     "heavy backlog may make no progress. Full autonomy = "
                     "--dangerously-skip-permissions")
        else:
            rep.ok(f"permission mode: '{cli}' argv is non-interactive with full autonomy "
                   "(safe to run detached)")
    elif verdict is None:
        rep.note(f"permission mode: cannot verify for custom agent '{cli}' — relying on the "
                 "preset's autonomy_confirmed; make sure its flags don't gate tool calls "
                 "interactively, or a detached run will hang on the first prompt")
    elif foreground:
        rep.note(f"permission mode: '{cli}' argv is interactive — fine with --fg (a human is "
                 "attached), but a DETACHED launch of this preset would hang on the first "
                 "tool prompt")
    else:
        rep.bad("detached run + interactive permission mode: the resolved '" + cli + "' argv "
                "carries no non-interactive permission flag, so it would hang silently on the "
                "first tool prompt (stdin is closed). Add the CLI's autonomy flag (claude: "
                "--permission-mode acceptEdits or --dangerously-skip-permissions; codex: "
                "--sandbox workspace-write; gemini: --yolo; copilot: --allow-all-tools), or "
                "run foreground with --fg")


# Env vars in a preset that change which code a "trusted" invocation actually runs. Even
# under a trusted config these deserve a loud flag — the security review showed how innocent
# a malicious preset looks at trust time (security 2026-06-10, findings 2+5).
_RESOLUTION_ENV = ("PATH",)
_INJECTION_ENV = ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "NODE_OPTIONS",
                  "BASH_ENV", "DYLD_INSERT_LIBRARIES")

# Token-shaped literal: one long run of key-alphabet characters, no path/URL separators.
_SECRET_SHAPE = re.compile(r"^[A-Za-z0-9_.\-]{20,}$")


def _looks_like_secret(value: str) -> bool:
    """Heuristic for 'an API key pasted literally into the repo config': a long token-shaped
    string (no spaces, no / or ://) mixing letters and digits with high character entropy.
    URLs, paths, flag values and model slugs stay below the bar; a hex/base62 credential
    clears it. Used for a WARNING only — a literal still runs, it is just flagged."""
    if not _SECRET_SHAPE.match(value):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)):
        return False
    freq: dict = {}
    for ch in value:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(value)
    entropy = -sum((c / n) * math.log2(c / n) for c in freq.values())
    return entropy >= 3.5


def resolve_preset_env(agent_env: dict, full_config: dict, rep: Report) -> dict:
    """F4 — token-ref indirection for preset env values, resolved BEFORE the capability
    probe so probe and spawn share the same environment (probe == spawn rule).

    `env:VAR` resolves from the launcher's own environment; `vault:<mount>/<path>[#field]`
    resolves via harness.keysource (the existing resolver — `integrations.vault` in the
    config gates it; env:/vault: are NOT reimplemented here). A literal value passes
    through unchanged, but a literal that LOOKS like a pasted secret is flagged in the
    spirit of _INJECTION_ENV: token-refs are the contract, the gateway key must never live
    in the repo config. Resolution failure (missing env var, unreadable vault path,
    disabled vault integration) is a blocking NO-GO — never a silent empty value. Resolved
    values are registered with the redaction layer by the resolver and are NEVER printed;
    only the ref text (which holds no secret) appears in messages.

    Trust scope (council review 2026-06-10): an `env:` ref lets the (TOFU-trusted) config
    choose WHICH launcher env vars enter the child env — that choice is part of what the
    human vouches for at --trust time, exactly like the argv and host checks the config
    already carries. A failed ref never falls back to a same-named launcher var: the key
    is omitted AND the launch is refused (rep.bad → exit 64 before any spawn)."""
    resolved_env: dict = {}
    for key, value in agent_env.items():
        if value.startswith(("env:", "vault:")):
            res = resolve_ref(value, config=full_config)
            if not res.value:  # None or "" — an empty value must gate exactly like a missing one
                rep.bad(f"preset env {key}: token-ref '{value}' did not resolve — "
                        f"{res.blind_spot or 'no value at the configured source'}")
                continue
            rep.ok(f"preset env {key}: resolved via {res.source} (value not shown)")
            if res.blind_spot:  # e.g. L5's non-loopback/non-https vault endpoint warning
                rep.note(f"preset env {key}: {res.blind_spot}")
            resolved_env[key] = res.value
        else:
            if _looks_like_secret(value):
                register_secret(value)
                rep.note(f"preset env {key} looks like a LITERAL secret pasted into the "
                         "repo config — use a token-ref (env:VAR or vault:path#field) so "
                         "the key never lives in the repo")
            resolved_env[key] = value
    return resolved_env


def check_agent_binary(argv: list, agent_env: dict, child_env: dict, cfg: dict,
                       rep: Report) -> None:
    """Allowlist + presence + capability probe — resolved under the EFFECTIVE CHILD ENV so
    the probe validates the SAME binary the spawn will run (a preset PATH must not let the
    probe check /usr/bin/claude while the launch runs a repo-shipped one). The 5s --version
    handshake is the cheapest insurance against flag/marker rot: binary-in-PATH says nothing
    about a CLI that moved its flags."""
    if not argv:
        return
    argv0 = argv[0]
    base = os.path.basename(argv0)
    if not is_allowlisted(argv0):
        if cfg.get("allow_custom_agent"):
            rep.note(f"'{argv0}' is not a bare known-CLI name — allowed because the trusted "
                     "config sets launcher.allow_custom_agent")
        else:
            rep.bad(f"'{argv0}' is not a known agent CLI invoked by bare name "
                    f"({', '.join(sorted(AGENT_CLIS))}) — refused. A path-bearing command "
                    "(./claude, /abs/claude) needs launcher.allow_custom_agent in the "
                    "config + re-trust")
            return
    for var in _RESOLUTION_ENV + _INJECTION_ENV:
        if var in agent_env:
            rep.note(f"preset env sets {var} — it changes which code the agent runs; "
                     "make sure you trust this preset")
    # Resolve against the child PATH (preset env merged), so probe == spawn target.
    resolved = shutil.which(argv0, path=child_env.get("PATH"))
    if not resolved:
        rep.bad(f"agent CLI '{argv0}' not found in PATH (as the run will see it)")
        return
    rep.ok(f"agent CLI found: {resolved}")
    probe = AGENT_CLIS.get(base, {}).get("version_args")
    if probe is None:
        probe = _as_argv(cfg.get("agent_probe_args")) or None
    if probe is None:
        rep.note(f"no capability probe known for '{base}' — skipping the pre-boot "
                 "handshake (set launcher.agent_probe_args to enable one)")
        return
    try:
        # capture_output + never echoed: the probe's stdout/stderr are deliberately
        # DISCARDED (only the rc is used), so a CLI that dumps env/config on --version
        # can never leak a resolved token-ref value into launcher output or a log
        # (security lens, F4 review 2026-06-10).
        res = subprocess.run([resolved, *probe], capture_output=True, text=True,
                             timeout=5, env=child_env)
        if res.returncode == 0:
            rep.ok(f"capability probe passed ({base} {' '.join(probe)})")
        else:
            rep.bad(f"capability probe FAILED (rc={res.returncode}) — the CLI is present "
                    "but rejected its own probe args; flags may have drifted")
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.bad(f"capability probe failed to run ({exc})")


def check_credentials(cfg: dict, rep: Report) -> None:
    configured = cfg.get("credentials_paths")
    paths = configured if configured is not None else DEFAULT_CREDENTIAL_PROBES
    expanded = [os.path.expanduser(p) for p in paths]
    found = [p for p in expanded if os.path.exists(p) and os.path.getsize(p) > 0]
    if found:
        rep.ok(f"credentials present: {found[0]}")
    elif configured is not None:
        # Explicitly configured -> the operator promised these exist -> blocking.
        rep.bad(f"none of the configured credentials_paths exist: {expanded}")
    else:
        rep.note("no credential file found (keychain/API-key setups have none) — "
                 "set launcher.credentials_paths to make this check blocking")


def check_git(repo: str, rep: Report) -> None:
    def _git(*args):
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, timeout=30)
    try:
        inside = _git("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.bad(f"git not usable ({exc})")
        return
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        rep.bad("git fails in repo (dubious ownership / not a repo?) — "
                + inside.stderr.strip()[:120])
        return
    rep.ok("git works (no ownership problem)")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "?"
    porcelain = _git("status", "--porcelain").stdout
    dirty = sum(1 for line in porcelain.splitlines() if line[:2].strip())
    staged = sum(1 for line in porcelain.splitlines() if line[:1].strip())
    if dirty:
        rep.note(f"branch {branch}: working tree has {dirty} changed file(s) — "
                 "check the parked-files list before an unattended run")
    else:
        rep.ok(f"branch {branch}: working tree clean")
    if staged:
        rep.note(f"{staged} file(s) already staged — the run would not start clean")


def check_writable(repo: str, rep: Report) -> None:
    probe_dir = os.path.join(repo, ".unattended")
    probe = os.path.join(probe_dir, ".preflight-write-test")
    try:
        ensure_private_dir(probe_dir)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        rep.ok("repo writable")
    except OSError:
        rep.bad(f"repo not writable for this user: {repo}")


def check_disk(repo: str, cfg: dict, rep: Report) -> None:
    mm = cfg.get("min_disk_mb")
    min_mb = int(DEFAULT_LAUNCHER["min_disk_mb"] if mm is None else mm)
    if min_mb <= 0:
        rep.note("disk-space check disabled (min_disk_mb <= 0)")
        return
    try:
        avail_mb = shutil.disk_usage(repo).free // (1024 * 1024)
    except OSError:
        rep.note("disk-space check failed — skipped")
        return
    if avail_mb >= min_mb:
        rep.ok(f"disk space: {avail_mb // 1024}G free")
    else:
        rep.bad(f"disk space low: {avail_mb}M free (< {min_mb}M required)")


def run_config_checks(repo: str, cfg: dict, rep: Report) -> None:
    """Host-specific checks come ONLY from config — nothing site-specific lives in code.

    Trust boundary: `command` is an operator-written shell line from the repo's own
    config. shell=True is deliberate (pipes, env vars) and is why the TOFU gate runs
    BEFORE this function: an untrusted repo config never reaches these commands."""
    for chk in cfg.get("checks") or []:
        name = str(chk.get("name") or chk.get("command") or "unnamed check")
        command = chk.get("command")
        blocking = bool(chk.get("blocking", True))
        if not command:
            rep.note(f"check '{name}' has no command — skipped")
            continue
        try:
            res = subprocess.run(command, shell=True, cwd=repo, capture_output=True,
                                 text=True, timeout=int(chk.get("timeout_s", 60)))
            passed = res.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        if passed:
            rep.ok(f"check '{name}' passed")
        elif blocking:
            rep.bad(f"check '{name}' FAILED (blocking)")
        else:
            rep.note(f"check '{name}' failed (non-blocking)")


def acquire_lock(repo: str, rep: Report, *, probe_only: bool):
    """Take the working-tree lock non-blocking, BEFORE the expensive preflight probes.

    Returns (fd, busy): fd is the held lock (None when disabled or probe_only), busy is
    True when another run holds it. The caller hands fd to the long-lived agent process so
    the kernel releases the lock the moment the run dies. probe_only (--check) acquires and
    releases immediately: it reports without ever blocking or disturbing a launch."""
    if os.environ.get("ANS_RUN_NO_LOCK") == "1":
        rep.note("ANS_RUN_NO_LOCK=1 — working-tree lock SKIPPED")
        return None, False
    path = os.path.join(repo, LOCK_REL)
    try:
        ensure_private_dir(os.path.dirname(path))
        # O_NOFOLLOW: never follow a pre-planted symlink.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        rep.bad(f"cannot open lock file {path} ({exc}) — repo unwritable or lock poisoned")
        return None, False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        rep.note(f"another run is active on this working tree (lock: {path})") \
            if probe_only else rep.bad(f"working tree busy (lock: {path})")
        return None, True
    if probe_only:
        os.close(fd)
        rep.ok("no other run active on this working tree (lock free)")
        return None, False
    rep.ok("working-tree lock acquired (released automatically when the run ends)")
    return fd, False


def open_log(repo: str, cfg: dict) -> str:
    log_dir = cfg.get("log_dir")
    log_dir = os.path.expanduser(log_dir) if log_dir else os.path.join(
        repo, ".unattended", "logs")
    created = not os.path.isdir(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    # The background run-log holds the agent's RAW stdout/stderr, which the redaction layer
    # does NOT scrub (it covers only harness-written surfaces) — so a resolved gateway key
    # the agent echoed can land here. Keep the log dir owner-only when we create it so the
    # unredacted stream is never world-readable (F4 security review 2026-06-10).
    if created:
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(log_dir, f"ans-{os.path.basename(repo)}-{stamp}.log")


def run_fresh_session_loop(repo, cfg, full_argv, child_env, fresh_n, lock_fd, release,
                           preset_name, log_repo=None) -> int:
    """Opt-in context strategy: instead of ONE long accumulating agent session, spawn a FRESH
    agent every N completed tickets so a long backlog doesn't degrade as one session.

    The supervisor runs in the FOREGROUND of this process (typically already detached by
    claude-run/cron): it holds the tree lock for the whole loop and respawns the agent until the
    backlog is genuinely drained. Terminal detection is the run-incomplete sentinel: ABSENT after an
    agent exits => the run reached a terminal state (DRAINED/HALTED/…) => stop. PRESENT => more work
    => clear the per-session counter + marker and spawn a fresh agent. A hard cap bounds the loop so
    a stuck sentinel can never spin forever.

    Each agent gets UE_SESSION_TICKET_BUDGET=N (so the driver counts and the Stop-hook permits an
    early stop) and UE_SESSION_BUDGET_MARKER pinned to the same path the hook checks."""
    sentinel = (os.environ.get("UE_RUN_INCOMPLETE")
                or os.path.join(repo, ".unattended", "run-incomplete"))
    state_dir = os.path.join(repo, ".unattended", "state")
    marker = (os.environ.get("UE_SESSION_BUDGET_MARKER")
              or os.path.join(state_dir, "session-budget-reached"))
    count_file = os.path.join(state_dir, "session-ticket-count")

    sess_env = dict(child_env)
    sess_env["UE_SESSION_TICKET_BUDGET"] = str(fresh_n)
    sess_env["UE_SESSION_BUDGET_MARKER"] = marker

    # Tell the agent it may stop after its session budget; the launcher resumes it fresh. The prompt
    # is the LAST argv element (agent_argv + args.prompt); augment a copy, never the default path.
    budget_note = (
        f"\n\n[fresh-session budget] Implement up to {fresh_n} ticket(s) this session, then you MAY "
        f"stop — the launcher will resume you in a FRESH session to continue the backlog. Use the "
        f"normal next->implement->complete loop; do not try to drain everything in one session.")
    full_argv = list(full_argv)
    if full_argv:
        full_argv[-1] = full_argv[-1] + budget_note

    log_path = open_log(log_repo or repo, cfg)  # logs land on the primary when isolated (log_repo)
    print(f"Fresh-session mode: respawn every {fresh_n} ticket(s), preset '{preset_name}' "
          f"(supervisor in foreground; lock held for the whole loop)")
    print(f"  log:   {log_path}")

    try:
        for session in range(1, SESSION_RESPAWN_CAP + 1):
            # Reset the per-session counter + early-stop marker BEFORE each spawn: "session start"
            # is defined as counter-absent, and a stale marker would let the new agent stop at once.
            ensure_private_dir(state_dir)
            for p in (count_file, marker):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            # O_NOFOLLOW (L4, post-audit): the log carries the agent's raw, UNREDACTED stream — a
            # pre-planted symlink at log_path must not redirect that write to an attacker-chosen
            # file, same rationale as acquire_lock's own O_NOFOLLOW open.
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(log_fd, 0o600)
            except OSError:
                pass
            with os.fdopen(log_fd, "ab") as logf:
                logf.write(f"\n== ANS fresh session #{session} (budget {fresh_n}) ==\n".encode())
                logf.flush()
                proc = subprocess.Popen(
                    full_argv,
                    stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True, env=sess_env,
                    pass_fds=(lock_fd,) if lock_fd is not None else (),
                )
            rc = proc.wait()
            # Resume-safety HALT is TERMINAL and needs operator action: run.py drops a `resume-halt`
            # marker when a stale/stranger run branch cannot be resumed. Respawning would only HALT
            # again every session until the cap, so stop NOW and point the operator at it. (The
            # driver clears the marker once a fresh/safe run branch is (re)entered.)
            if os.path.exists(os.path.join(state_dir, "resume-halt")):
                print(f"ans-run: HALT after session #{session} (rc={rc}) — a stale/unsafe run branch "
                      f"cannot be resumed. The backlog is stopped pending operator action; inspect "
                      f"{os.path.join(state_dir, 'resume-halt')} and the run report. Not respawning.",
                      file=sys.stderr)
                return EX_NOGO
            # Terminal detection: the driver clears the sentinel on DRAINED/HALTED/LOW_YIELD/
            # STOPPED_CREDITS. Absent => the backlog is done => stop the loop (success).
            if not os.path.exists(sentinel):
                print(f"Fresh-session loop done after {session} session(s) "
                      f"(run reached a terminal state; last agent rc={rc}).")
                return 0
            print(f"Session #{session} ended (rc={rc}); backlog not drained — respawning.")
        print(f"ans-run: fresh-session cap ({SESSION_RESPAWN_CAP}) hit with work remaining — "
              "stopping the loop. Inspect the run; the sentinel is still present.", file=sys.stderr)
        return EX_NOGO
    finally:
        release()


def compose_watchdog_argv(full_argv, heartbeat_path, wd_cfg, per_ticket_timeout_s,
                          fix_iterations=3, *, disabled: bool = False):
    """Wrap the agent argv in the heartbeat watchdog so a DETACHED run survives a sustained
    529/overload wave — which manifests as a FROZEN heartbeat (the process is alive but wedged,
    the gap the Stop-hook can't see). bin/ans-run otherwise spawns a BARE agent whose only
    retries are the SDK's internal ~20×/~135s; a sustained wave exhausts them and the run wedges
    for good, with no external restart (two real 2026-06-12 incidents, one dead ~7.5h). The
    watchdog runs the agent as a child and, on a STALE heartbeat, restarts it RESUMABLE up to a
    cap, then alerts (exit 75). A clean child exit — including a non-zero crash — is passed
    through, NOT restarted (surfaced fast to the caller instead). Default-ON (ticket 03);
    returns full_argv unchanged when disabled (--no-watchdog or launcher.watchdog.enabled=false).

    --stale MUST exceed the real worst-case gap BETWEEN heartbeats or it false-restarts a healthy
    run (→ re-stale → exhaust the cap → exit 75, killing a run that was fine). The agent only
    beats at harness next/complete boundaries, so one ticket's whole implement→gate→review→
    consensus→complete span passes with no beat: per_ticket_timeout_s bounds a SINGLE gate
    subprocess, and a ticket runs up to fix_iterations gate rounds plus review/consensus. So
    default stale ≈ per_ticket_timeout_s × (fix_iterations + 1) + a review/consensus margin.
    Operators with lighter tickets can lower launcher.watchdog.stale_s to catch a hang sooner."""
    wd_cfg = wd_cfg or {}
    if disabled or wd_cfg.get("enabled") is False:
        return list(full_argv)
    budget = int(per_ticket_timeout_s or 1800)
    iters = int(fix_iterations if fix_iterations is not None else 3)
    default_stale = budget * (iters + 1) + 1800   # + 30min review/consensus margin
    stale = int(wd_cfg.get("stale_s") or default_stale)
    early_stale = int(wd_cfg.get("early_stale_s", 1200))
    argv = [sys.executable, "-m", "agents_never_sleep.watchdog",
            "--heartbeat", heartbeat_path,
            "--stale", str(stale),
            "--early-stale", str(early_stale),
            "--max-restarts", str(int(wd_cfg.get("max_restarts", 3))),
            "--poll", str(int(wd_cfg.get("poll_s", 30))),
            "--grace", str(int(wd_cfg.get("grace_s", 300)))]
    alert = wd_cfg.get("alert")
    if alert:
        argv += ["--alert", str(alert)]
    return argv + ["--"] + list(full_argv)


def _claude_json_path() -> str:
    """Location of Claude Code's per-user config (~/.claude.json), overridable via ANS_CLAUDE_JSON for
    tests (parallel to ANS_TRUST_STORE) so the suite never touches the real home file."""
    return os.environ.get("ANS_CLAUDE_JSON") or os.path.expanduser("~/.claude.json")


def ensure_workspace_trusted(repo: str) -> None:
    """Preflight: mark `repo` as a TRUSTED workspace in ~/.claude.json so Claude Code honours the
    project's allow-list instead of raising an interactive 'do you trust this folder?' dialog on the
    first tool call — which HANGS an unattended run (no human is present to click 'trust', so every
    permission prompt blocks forever). This is the field failure where a whole night's run stalled on
    an unanswered prompt.

    Contract: ADDITIVE and IDEMPOTENT — only the target path's `hasTrustDialogAccepted` flag is set
    true; every other project entry and field is preserved byte-for-byte. Writes atomically (temp +
    rename, 0600) and only when a change is actually needed. FAIL-SAFE: any error (missing/creatable
    file, malformed JSON, unwritable home) is logged and swallowed — a trust-write must NEVER abort or
    crash a launch. When the file is absent it is created pre-trusted (else Claude Code would create it
    and show the dialog); when it is present but unparseable it is left UNTOUCHED (never clobber a
    file we cannot safely merge).

    CONCURRENCY: the whole read-modify-write is serialized under an exclusive flock on a sibling
    lock file, so two ans-run launches racing on ~/.claude.json cannot last-writer-wins away each
    other's entry. The lock is process-scoped (released on fd close / process death — never stale),
    and the critical section is a small JSON read+write, so blocking is bounded."""
    path = _claude_json_path()
    lock_fd = None
    try:
        try:
            lock_fd = os.open(path + ".ans-lock", os.O_CREAT | os.O_WRONLY, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            lock_fd = None  # best-effort: proceed unlocked rather than skip the trust-write
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  trust: ~/.claude.json is unreadable ({exc}) — leaving it untouched; a "
                      f"trust dialog may still prompt. Fix the file to auto-trust the workspace.",
                      file=sys.stderr)
                return
            if not isinstance(data, dict):
                print("  trust: ~/.claude.json is not a JSON object — leaving it untouched.",
                      file=sys.stderr)
                return
        else:
            data = {}
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            print("  trust: ~/.claude.json 'projects' is not an object — leaving it untouched.",
                  file=sys.stderr)
            return
        entry = projects.get(repo)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — nothing to write (idempotent)
        if isinstance(entry, dict):
            entry["hasTrustDialogAccepted"] = True
        else:
            projects[repo] = {"hasTrustDialogAccepted": True,
                              "hasCompletedProjectOnboarding": True}
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude.json.ans-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        print(f"  trust: marked workspace {repo} trusted in ~/.claude.json "
              "(so no interactive trust dialog can hang an unattended run)")
    except Exception as exc:  # noqa: BLE001 — a trust-write must never abort a launch
        print(f"  trust: could not auto-trust the workspace ({exc}) — continuing; a trust dialog "
              "may prompt on the first tool call.", file=sys.stderr)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)  # releases the flock


def main() -> int:
    # Subcommand dispatch: `ans-run init ...` / `ans-run install-hooks ...` are recognised
    # subcommands, routed BEFORE the default prompt path (where an unknown first arg becomes
    # the agent prompt). Everything after the subcommand token is passed through verbatim.
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        return init_cmd.run_init(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "install-hooks":
        return init_cmd.run_install_hooks(sys.argv[2:])

    parser = argparse.ArgumentParser(prog="ans-run", add_help=True)
    parser.add_argument("--repo", default=None, help="working tree (default: cwd)")
    parser.add_argument("--agent", default=None,
                        help="named agent preset from launcher.agents")
    parser.add_argument("--fg", action="store_true",
                        help="run in foreground (agent exit code is propagated)")
    parser.add_argument("--no-watchdog", action="store_true",
                        help="do NOT wrap the background launch in the heartbeat watchdog "
                             "(default: wrapped, so a hang/overload restarts the run resumable)")
    parser.add_argument("--check", action="store_true",
                        help="dry run: print the GO/NO-GO preflight report and exit — "
                             "never starts a run, never touches the working tree, never spends a token")
    parser.add_argument("--yes", action="store_true",
                        help="skip the pre-launch confirmation prompt (for CI/automation; the prompt "
                             "is also skipped automatically when stdin isn't a terminal)")
    parser.add_argument("--trust", action="store_true",
                        help="record trust for the repo's current config and exit")
    parser.add_argument("prompt", nargs="*", help="prompt passed to the agent CLI")
    args = parser.parse_args()

    repo = os.path.realpath(args.repo or os.getcwd())
    if not os.path.isdir(repo):
        print(f"ans-run: repo dir not found: {repo}", file=sys.stderr)
        return EX_NOGO

    config_path = os.path.join(repo, CONFIG_REL)
    if args.trust:
        digest = record_trust(repo, config_path)
        if digest is None:
            print(f"ans-run: no {CONFIG_REL} in {repo} — nothing to trust", file=sys.stderr)
            return EX_NOGO
        print(f"Trusted {config_path} (sha256 {digest[:16]}…) for user "
              f"{os.environ.get('USER', '?')}")
        return 0

    # Root-guard before anything else: re-exec when a TRUSTED config configures a target user.
    # An untrusted config's target_user is never acted on here (trusted_target_user returns None)
    # — the ordinary TOFU gate below still runs (still as root) and NO-GOes on an untrusted
    # config, so nothing repo-described ever executes either way.
    if os.geteuid() == 0:
        target = trusted_target_user(repo, config_path)
        if target:
            return reexec_as_target_user(target)  # returns only on failure

    print(f"== ANS preflight — repo: {repo} ==")
    # Auto-trust the workspace so Claude Code never raises an interactive folder-trust dialog that
    # would hang this unattended run (fail-safe; never aborts a launch). Skipped under --check, which
    # is a side-effect-free dry run.
    if not args.check:
        ensure_workspace_trusted(repo)
    rep = Report()
    cfg, config_exists, cfg_digest, full_config = load_launcher_config(repo, rep)

    # TOFU gate FIRST: everything below may execute config-described commands.
    if not check_trust(repo, cfg_digest, rep):
        print("== NO-GO: untrusted config ==")
        return EX_NOGO

    check_identity(cfg, rep)

    # Lock next: a busy tree must not pay for the expensive probes below.
    lock_fd, busy = acquire_lock(repo, rep, probe_only=args.check)
    if busy and not args.check:
        print("== NO-GO: another run is active on this working tree ==")
        print("ans-run: working tree busy — refused", file=sys.stderr)
        return EX_BUSY

    def _release():
        if lock_fd is not None:
            os.close(lock_fd)

    # chdir into the repo BEFORE the probe so the binary the probe resolves is the same
    # one the spawn runs (a preset with a relative PATH entry would otherwise resolve
    # against two different cwds). All check_* helpers take `repo` explicitly, so the
    # early chdir is safe.
    os.chdir(repo)
    agent_argv, agent_env, preset_name = resolve_agent(
        cfg, config_exists, args.agent, rep)
    # A detached launch (no --fg) with an interactively-gated agent hangs on the first tool
    # prompt — inspect the resolved argv, not just the autonomy_confirmed boolean.
    check_detached_permission_mode(agent_argv, args.fg, rep)
    # Non-blocking: is enforcement actually wired for the resolved harness? (Task B, consensus T1)
    check_enforcement_hooks_wired(agent_argv, rep)
    # Token-ref resolution (F4) happens HERE — before the capability probe — so the probe
    # and the spawn see the identical resolved environment.
    agent_env = resolve_preset_env(agent_env, full_config, rep)
    child_env = dict(os.environ)
    child_env.update(agent_env)
    # Freeze the out-of-repo consent snapshot (Part B) into the agent-CLI env, parallel to
    # CLAUDE_UNATTENDED. Done HERE — after the TOFU gate (937) proved the config trusted, and
    # BEFORE any worktree reassignment (1034) / agent spawn — so consent is (a) keyed to the
    # PRIMARY repo where the wizard captured it, (b) frozen before any agent-influenceable step,
    # and (c) present for every spawn path (fg exec, fresh-session loop, watchdog, bare Popen).
    # enforce.py reads consent ONLY from this env var; a child cannot rewrite its parent's env.
    from . import consent_store
    child_env["UE_CONSENT"] = json.dumps((consent_store.read(repo) or {}).get("actions") or {})
    child_env["UE_CONSENT_AUDIT"] = consent_store.audit_path(repo)
    check_agent_binary(agent_argv, agent_env, child_env, cfg, rep)
    check_credentials(cfg, rep)
    check_git(repo, rep)
    check_writable(repo, rep)
    check_disk(repo, cfg, rep)
    run_config_checks(repo, cfg, rep)

    if rep.failed:
        _release()
        print("== NO-GO: fix the ✗ items above before launching ==")
        return EX_NOGO
    print("== GO" + (" (with warnings)" if rep.warned else "") + " ==")

    if args.check:
        _release()
        return EX_BUSY if busy else 0
    if not args.prompt:
        _release()
        print("ans-run: no prompt given", file=sys.stderr)
        return EX_NOGO

    # Preflight passed and we're about to spend tokens: give a nervous first-time operator one last
    # look before it commits. Gated on isatty so a detached/CI/cron launch (no controlling terminal)
    # never blocks waiting for input; --yes skips it explicitly for scripted use.
    if not args.yes and sys.stdin.isatty():
        prompt_preview = " ".join(args.prompt)
        if len(prompt_preview) > 80:
            prompt_preview = prompt_preview[:77] + "..."
        print(f"\n  About to start a REAL run (tokens will be spent):")
        print(f"    repo:   {repo}")
        print(f"    agent:  {preset_name}")
        print(f"    prompt: {prompt_preview}")
        reply = input("\n  Type 'yes' to proceed, or anything else to cancel: ").strip().lower()
        if reply != "yes":
            _release()
            print("ans-run: cancelled by user", file=sys.stderr)
            return EX_NOGO

    full_argv = agent_argv + args.prompt

    # Direction 2 (INT-2674): give this child a unique run-token so it can reliably
    # self-identify during a process check. The tree-flock already guarantees ≤1 live
    # runner per repo, so a same-token match is DEFINITIVELY this child — not a stranger.
    # Token goes into the prompt (visible in /proc/<pid>/cmdline) and into UE_ANS_RUN_TOKEN
    # (visible in the child's env). Both channels are set HERE, upstream of every spawn
    # branch (--fg exec, fresh-session loop, watchdog-wrapped, and bare Popen), so no
    # combination of flags can omit it — same pattern as the M1 CLAUDE_UNATTENDED hardening.
    run_token = str(uuid.uuid4())
    child_env["UE_ANS_RUN_TOKEN"] = run_token
    if full_argv:
        full_argv[-1] = full_argv[-1] + (
            f"\n\n[run-token] Your unique ANS run-token is {run_token} (also in env "
            f"UE_ANS_RUN_TOKEN). The tree-flock guarantees at most one live runner per "
            f"repo — so if a process check finds a claude process or watchdog that carries "
            f"this exact token in its command-line or environment, that IS yourself. "
            f"Do not treat it as a competing worker; start the next→complete loop immediately.")

    # G4b: auto-worktree isolation. When autonomy.live_tree == "auto_worktree", run the whole session
    # in a dedicated EXTERNAL git worktree so the primary/live tree is never touched. The lock stays
    # on the PRIMARY repo (already held); state/sentinel/heartbeat become worktree-relative via the
    # rebind + chdir below, and every re-invoked `ans next`/`ans complete` inherits cwd. Not applied
    # under --fg: exec replaces this process, leaving no place to clean up.
    primary_repo = repo
    worktree = None
    # Default-upgrade (2026-07-19): a DETACHED run on a DIRTY, non-linked primary tree isolates by
    # default so an unattended run can never revert a human's uncommitted work in place (the field
    # incident that reverted a modified .claude/settings.json). An explicit live_tree (auto_worktree/
    # ack/require_isolation) is honored verbatim; --fg / clean / already-linked are never upgraded.
    _autonomy = full_config.setdefault("autonomy", {})
    _configured_policy = _autonomy.get("live_tree")
    _effective_policy = resolve_live_tree_policy(
        _configured_policy,
        is_dirty=tracked_dirt_count(primary_repo) > 0,
        is_foreground=args.fg,
        is_linked=_is_linked_worktree(primary_repo))
    # Announce ONLY a real auto-isolation upgrade (a normalizing None->'warn' is silent).
    if _effective_policy == "auto_worktree" and _configured_policy != "auto_worktree":
        print(f"  live-tree: dirty detached tree — isolating by default (autonomy.live_tree "
              f"{_configured_policy!r} -> 'auto_worktree'); set 'ack' to opt out")
    _autonomy["live_tree"] = _effective_policy
    if should_isolate(full_config, primary_repo):
        if args.fg:
            # Fail-closed (matches the doctrine below): the operator asked for isolation, so running
            # unisolated in the live tree is exactly the hazard to avoid. --fg execs THIS process into
            # the agent (foreground), so we refuse rather than run in-place.
            _release()
            print("== NO-GO: auto_worktree is not supported under --fg — run detached (drop --fg), "
                  "or unset autonomy.live_tree=auto_worktree ==", file=sys.stderr)
            return EX_NOGO
        try:
            worktree = create_isolated_worktree(
                primary_repo, f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
        except WorktreeError as exc:
            _release()
            print(f"== NO-GO: auto_worktree requested but isolation failed: {exc} ==",
                  file=sys.stderr)
            return EX_NOGO  # fail-closed: NEVER fall back to running in the live tree
        os.chdir(worktree)
        repo = worktree  # run-local state/heartbeat/sentinel become worktree-scoped (isolated)
        # But the morning report is ANS's DELIVERABLE — pin it to the PRIMARY tree so it survives the
        # worktree's removal (the driver honors UE_REPORT_PATH). Logs likewise go to the primary
        # (open_log below), so the operator finds both where they expect them.
        report_name = (full_config.get("report") or {}).get("local_path") or "night-report.md"
        child_env["UE_REPORT_PATH"] = os.path.join(primary_repo, report_name)
        # Pin the run-incomplete SENTINEL to the isolated worktree, parallel to the report path (and to
        # UE_SESSION_BUDGET_MARKER, pinned in run_fresh_session_loop). The fresh-session supervisor
        # checks the sentinel at a worktree-relative path; the child driver + Stop-hook resolve it from
        # ${UE_RUN_INCOMPLETE:-<--repo|$PWD>/...}. Without this pin, a child launched with --repo != its
        # CWD (the cron/claude-run case that _sentinel_path_ok guards) would write/read the sentinel on
        # the PRIMARY tree while the supervisor watches the worktree — the loop then mis-detects a
        # terminal after ONE session and never-stop collapses. Pinning here makes all three agree on ONE
        # absolute sentinel inside the worktree, for every detached spawn path (fresh-session loop,
        # watchdog, bare Popen) since they all derive from child_env.
        child_env["UE_RUN_INCOMPLETE"] = os.path.join(worktree, ".unattended", "run-incomplete")
        print(f"  isolated: session runs in a dedicated git worktree — {worktree}")
        print(f"  report:   {child_env['UE_REPORT_PATH']} (kept on your primary tree)")

    if args.fg:
        # exec: this process BECOMES the agent; the inherited FD keeps the lock held for
        # the whole run (os.open sets CLOEXEC per PEP 446, so flip inheritable explicitly)
        # and the agent's exit code propagates naturally.
        if lock_fd is not None:
            os.set_inheritable(lock_fd, True)
        sys.stdout.flush()
        os.execvpe(full_argv[0], full_argv, child_env)
        return 1  # unreachable

    # Every detached path from here on (fresh-session loop, watchdog-wrapped, or a bare direct
    # spawn with --no-watchdog) MUST run with CLAUDE_UNATTENDED=1 so the deny hooks are armed. This
    # used to be set only by watchdog.py's own child env when the watchdog wrapper was in play —
    # --no-watchdog, and fresh-session mode (which never touches watchdog.py at all), could silently
    # launch the agent with enforcement disarmed. Force it here, unconditionally, upstream of every
    # detached branch, so no combination of flags can leave it unset (post-audit hardening, M1).
    child_env["CLAUDE_UNATTENDED"] = "1"

    try:
        fresh_n = int(cfg.get("fresh_session_every") or 0)
    except (TypeError, ValueError):
        fresh_n = 0
    if fresh_n > 0:
        # This supervisor BLOCKS until the backlog drains, so it is safe to clean up the isolated
        # worktree in a finally here (the run is genuinely over on return). The ans/run-* branch lives
        # in the shared store and survives.
        try:
            return run_fresh_session_loop(
                repo, cfg, full_argv, child_env, fresh_n, lock_fd, _release, preset_name,
                log_repo=primary_repo if worktree is not None else None)
        finally:
            if worktree is not None and not cleanup_isolated_worktree(primary_repo, worktree):
                print(f"ans-run: isolated worktree left in place for inspection: {worktree}",
                      file=sys.stderr)

    # Wrap the detached launch in the heartbeat watchdog by default (ticket 03): a bare
    # `claude -p` has NO external restart, so a sustained 529/overload wave that FREEZES the
    # heartbeat wedges the run for good. The watchdog restarts it RESUMABLE on a stale
    # heartbeat, then alerts (a clean/crashed exit is surfaced fast, not restarted). The
    # watchdog holds the tree-lock fd across restarts (its agent children don't inherit it,
    # close_fds), so the lock stays held for the whole run and releases only when it exits.
    watchdog_on = not (args.no_watchdog or (cfg.get("watchdog") or {}).get("enabled") is False)
    # hb_path is the DEFAULT run.py heartbeat location; the watchdog forces UE_HEARTBEAT to it,
    # and run.py prefers UE_HEARTBEAT over its own --state-dir fallback, so the agent always
    # writes where the watchdog polls even if a preset overrode --state-dir.
    hb_path = os.path.join(repo, ".unattended", "state", "heartbeat.json")
    budget_cfg = full_config.get("budget") or {}
    ptt = budget_cfg.get("per_ticket_timeout_s")
    fix_iters = budget_cfg.get("per_ticket_fix_iterations", 3)
    spawn_argv = compose_watchdog_argv(full_argv, hb_path, cfg.get("watchdog") or {}, ptt,
                                       fix_iters, disabled=args.no_watchdog)
    if watchdog_on:
        # `python -m agents_never_sleep.watchdog` runs a FRESH interpreter — make sure the
        # skill package is importable even if the preset sets no PYTHONPATH.
        skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pp = child_env.get("PYTHONPATH", "")
        parts = pp.split(os.pathsep) if pp else []
        if skill_root not in parts:
            child_env["PYTHONPATH"] = os.pathsep.join([skill_root, *parts])

    # Logs go to the PRIMARY when isolated, so they outlive the worktree (which the next launch
    # reclaims); state/heartbeat stay worktree-scoped via `repo`.
    log_path = open_log(primary_repo if worktree is not None else repo, cfg)
    # 0600: the agent's raw, UNREDACTED stream may contain a resolved gateway key — never
    # world-readable. fchmod tightens it even if the file pre-existed looser. O_NOFOLLOW (L4,
    # post-audit): a pre-planted symlink at log_path must not redirect that unredacted write to
    # an attacker-chosen file, same rationale as acquire_lock's own O_NOFOLLOW open.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(log_fd, 0o600)
    except OSError:
        pass
    with os.fdopen(log_fd, "ab") as logf:
        proc = subprocess.Popen(
            spawn_argv,
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=child_env,
            pass_fds=(lock_fd,) if lock_fd is not None else (),
        )
    _release()  # child shares the open file description -> lock stays held by the run
    time.sleep(2)
    early = proc.poll()
    if early is not None and early != 0:
        who = "run supervisor (watchdog)" if watchdog_on else "agent"
        print(f"ans-run: {who} exited immediately (rc={early}) — last log lines:",
              file=sys.stderr)
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[-5:]:
                    print(f"    {line.rstrip()}", file=sys.stderr)
        except OSError:
            pass
        return early
    if watchdog_on:
        print(f"Started in background under the heartbeat watchdog (PID {proc.pid}, preset "
              f"'{preset_name}'; restarts the agent on hang/overload up to the cap, then "
              "alerts). Opt out with --no-watchdog.")
    else:
        print(f"Started in background (PID {proc.pid}, preset '{preset_name}', "
              "lock held by the run process; watchdog OFF)")
    print(f"  log:   {log_path}")
    print(f"  watch: tail -f {log_path}")
    if worktree is not None:
        # A detached run outlives this process, so we cannot remove its worktree here (it is still in
        # use). It is force-removed and recreated fresh on the next auto_worktree launch — bounded to
        # one stale tree. The run's ans/run-* branch is in the shared store, mergeable from primary.
        print(f"  isolated worktree: {worktree} (reclaimed on the next auto_worktree launch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
