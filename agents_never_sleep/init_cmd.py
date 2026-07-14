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


def write_demo_tickets(repo: str, cfg: dict) -> None:
    pass  # implemented in Task 4


def print_enforcement_handoff(harness, repo: str) -> None:
    pass  # implemented in Task 5


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
    raise NotImplementedError  # implemented in Task 5
