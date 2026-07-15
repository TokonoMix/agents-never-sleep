"""ans init — first-touch project onboarding (launch-time-only; never re-invoked mid-run).

Gets a fresh repo to 'a run can start': detect the harness, scaffold a safe project config +
inert example tickets, and hand OFF (never silently perform) enforcement-hook wiring. It writes
only inside <repo>; it never touches host/global config (~/.claude/settings.json etc.).
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import subprocess
import sys
import tempfile

from . import agent_clis, capabilities, config, preflight, trust

EX_OK, EX_ERR = 0, 1


# Maturity preference for the --yes multi-CLI pick: live-verified first, then ALLOWLIST order.
# NB: the agent-CLI name IS the capabilities key ("claude"/"codex"/"gemini"/"copilot") — never
# map to "claude-code" here (see Global Constraints > Capabilities platform key).
def _maturity_rank(name: str) -> int:
    verified = name in capabilities.LIVE_VERIFIED
    return (0 if verified else 1, agent_clis.ALLOWLIST.index(name))


def select_harness(installed, *, assume_yes, ask=input, out=print):
    if not installed:
        return None
    if len(installed) == 1:
        return installed[0]
    ranked = sorted(installed, key=_maturity_rank)
    if assume_yes:
        out(f"Multiple agent CLIs found ({', '.join(installed)}); "
            f"--yes selects the highest-enforcement-maturity one: {ranked[0]}.")
        return ranked[0]
    # Show each candidate WITH its enforcement maturity so the choice is informed (spec decision #2).
    out("Multiple agent CLIs found — choose which runs this backlog:")
    for i, name in enumerate(ranked, 1):
        out(f"  {i}) {name}  —  {capabilities.status_line(name)}")
    try:
        raw = ask(f"Which? [1-{len(ranked)}, default 1] ").strip()
    except EOFError:
        # Non-TTY / closed stdin: never hang — fall back to the highest-maturity default.
        out("(no input available — defaulting to the highest-maturity CLI)")
        return ranked[0]
    if not raw:
        return ranked[0]
    try:
        idx = int(raw)
    except ValueError:
        return ranked[0]
    return ranked[idx - 1] if 1 <= idx <= len(ranked) else ranked[0]


def maturity_lines(platform):
    lines = [capabilities.status_line(platform)]
    for note in capabilities.report_notes(platform):
        lines.append(f"  ⚠️  {note}")
    return lines


def _stdin_is_tty() -> bool:
    """Indirection point so tests can force the interactive-consent branch deterministically
    without a real controlling terminal (same idiom as config.is_interactive, split out here
    because run_init's interactivity test is --yes-aware, not identical to that function)."""
    return sys.stdin.isatty()


def _ask_consent(prompt: str, default: str) -> str:
    """input()-backed consent prompt, EOFError-guarded so a closed/non-TTY stdin mid-prompt can
    never hang the run — falls back to `default` exactly like install_hooks' EOFError handling."""
    try:
        ans = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return ans or default


def offer_consent_preauthorization(repo: str, *, assume_yes: bool) -> None:
    """Onboarding-time consent pre-choice (Task C): offer the SAME per-class "Actions" prompt the
    wizard has, via the shared config.prompt_and_write_consent helper — never a re-implementation.
    Under --yes (non-interactive) or a non-TTY stdin, write NO consent (skip-prompts must not
    silently authorize execution) and print a one-line pointer to re-run interactively."""
    if assume_yes or not _stdin_is_tty():
        print("")
        print("No actions pre-authorized; re-run `ans-run` (the wizard), or `ans-run init` "
              "interactively, to pre-authorize deny-list classes.")
        return
    config.prompt_and_write_consent(repo, ask=_ask_consent)


def _is_git_repo(repo: str) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo,
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, OSError):
        return False


