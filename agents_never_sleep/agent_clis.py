"""Single source of truth for agent-CLI invocations (launcher + wizard import this).

Why one module: the platform→invocation map encodes two independently-rotting facts
(which binary, which flags) — keeping copies in the wizard and the launcher guarantees
silent divergence. Both import THIS table; update it in one place when a CLI changes.

Two invocation variants per CLI, never merged silently:
  * cmd_safe       — runs the prompt but leaves the CLI's own permission system fully on.
                     Under a detached run (stdin closed, nobody watching) this can stall on
                     the first approval prompt — safe, but possibly useless.
  * cmd_unattended — adds the CLI's autonomy flag so tool calls are not interactively
                     gated. POLICY: this variant is NEVER applied silently. The wizard
                     shows `grants` (what the flag actually allows) and the human must
                     explicitly confirm; only then does a preset record autonomy_confirmed.

Flag reality check (2026-06, verified against vendor docs — re-verify when bumping):
  claude  -p alone still enforces permissions; acceptEdits auto-approves file edits only.
  codex   exec defaults to on-request approvals → fails non-interactive without --sandbox.
  gemini  --yolo auto-approves everything; there is no edits-only middle tier for -p.
  copilot -p requires --allow-all-tools to run programmatically.
"""
from __future__ import annotations

import os
import shutil

# argv[0] values the launcher will exec without `allow_custom_agent` (basename match).
ALLOWLIST = ("claude", "codex", "gemini", "copilot")

AGENT_CLIS = {
    "claude": {
        "cmd_safe": ["claude", "-p"],
        "cmd_unattended": ["claude", "-p", "--permission-mode", "acceptEdits"],
        "grants": ("auto-approves FILE EDITS; shell commands and network stay gated "
                   "(full bypass would be --dangerously-skip-permissions — not suggested)"),
        "version_args": ["--version"],
    },
    "codex": {
        "cmd_safe": ["codex", "exec"],
        "cmd_unattended": ["codex", "exec", "--sandbox", "workspace-write"],
        "grants": ("auto-approves edits/commands INSIDE the workspace sandbox; "
                   "network and out-of-tree writes stay blocked"),
        "version_args": ["--version"],
    },
    "gemini": {
        "cmd_safe": ["gemini", "-p"],
        "cmd_unattended": ["gemini", "--yolo", "-p"],
        "grants": ("auto-approves ALL tool calls — file writes, shell AND network; "
                   "run it in a container/VM or on a throwaway checkout"),
        "version_args": ["--version"],
    },
    "copilot": {
        "cmd_unattended": ["copilot", "--allow-all-tools", "-p"],
        "cmd_safe": ["copilot", "-p"],
        "grants": ("auto-approves ALL tool calls (--allow-all-tools is required for "
                   "programmatic -p use); treat like full bypass"),
        "version_args": ["--version"],
    },
}

# Per-CLI argv markers whose presence means tool calls are NOT interactively gated — a
# DETACHED run (stdin closed) won't stall on an approval prompt. Kept in lockstep with the
# cmd_unattended vs cmd_safe delta above: cmd_unattended carries exactly these, cmd_safe none.
# "flags" = bare autonomy flags; "pairs" = flag→accepted-values (a "--flag value" or
# "--flag=value" whose value is non-interactive). Re-verify when a CLI's flags change.
NONINTERACTIVE_MARKERS = {
    "claude": {"flags": {"--dangerously-skip-permissions"},
               "pairs": {"--permission-mode": {"acceptEdits", "bypassPermissions"}}},
    "codex": {"flags": set(),
              "pairs": {"--sandbox": {"workspace-write", "danger-full-access"}}},
    "gemini": {"flags": {"--yolo"}, "pairs": {}},
    "copilot": {"flags": {"--allow-all-tools"}, "pairs": {}},
}


def cli_for_argv(argv) -> str:
    """Best-effort agent-CLI name from a resolved argv's basename; '' if unknown/custom.
    A path-bearing argv0 still matches by basename here — this is a policy hint, not the
    exec allowlist (is_allowlisted is the security gate for what may be executed)."""
    if not argv:
        return ""
    base = os.path.basename(str(argv[0]))
    return base if base in AGENT_CLIS else ""


def is_noninteractive_permission(argv, cli: str = "") -> "bool | None":
    """Does argv carry the CLI's non-interactive permission flag (safe to run detached)?

    True  = a recognized autonomy flag/pair is present (won't stall on a tool prompt).
    False = the CLI is known but argv has none of its autonomy markers (cmd_safe-style →
            a detached run would hang on the first approval prompt).
    None  = unknown/custom CLI — cannot judge from the table; the caller decides.
    """
    cli = cli or cli_for_argv(argv)
    spec = NONINTERACTIVE_MARKERS.get(cli)
    if spec is None:
        return None
    tokens = [str(a) for a in (argv or [])]
    if any(flag in tokens for flag in spec["flags"]):
        return True
    for flag, values in spec["pairs"].items():
        for i, tok in enumerate(tokens):
            if tok == flag and i + 1 < len(tokens) and tokens[i + 1] in values:
                return True
            if tok.startswith(flag + "=") and tok.split("=", 1)[1] in values:
                return True
    return False


# Session env markers, EXPLICIT keys only (no substring scans, no API-key heuristics —
# an exported GEMINI_API_KEY does not mean the session runs inside Gemini CLI). Used as
# a wizard-prefill HINT only; never to select a spawn target at launch time.
SESSION_MARKERS = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SANDBOX", "OPENAI_CODEX"),
    "gemini": ("GEMINI_CLI", "GEMINI_CLI_SESSION"),
    "copilot": ("COPILOT_AGENT", "GITHUB_COPILOT_CLI"),
}


def detect_session_platform(env=None) -> str:
    """Best-effort hint for wizard prefill. UE_PLATFORM (the explicit override the
    harness already honors) always wins; otherwise explicit marker keys; else ''."""
    env = os.environ if env is None else env
    explicit = (env.get("UE_PLATFORM") or "").strip().lower()
    if explicit in AGENT_CLIS:
        return explicit
    for platform, keys in SESSION_MARKERS.items():
        if any(env.get(k) for k in keys):
            return platform
    return ""


def scaffold_preset(name: str, *, confirmed: bool) -> dict:
    """The single source of a launcher-preset row. `confirmed` -> the unattended-autonomy cmd; else the
    safe cmd (autonomy is never enabled without an explicit human confirm)."""
    spec = AGENT_CLIS[name]
    return {"cmd": spec["cmd_unattended"] if confirmed else spec["cmd_safe"],
            "autonomy_confirmed": confirmed, "env": {}}


def installed_clis() -> list:
    """Which known agent CLIs resolve in PATH right now (wizard scaffolding input)."""
    return [name for name in AGENT_CLIS if shutil.which(name)]


def is_allowlisted(argv0: str) -> bool:
    """A known agent CLI invoked as a BARE command name (resolved via PATH). A path-bearing
    argv0 (./claude, /repo/claude) must NOT match — a hostile repo could ship its own
    executable named `claude` and a basename match would wave it through. Path-bearing
    commands fall through to the explicit allow_custom_agent opt-out instead."""
    return argv0 == os.path.basename(argv0) and argv0 in ALLOWLIST
