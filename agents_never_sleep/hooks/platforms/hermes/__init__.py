"""ANS Hermes plugin entrypoint — install into ~/.hermes/plugins/agents-never-sleep/.

Thin wiring only. The enforcement logic lives in the installed `agents_never_sleep` package
(agents_never_sleep.hermes_plugin.ans_pre_tool) so it stays single-sourced with the rest of
the skill and is hermetically testable (acceptance/test_enforce_hermes.py). This file just
registers it on Hermes's `pre_tool_call` hook.

Requires `agents_never_sleep` importable in Hermes's Python env
(`pip install agents-never-sleep` — or `pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.1.0`).
See README.md for install + the plugins.enabled opt-in.
"""
from __future__ import annotations


def register(ctx) -> None:
    """Hermes plugin entrypoint. `ctx.register_hook(event, cb)` is Hermes's plugin API
    (hermes_cli.plugins.PluginContext.register_hook). The callback returns
    {"action": "block", "message": ...} to deny a tool, honoured by
    get_pre_tool_call_block_message (first valid block directive wins)."""
    from agents_never_sleep.hermes_plugin import ans_pre_tool

    ctx.register_hook("pre_tool_call", ans_pre_tool)