_DEMO_TICKETS = [
    ("01-example-add-readme-badge.md",
     "title: Example — add a build badge to the README\n"
     "expected_outcome: README shows a passing-tests badge\n"
     "blast_radius: docs-only\n",
     "This is an EXAMPLE ticket showing the shape ANS expects. It is NOT run: examples live in\n"
     "`.claude/ans-examples/` which the backlog scanner never reads. Copy a ticket you actually\n"
     "want into `<repo>/tickets/` (the default backlog source) to have ANS work it.\n"),
    ("02-example-fix-flaky-test.md",
     "title: Example — stabilise a flaky test\n"
     "expected_outcome: the named test passes 20x in a row\n"
     "blast_radius: test-only\n",
     "Example only. Describe the failure, the file, and a concrete done-condition ANS can verify.\n"),
    ("03-example-small-refactor.md",
     "title: Example — extract a helper for X\n"
     "expected_outcome: behaviour identical; the new helper has a unit test\n"
     "blast_radius: single-module\n",
     "Example only. Keep tickets small and independently verifiable — one deliverable each.\n"),
]


def write_demo_tickets(repo: str, cfg: dict) -> list[str]:
    # ALWAYS <repo>/.claude/ans-examples — NEVER <repo>/tickets (the backlog source), so an example
    # can never be picked up by load_tickets and run. (Consensus round 2 confirmation.)
    out_dir = os.path.join(repo, ".claude", "ans-examples")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for name, front, body in _DEMO_TICKETS:
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"---\n{front}---\n\n{body}")
        paths.append(path)
    print(f"Wrote {len(paths)} inert example tickets to {out_dir} (reference only — never run).")
    return paths


# Keyed on the agent-CLI / harness name (same convention as capabilities + agent_clis), NOT the
# "claude-code" display label. NOTE: `cursor` (and other enforcement-only platforms) are valid
# install-hooks TARGETS even though they are NOT in agent_clis.ALLOWLIST — ANS enforces on more
# platforms (hooks/platforms/) than it launches. So `init` never selects cursor (its harness comes
# from ALLOWLIST), but `install-hooks --harness cursor` is legitimate.
_BLAST_RADIUS = {
    "claude": ("~/.claude/settings.json", "GLOBAL — affects every Claude Code session"),
    "gemini": ("~/.gemini/settings.json", "GLOBAL — affects every Gemini CLI session"),
    "codex": ("~/.codex/hooks.json", "GLOBAL — affects every Codex CLI session"),
    "copilot": (".github/hooks/ (in this repo)", "PROJECT-LOCAL"),
    "cursor": (".cursor/hooks.json (in this repo)", "PROJECT-LOCAL"),
}

# This install's own root (parent of hooks/), resolved via realpath so a symlinked skill
# install (e.g. ~/.claude/skills/agents-never-sleep -> this checkout) still resolves to the
# real absolute path — the same path substituted into the hook snippets below.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# This module's own directory — the wheel-packaged fallback root. A pip/wheel install ships a
# read-only copy of the hooks/ tree at agents_never_sleep/hooks/ (package-data; see
# pyproject.toml), so `_PACKAGED_HOOKS_ROOT/hooks` is always a real hooks/ dir for a pip install
# even though it has no repo-root hooks/ next to _PACKAGE_ROOT. Kept as its own name (not derived
# from _PACKAGE_ROOT) so the two resolution roots vary independently — including in tests that
# monkeypatch one without the other.
_PACKAGED_HOOKS_ROOT = os.path.dirname(os.path.realpath(__file__))


def _hooks_root() -> str:
    """Root-first, package-fallback resolver for the hooks/ tree. Prefers the repo-root hooks/
    next to this checkout (canonical, editable — identical to pre-packaging behavior, so a
    source-checkout install like the live dev01 one is completely unaffected). Falls back to the
    read-only copy shipped inside the wheel at agents_never_sleep/hooks/ when there is no
    repo-root hooks/ at all, e.g. a pure `pip install agents-never-sleep`. This is the single
    place hooks-tree resolution happens — every root-hooks/ lookup in this module routes through
    it so checkout and wheel installs can never diverge in how a path is built, only in which
    root they land on."""
    checkout_hooks = os.path.join(_PACKAGE_ROOT, "hooks")
    if os.path.isdir(checkout_hooks):
        return checkout_hooks
    return os.path.join(_PACKAGED_HOOKS_ROOT, "hooks")


