"""Capability detection + graceful-degradation reporting for cross-platform enforcement.

Best-effort enforcement means each platform enforces what its hook system allows; the rest falls back
to the SKILL.md prose contract. This module makes that status EXPLICIT so a missing guarantee is never
silent: it owns the per-platform capability matrix and produces blind-spot notes for the soft-enforced
guarantees, which the driver/report surface as startup notes + morning-report BLIND SPOTs.

The matrix is researched fact (see references/cross-platform-enforcement-design.md), not a guess.
"""
from __future__ import annotations

import os

NATIVE = "native"
DEGRADED = "soft-enforced"

DENY_IRREVERSIBLE = "deny_irreversible"
NEVER_STOP = "never_stop"
NEVER_ASK = "never_ask"
_ORDER = (DENY_IRREVERSIBLE, NEVER_STOP, NEVER_ASK)

# Per-platform guarantee status. 🟡 soft-enforced cells fall back to the prose contract + a blind-spot.
_MATRIX = {
    "claude":   {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: NATIVE,   NEVER_ASK: NATIVE},
    "gemini":   {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: NATIVE,   NEVER_ASK: DEGRADED},
    "codex":    {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: NATIVE,   NEVER_ASK: DEGRADED},
    "copilot":  {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: NATIVE,   NEVER_ASK: NATIVE},
    "cursor":   {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: DEGRADED, NEVER_ASK: DEGRADED},
    "windsurf": {DENY_IRREVERSIBLE: NATIVE, NEVER_STOP: DEGRADED, NEVER_ASK: DEGRADED},
}

_LABEL = {DENY_IRREVERSIBLE: "deny-irreversible", NEVER_STOP: "never-stop", NEVER_ASK: "never-ASK"}
_WHY = {
    NEVER_ASK: "no hook fires before the agent asks the user on this platform",
    NEVER_STOP: "this platform has no stop hook that can block end-of-turn",
    DENY_IRREVERSIBLE: "this platform has no pre-command hook that can deny",
}

SUPPORTED = tuple(_MATRIX)

# Platforms whose NATIVE guarantees are proven to actually fire (hooks live-tested / in use). The
# others are built to each platform's DOCUMENTED contract but not yet smoke-tested on the real tool,
# so a "native" cell there must NOT be reported as proven protection (avoid active false assurance).
LIVE_VERIFIED = frozenset({"claude"})

# The DOCUMENTED hook-contract reference each platform's adapter was built + hermetically tested
# against (see references/cross-platform-enforcement-design.md "Sources", researched 2026-06).
# Recorded so the biggest post-1.0 yank risk — a host changing its hook API out from under us — is
# DETECTABLE: if a real-tool deny ever stops registering, compare that tool's current hook version
# against the one stamped here. This is the cheap guard the stability guarantee leans on
# (SEMVER §D5: ANS's API is Stable; adapter BEHAVIOUR is best-effort vs these recorded versions).
_HOOK_CONTRACT = {
    "claude":   "Claude Code PreToolUse/Stop hooks — 2026-06 documented contract",
    "gemini":   "Gemini CLI settings.json hooks — 2026-06 documented contract",
    "codex":    "Codex CLI hooks.json / config.toml [hooks] — 2026-06 documented contract",
    "copilot":  "Copilot CLI .github/hooks + ask_user tool — 2026-06 documented contract",
    "cursor":   "Cursor .cursor/hooks.json — 2026-06 documented contract",
    "windsurf": "Windsurf hooks.json — 2026-06 documented contract",
}


def hook_contract(platform: str) -> str:
    """The documented hook-contract reference the platform's adapter targets — the version to
    compare against if a real-tool deny ever stops registering (third-party-hook drift guard)."""
    return _HOOK_CONTRACT.get(platform, _HOOK_CONTRACT["claude"])


def detect_platform(env=None) -> str:
    """The platform id. Explicit `UE_PLATFORM` wins (set by the launcher/install); defaults to
    'claude'. We do NOT guess from incidental env vars — an explicit signal or the safe default."""
    env = env or os.environ
    p = (env.get("UE_PLATFORM") or "").strip().lower()
    return p if p in _MATRIX else "claude"


def guarantees(platform: str) -> dict:
    return dict(_MATRIX.get(platform, _MATRIX["claude"]))


def is_native(platform: str, guarantee: str) -> bool:
    return guarantees(platform).get(guarantee) == NATIVE


def degradation_notes(platform: str) -> list:
    """One blind-spot line per guarantee that this platform can't natively enforce."""
    g = guarantees(platform)
    return [f"enforcement on {platform}: {_LABEL[k]} is NOT natively enforced ({_WHY[k]}) — relying "
            f"on the prose contract; review {_LABEL[k]} decisions accordingly."
            for k in _ORDER if g[k] == DEGRADED]


def verification_note(platform: str):
    """For a platform whose NATIVE guarantees aren't yet live-verified, a caveat so the report never
    over-promises: 'native' means built-to-documented-contract, not proven-firing-on-the-real-tool."""
    if platform in LIVE_VERIFIED:
        return None
    native = [_LABEL[k] for k in _ORDER if guarantees(platform)[k] == NATIVE]
    if not native:
        return None
    return (f"enforcement on {platform}: {', '.join(native)} built to {platform}'s DOCUMENTED hook "
            f"contract [{hook_contract(platform)}] but NOT yet live-verified on the real tool — run "
            "the smoke-test (hooks/platforms/README.md) to confirm a deny actually registers.")


def report_notes(platform: str) -> list:
    """All enforcement blind-spots for the morning report: soft-enforced guarantees + the not-yet-verified
    caveat for native-but-unproven platforms."""
    notes = degradation_notes(platform)
    vn = verification_note(platform)
    if vn:
        notes.append(vn)
    return notes


def status_line(platform: str) -> str:
    g = guarantees(platform)
    suffix = " (live-verified)" if platform in LIVE_VERIFIED else " (per docs — NOT live-verified)"
    return f"[enforcement:{platform}] " + " · ".join(f"{_LABEL[k]}={g[k]}" for k in _ORDER) + suffix
