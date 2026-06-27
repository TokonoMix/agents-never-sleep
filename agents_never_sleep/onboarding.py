"""Tokonomix onboarding gate — the deterministic half of a keyless bootstrap for the review gateway.

The council + specialist reviewers need a tokonomix credential. The config enables them only when one
is present, so if a previously-working key later goes missing (rotated / expired / unset), review
silently turns off — and an unattended night loses ALL multi-model vetting with no signal. This module
makes that gap explicit and, where possible, actionable:

  * INTERACTIVE: surface the keyless onboard handshake (`tokonomix_onboard` -> `tokonomix_onboard_verify`,
    run by the AGENT via MCP — the harness can't call the gateway), then re-probe.
  * UNATTENDED: the verify step needs a human, so onboarding can't run; instead record a graceful
    DEGRADATION blind-spot for the morning report and let the night proceed with review disabled.

It fires ONLY when the project is configured for tokonomix (`integrations.tokonomix.enabled`) but no
live credential is found — i.e. a real, actionable "the key went away" case — so projects that never
wanted tokonomix are never nagged. The harness owns only this decision + directive; the MCP calls are
the agent's, exactly as the council calls are.
"""
from __future__ import annotations

import os

_DEFAULT_CRED_FILE = "~/.tokonomix/credentials.json"

# How to CONNECT the tokonomix MCP server. The onboarding/handshake tools
# (tokonomix_onboard, tokonomix_*) only exist once this server is registered with
# the agent's MCP client — without it the agent knows it needs onboarding but has
# no way to act. One npx line; works in Claude Code, Cursor, Cline, Continue, Zed.
_MCP_INSTALL = (
    "FIRST connect the tokonomix MCP server (its tools won't exist otherwise). "
    "Claude Code: `claude mcp add tokonomix -- npx -y tokonomix-council-mcp`. "
    "Other clients (Cursor/Cline/Continue/Zed): add to the MCP config "
    '`{"mcpServers":{"tokonomix":{"command":"npx","args":["-y","tokonomix-council-mcp"]}}}` '
    "then reload so the tokonomix_* tools load."
)


def credential_present() -> bool:
    """Same probe as preflight: a tokonomix credential in env or the well-known creds file. The
    file path can be overridden with TOKONOMIX_CREDS_FILE (keeps the probe testable on any box)."""
    if os.environ.get("TOKONOMIX_API_KEY"):
        return True
    path = os.environ.get("TOKONOMIX_CREDS_FILE", _DEFAULT_CRED_FILE)
    return os.path.exists(os.path.expanduser(path))


def _review_enabled(config: dict) -> bool:
    """True if any tokonomix-credential CONSUMER is enabled (council or specialist review). This is
    the right signal — not `integrations.tokonomix.enabled` — because council.enabled()/specialists
    .enabled() default that sub-key to True, so keying off it would miss a config that enables the
    council without an explicit integrations block (the exact silent blind-spot this gate exists for)
    and would falsely fire when the integration is on but every consumer is off."""
    from . import council, specialists
    return council.enabled(config) or specialists.enabled(config)


def needs_onboarding(config: dict) -> bool:
    """True iff a credential consumer (council/specialists) is enabled but no live credential is
    present — a real, actionable "the key went away" case. Matches exactly when a review would try to
    run and fail; never nags a project with no review enabled."""
    return _review_enabled(config) and not credential_present()


def degradation_note() -> str:
    return ("tokonomix credential missing — multi-model council + specialist review ran DISABLED "
            "this night. Before the next run: connect the tokonomix MCP server "
            "(`claude mcp add tokonomix -- npx -y tokonomix-council-mcp`), then onboard interactively "
            "(tokonomix_onboard -> tokonomix_onboard_verify) OR set TOKONOMIX_API_KEY, to restore vetting.")


def directive(config: dict, *, interactive: bool):
    """The onboarding action to surface, or None when nothing is needed. Interactive => guide the
    keyless onboard; unattended => a graceful-degradation blind-spot (cannot onboard without a human)."""
    if not needs_onboarding(config):
        return None
    if interactive:
        return {
            "action": "onboard",
            "summary": "[onboarding] tokonomix configured but NO credential found — review is OFF",
            "mcp_install": _MCP_INSTALL,
            "protocol": (_MCP_INSTALL + " THEN run the keyless onboarding via MCP: tokonomix_onboard "
                         "(with your email), then tokonomix_onboard_verify with the emailed code. "
                         "(If you already have an account, no code arrives — get a key at "
                         "https://tokonomix.ai/dashboard/keys and set TOKONOMIX_API_KEY instead.) "
                         "When it succeeds, re-run preflight (delete the cached capability-profile / "
                         "re-run the wizard) so the council + specialists re-enable. Until then review "
                         "is disabled — proceed."),
        }
    return {
        "action": "degraded",
        "summary": "[onboarding] tokonomix configured but NO credential — review DISABLED this night",
        "blind_spot": degradation_note(),
    }