# Harnesses `install-hooks` can actually MERGE into: a single host settings JSON file at a
# fixed, HOME-relative path, using the nested `hooks: {Event: [{matcher?, hooks:[...]}]}` shape.
# copilot/cursor are project-local (repo-relative, different write model — a drop-in file for
# copilot, a flatter schema for cursor) and this command has no --repo flag, so they are valid
# `--harness` TARGETS (never rejected) but are handed off rather than auto-written (see
# run_install_hooks). Keep this table the single place write-target and detect-target are
# defined, so `hooks_wired` and the writer can never diverge.
_HOME_HARNESS_SETTINGS = {
    "claude": {
        "rel_path": ("claude", "settings.json"),
        "snippet_rel": ("hooks", "settings-snippet.json"),
        "placeholder": "/ABSOLUTE/PATH/TO/agents-never-sleep/",
        "markers": ("stop_guard.sh", "deny_irreversible.sh", "deny_ask.sh"),
    },
    "gemini": {
        "rel_path": ("gemini", "settings.json"),
        "snippet_rel": ("hooks", "platforms", "gemini", "settings.json"),
        "placeholder": "<SKILL_DIR>",
        "markers": ("enforce.sh gemini pre_tool", "enforce.sh gemini stop"),
    },
    "codex": {
        "rel_path": ("codex", "hooks.json"),
        "snippet_rel": ("hooks", "platforms", "codex", "hooks.json"),
        "placeholder": "<SKILL_DIR>",
        "markers": ("enforce.sh codex pre_tool", "enforce.sh codex stop"),
    },
}


def _settings_path(harness: str, *, home: str | None = None) -> str | None:
    """Absolute path to `harness`'s host settings file, or None for a harness install-hooks
    doesn't (yet) merge into. The single source of truth for both the writer and hooks_wired."""
    info = _HOME_HARNESS_SETTINGS.get(harness)
    if info is None:
        return None
    home = home if home is not None else os.path.expanduser("~")
    return os.path.join(home, "." + harness, *info["rel_path"][1:])


def _read_json(path: str):
    """Parsed JSON dict, or None on a missing file / malformed JSON — never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _all_hook_commands(hooks_obj) -> list[str]:
    """Every `command` string anywhere under a settings file's `hooks` subtree."""
    cmds: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            cmd = o.get("command")
            if isinstance(cmd, str):
                cmds.append(cmd)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(hooks_obj)
    return cmds


def _hook_script_exists(cmd: str) -> bool:
    """The script path token of a hook `command` string, checked for existence on disk — a
    settings file can keep naming a script from a moved/deleted ANS install, which is dead
    enforcement even though the reference is intact. The command is either the script invoked
    directly (`<script> [args]`, some platforms append CLI args after it, e.g. 'enforce.sh
    gemini pre_tool') or explicitly via `bash <script> [args]` — the wired form since wheel
    package-data loses the +x bit, so the script can't rely on being directly executable. Skip a
    leading bare `bash` interpreter token before taking the script path, so both forms resolve
    to the same script."""
    tokens = cmd.strip().split()
    if not tokens:
        return False
    idx = 1 if tokens[0] == "bash" and len(tokens) > 1 else 0
    return os.path.isfile(tokens[idx])


