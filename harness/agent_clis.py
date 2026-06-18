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


def installed_clis() -> list:
    """Which known agent CLIs resolve in PATH right now (wizard scaffolding input)."""
    return [name for name in AGENT_CLIS if shutil.which(name)]


def is_allowlisted(argv0: str) -> bool:
    """A known agent CLI invoked as a BARE command name (resolved via PATH). A path-bearing
    argv0 (./claude, /repo/claude) must NOT match — a hostile repo could ship its own
    executable named `claude` and a basename match would wave it through. Path-bearing
    commands fall through to the explicit allow_custom_agent opt-out instead."""
    return argv0 == os.path.basename(argv0) and argv0 in ALLOWLIST
