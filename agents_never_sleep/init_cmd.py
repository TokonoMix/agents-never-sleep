"""ans init — first-touch project onboarding (launch-time-only; never re-invoked mid-run).

Gets a fresh repo to 'a run can start': detect the harness, scaffold a safe project config +
inert example tickets, and hand OFF (never silently perform) enforcement-hook wiring. It writes
only inside <repo>; it never touches host/global config (~/.claude/settings.json etc.).
"""
from __future__ import annotations

import argparse
import os
import subprocess

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

    write_demo_tickets(repo, cfg)          # Task 4
    print_enforcement_handoff(harness, repo)   # Task 5 — harness may be None (generic handoff)
    return EX_OK


def run_install_hooks(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ans-run install-hooks", add_help=True)
    p.add_argument("--harness", default=None, help="claude|codex|gemini|copilot|cursor")
    a = p.parse_args(argv)
    if a.harness and a.harness not in _BLAST_RADIUS:
        print(f"install-hooks: unknown harness {a.harness!r}; known: {', '.join(_BLAST_RADIUS)}")
        return EX_ERR
    # Do NOT default to claude — printing the wrong global path is exactly the footgun this feature
    # exists to prevent. With no --harness, show every target so the user chooses knowingly.
    targets = {a.harness: _BLAST_RADIUS[a.harness]} if a.harness in _BLAST_RADIUS else _BLAST_RADIUS
    print("install-hooks — guided install (diff + confirm) is pending the UX review.")
    if not a.harness:
        print("No --harness given; showing all targets so you pick knowingly:")
    for name, (target, radius) in targets.items():
        print(f"  {name}: {target}  ({radius})")
    print("For now follow the manual steps in hooks/README.md (Claude) or hooks/platforms/README.md")
    print("(other harnesses). Nothing was written.")
    return EX_OK