def _enforcement_python_can_import() -> bool:
    """True iff `python3` on PATH can `import agents_never_sleep` — the interpreter the wired
    hook scripts actually invoke at enforcement time (`python3 -m agents_never_sleep.enforce ...
    || true`). This is deliberately checked with a FRESH `python3` subprocess rather than trusting
    `sys.executable`/the running interpreter: install-hooks itself always runs from an environment
    that has the package (that's how it got invoked), so checking `sys.executable` would always
    pass and mask the exact gap this exists to catch — a wheel install where Claude Code spawns
    the hook with a *different* python3 than the one this package was installed into, so the
    import fails, `|| true` swallows it, and enforcement silently fails open. Best-effort: any
    failure to even run `python3` (not found, times out) also counts as "can't import"."""
    try:
        r = subprocess.run(["python3", "-c", "import agents_never_sleep"],
                            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def hooks_wired(harness: str, *, home: str | None = None) -> bool:
    """True iff the ANS deny-hooks are already referenced in `harness`'s host settings file
    AND the referenced script still exists on disk. Best-effort: robust to a missing file,
    malformed JSON, an unrelated settings file, or a moved/deleted install (all → False).
    Solid for claude/gemini/codex (fixed HOME-relative settings path); copilot/cursor are
    project-local with no --repo to resolve against here, so they always report False —
    install-hooks hands those off rather than guessing a repo (see run_install_hooks)."""
    info = _HOME_HARNESS_SETTINGS.get(harness)
    if info is None:
        return False
    path = _settings_path(harness, home=home)
    data = _read_json(path)
    if not isinstance(data, dict):
        return False
    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        return False
    commands = _all_hook_commands(hooks_obj)
    for marker in info["markers"]:
        if not any(_hook_script_exists(cmd) for cmd in commands if marker in cmd):
            return False
    return True


def _hooks_source_ready(harness: str) -> bool:
    """True iff this install actually has the hooks/ tree `harness` needs: the snippet file AND
    every hook script its markers reference. False for a pip/wheel install — the wheel packages
    the launcher only, hooks/ (settings-snippet.json + the *.sh/enforce.sh scripts) is a
    source-checkout-only asset (see docs/tutorials/claude-code.md). Checked BEFORE any diff/write
    so a packaged install degrades gracefully instead of a FileNotFoundError traceback."""
    info = _HOME_HARNESS_SETTINGS[harness]
    hooks_dir = _hooks_root()
    # snippet_rel's first component is always "hooks" — hooks_dir already points AT that dir.
    snippet_path = os.path.join(hooks_dir, *info["snippet_rel"][1:])
    if not os.path.isfile(snippet_path):
        return False
    for marker in info["markers"]:
        script = marker.split(" ", 1)[0]   # marker may carry CLI args after the script name
        if not os.path.isfile(os.path.join(hooks_dir, script)):
            return False
    return True


def _load_snippet(harness: str) -> dict | None:
    """The harness's hook snippet with its placeholder replaced by this install's real
    absolute path (resolved from the package location, never a guessed/relative path).
    Returns None (never raises) if the snippet file can't be read — the caller is expected to
    have already checked `_hooks_source_ready`, so this is a belt-and-braces net, not the
    primary guard."""
    info = _HOME_HARNESS_SETTINGS[harness]
    hooks_dir = _hooks_root()
    # snippet_rel's first component is always "hooks" — hooks_dir already points AT that dir.
    snippet_path = os.path.join(hooks_dir, *info["snippet_rel"][1:])
    try:
        with open(snippet_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    placeholder = info["placeholder"]
    # The placeholder templates a "skill root" (parent of hooks/), not hooks_dir itself — the
    # snippet text already spells out "/hooks/<script>" after it. dirname(hooks_dir) is that
    # root under EITHER resolution: the checkout root when hooks_dir is the repo-root hooks/, or
    # this package's own dir when hooks_dir is the wheel-packaged fallback.
    root = os.path.dirname(hooks_dir)
    replacement = (root + "/") if placeholder.endswith("/") else root
    return json.loads(text.replace(placeholder, replacement))


def _merge_hook_group(existing_groups: list, snippet_group: dict) -> None:
    """Merge one snippet hook-group (e.g. {"matcher": "Bash", "hooks": [...]}) into the host's
    existing group list for that event, in place. Groups are matched by `matcher` (None for
    matcher-less events like Stop). Idempotent: a command already present (exact string match —
    stable for a given install) is never duplicated. Never touches unrelated groups/matchers."""
    matcher = snippet_group.get("matcher")
    target = None
    for g in existing_groups:
        if isinstance(g, dict) and g.get("matcher") == matcher:
            target = g
            break
    if target is None:
        existing_groups.append(copy.deepcopy(snippet_group))
        return
    target_hooks = target.setdefault("hooks", [])
    existing_cmds = {h.get("command") for h in target_hooks if isinstance(h, dict)}
    for h in snippet_group.get("hooks", []):
        if h.get("command") not in existing_cmds:
            target_hooks.append(copy.deepcopy(h))
            existing_cmds.add(h.get("command"))


def _merge_settings(existing: dict, snippet: dict) -> dict:
    """Merge `snippet`'s hooks into a deep copy of `existing`. Touches ONLY the `hooks` key —
    `permissions` and every other top-level key survive byte-for-byte, as does any pre-existing
    hook whose matcher/event the snippet doesn't touch."""
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    merged_hooks = merged.get("hooks")
    if not isinstance(merged_hooks, dict):
        merged_hooks = {}
        merged["hooks"] = merged_hooks
    for event, groups in snippet.get("hooks", {}).items():
        existing_groups = merged_hooks.get(event)
        if not isinstance(existing_groups, list):
            existing_groups = []
            merged_hooks[event] = existing_groups
        for group in groups:
            _merge_hook_group(existing_groups, group)
    return merged


def _pretty_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _write_json_atomic(path: str, text: str) -> None:
    """temp file in the SAME directory + fsync + os.replace — same pattern as trust.py/config.py
    so a crash mid-write can never leave partially-written host settings."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".ans-install-hooks-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def print_enforcement_handoff(harness, repo: str) -> None:
    print("")
    print("Enforcement (never-ASK / deny-irreversible / never-stop) is NOT wired by init.")
    if harness and harness in _BLAST_RADIUS:
        target, radius = _BLAST_RADIUS[harness]
        print(f"  Target for {harness}: {target}")
        print(f"  Blast radius: {radius}")
        print(f"  Wire it as a deliberate, reversible step:  ans-run install-hooks --harness {harness}")
    else:
        # No harness selected (none installed): stay generic — never name a Claude path we can't justify.
        print("  No agent CLI selected — once you install one, wire enforcement with:")
        print("    ans-run install-hooks --harness <claude|codex|gemini|copilot>")
    print("  (init never touches host/global config — this is on purpose.)")


def run_init(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ans-run init", add_help=True)
    p.add_argument("--repo", default=None)
    p.add_argument("--yes", action="store_true", help="non-interactive; safe defaults, no prompts")
    p.add_argument("--force", action="store_true", help="overwrite an existing project config")
    p.add_argument("--trust", action="store_true",
                   help="also record TOFU trust so a detached run can start immediately (CI one-shot: "
                        "`ans init --yes --trust`). Under --yes trust is NOT recorded unless this is set.")
    a = p.parse_args(argv)

    repo = os.path.realpath(a.repo or os.getcwd())
    if not _is_git_repo(repo):
        print(f"ans init: {repo} is not a git repository — ANS is git-backed for reversibility. "
              "Run `git init` first.")
        return EX_ERR

    cfg_path = config.config_path(repo)
    if os.path.exists(cfg_path) and not a.force:
        print(f"ans init: config already present at {cfg_path} — use --force to overwrite, "
              "or `ans-run` (the wizard) to adjust it. Nothing changed.")
        return EX_ERR

    profile = preflight.run_preflight(repo, unattended=a.yes)
    # installed_clis() today returns only AGENT_CLIS keys (== ALLOWLIST), so every name is a valid
    # capabilities key + ALLOWLIST.index() arg. Filter defensively anyway so a FUTURE change to
    # installed_clis (e.g. adding an enforcement-only tool like cursor) can never feed _maturity_rank
    # or scaffold_preset a non-ALLOWLIST name -> ValueError. (Consensus round 2 blocker.)
    installed = [n for n in agent_clis.installed_clis() if n in agent_clis.ALLOWLIST]
    harness = select_harness(installed, assume_yes=a.yes)   # may be None

    print(f"== ans init — {repo} ==")
    detected = [g[0] for g in profile.gates]
    if detected:
        print(f"Detected test/quality gates: {detected}")
        print("  ⚠️  GUESS — confirm these are your REAL suite before an unattended run "
              "(edit gates in the config, or use `ans-run` the wizard).")
    else:
        print("No test gate detected — set gates manually in the config before a real run.")

    # Maturity notice ONLY when a real harness was selected. When none is installed we must NOT
    # fabricate a Claude identity (consensus finding #1): print a generic warn+hint instead.
    if harness:
        for line in maturity_lines(harness):   # harness name IS the capabilities key
            print(line)
    else:
        print("No agent CLI found in PATH (claude/codex/gemini/copilot). Config is scaffolded and "
              "otherwise ready — install a CLI and re-run `ans init --force`, or set launcher.agents "
              "manually. (No enforcement maturity to report without a harness.)")

    cfg = config.default_config(profile)   # NB: pre-seeds launcher.agents["managed"] (gateway tier)
    for name in installed:                 # ...we ADD one preset per installed CLI alongside it.
        cfg["launcher"]["agents"][name] = agent_clis.scaffold_preset(name, confirmed=False)
    cfg["launcher"]["default_agent"] = harness   # None when no CLI installed (schema allows null)
    config.save_config(repo, cfg)
    print(f"Wrote {cfg_path} (safe defaults; autonomy stays OFF until you confirm via `ans-run`).")

    # TOFU trust: a detached run NO-GOes on an untrusted config (launcher check_trust). record_trust
    # writes ONLY the ANS trust store ~/.config/agents-never-sleep/trusted.json — never a harness
    # config, and it resolves HOME at call time (verified). Record it when a tty human authored this
    # (interactive), OR when the operator explicitly opts in with --trust (the CI one-shot). Under a
    # bare --yes we do NOT auto-trust — "skip prompts" must not silently mean "authorize execution".
    if (not a.yes) or a.trust:
        trust.record_trust(repo, cfg_path)
        if harness:
            print("Recorded trust for this config. A detached run can start.")
        else:
            print("Recorded trust for this config. Install an agent CLI and re-run `ans init --force` "
                  "before a detached run can start.")   # don't claim a run can start with no CLI
    else:
        print("Config not yet trusted — before a DETACHED run: `ans-run --trust --repo <repo>` "
              "(or re-run `ans init --yes --trust`).")

    offer_consent_preauthorization(repo, assume_yes=a.yes)   # Task C
    write_demo_tickets(repo, cfg)          # Task 4
    print_enforcement_handoff(harness, repo)   # Task 5 — harness may be None (generic handoff)
    return EX_OK


def run_install_hooks(argv: list[str], *, ask=input, out=print) -> int:
    """Wire the ANS deny-hooks into `--harness`'s host settings file: diff + one confirmation,
    atomic write, then verify `hooks_wired()` is True. No default --harness (consensus #5) — with
    none given, list every valid target and exit; an unknown harness is rejected. Blast radius:
    touches ONLY the named harness's settings file, never any other."""
    p = argparse.ArgumentParser(prog="ans-run install-hooks", add_help=True)
    p.add_argument("--harness", default=None, help="claude|codex|gemini|copilot|cursor")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt (CI one-shot; still diffs first)")
    a = p.parse_args(argv)
    if a.harness and a.harness not in _BLAST_RADIUS:
        out(f"install-hooks: unknown harness {a.harness!r}; known: {', '.join(_BLAST_RADIUS)}")
        return EX_ERR
    if not a.harness:
        # Do NOT default to claude — printing/writing the wrong global path is exactly the footgun
        # this feature exists to prevent. With no --harness, show every target so the user picks
        # knowingly; nothing is written.
        out("install-hooks — pick a target so the right config is touched:")
        for name, (target, radius) in _BLAST_RADIUS.items():
            out(f"  {name}: {target}  ({radius})")
        out("Usage: ans-run install-hooks --harness <name> [--yes]")
        return EX_OK

    harness = a.harness
    target, radius = _BLAST_RADIUS[harness]

    if harness not in _HOME_HARNESS_SETTINGS:
        # Accepted target, but project-local with a different write model (drop-in file for
        # copilot; a flatter schema for cursor) and this command has no --repo — hand off rather
        # than guess a repo path or half-write a schema this task didn't verify end-to-end.
        out(f"install-hooks: {harness} is a valid target ({target}, {radius}) but its config is "
            f"project-local — automatic merge isn't wired up for it yet. Follow the manual steps "
            f"in hooks/platforms/README.md.")
        return EX_OK

    if hooks_wired(harness):
        out(f"install-hooks: {harness} is already wired ({target}) — nothing to do.")
        return EX_OK

    if not _hooks_source_ready(harness):
        out(f"install-hooks: needs the ANS source checkout — the enforcement hook scripts "
            f"(hooks/*.sh) are not present in this install. A pip/wheel install ships the "
            f"launcher only — use the git checkout (or the installed Agent Skill) to wire hooks. "
            f"See docs/tutorials/claude-code.md.")
        return EX_ERR

    path = _settings_path(harness)
    existing = _read_json(path) or {}
    snippet = _load_snippet(harness)
    if snippet is None:
        out(f"install-hooks: could not read the hook snippet for {harness} — nothing written. "
            f"See docs/tutorials/claude-code.md.")
        return EX_ERR
    merged = _merge_settings(existing, snippet)

    before_text = _pretty_json(existing)
    after_text = _pretty_json(merged)
    diff = list(difflib.unified_diff(before_text.splitlines(keepends=True),
                                      after_text.splitlines(keepends=True),
                                      fromfile=path, tofile=path))
    out(f"install-hooks — target: {path}  ({radius})")
    out("".join(diff) if diff else "(no changes)")

    if not a.yes:
        # Never hang: only prompt on a real TTY. Non-interactive (CI, this test suite, a piped
        # invocation) with no --yes declines and returns promptly — nothing is written.
        if not sys.stdin.isatty():
            out("install-hooks: non-interactive and no --yes — declining to write. "
                "Re-run with --yes to confirm non-interactively.")
            return EX_OK
        try:
            reply = ask("Write these hooks into the settings above? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            out("install-hooks: not confirmed — nothing written.")
            return EX_OK

    _write_json_atomic(path, after_text)

    if not hooks_wired(harness):
        out(f"install-hooks: wrote {path} but hooks_wired({harness!r}) is still False — "
            f"something's off; please inspect {path} by hand.")
        return EX_ERR
    out(f"✓ enforcement hooks wired for {harness}")
    if _enforcement_python_can_import():
        out("✓ enforcement check: this system's `python3` can import agents_never_sleep — "
            "enforcement will run.")
    else:
        out("WARNING: enforcement may FAIL OPEN — the `python3` on PATH here cannot "
            "`import agents_never_sleep`. The wired hooks run `python3 -m "
            "agents_never_sleep.enforce ... || true`; if the harness invokes that hook with a "
            "python3 that can't see this package, the import fails, `|| true` swallows it, and "
            "dangerous commands are ALLOWED with no error. Run ans-run (and the harness it "
            "launches) from the same environment/venv where agents-never-sleep is installed, or "
            "use the source-checkout install, to close this gap.")
    return EX_OK
