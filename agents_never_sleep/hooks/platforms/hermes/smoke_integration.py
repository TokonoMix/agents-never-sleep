#!/usr/bin/env python3
"""Hermes INTEGRATION smoke-test — run on the host where Hermes is installed.

Exercises the ANS plugin against Hermes's REAL plugin machinery (PluginContext.register_hook +
get_pre_tool_call_block_message), proving the deny + never-ASK flow end-to-end through Hermes's
own dispatch — not the hermetic stand-in in acceptance/test_enforce_hermes.py.

NOT a hermetic acceptance test (it needs Hermes on disk), so it is deliberately NOT named
test_*.py and is excluded from run_all.sh. Skips CLEANLY (exit 0) if Hermes is not present.

Not covered (install/config, do this separately): the systemd gateway auto-discovering the
plugin from ~/.hermes/plugins/ and the plugins.enabled gate — that is the deploy step.

Usage:  python3 hooks/platforms/hermes/smoke_integration.py
Env:    HERMES_CODE=/path/to/hermes/code   (default /opt/your-orchestrator/code)
"""
import importlib.util
import os
import sys

HERMES = os.environ.get("HERMES_CODE", "/opt/your-orchestrator/code")
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))  # .../agents-never-sleep

if not os.path.isdir(HERMES):
    print(f"SKIP: Hermes not found at {HERMES} (set HERMES_CODE). Nothing to smoke here.")
    sys.exit(0)

sys.path.insert(0, HERMES)
sys.path.insert(0, SKILL)

try:
    import hermes_cli.plugins as P  # real Hermes plugin module
except Exception as exc:  # pragma: no cover - environment dependent
    print(f"SKIP: could not import Hermes plugins module ({exc}).")
    sys.exit(0)

fails = []

# Register ANS's ACTUAL entrypoint via Hermes's REAL PluginContext -> the global manager.
mgr = P.get_plugin_manager()
ctx = P.PluginContext(P.PluginManifest(name="agents-never-sleep"), mgr)
spec = importlib.util.spec_from_file_location("ans_hermes_entry", os.path.join(HERE, "__init__.py"))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
entry.register(ctx)

if not mgr._hooks.get("pre_tool_call"):
    fails.append("plugin did not register a pre_tool_call hook into Hermes's manager")


def block(tool, args):
    return P.get_pre_tool_call_block_message(tool, args)


os.environ.pop("CLAUDE_UNATTENDED", None)
os.environ["UE_UNATTENDED"] = "1"

cases = [
    ("force-push", "terminal", {"command": "git push --force origin main"}, "irreversible"),
    ("clarify (never-ASK)", "clarify", {"question": "which approach?"}, "PARK"),
    ("nested rm -rf", "some_tool", {"params": {"shell": "rm -rf /home"}}, "irreversible"),
]
for label, tool, args, want in cases:
    m = block(tool, args)
    ok = bool(m and want in m)
    print(f"{'OK  ' if ok else 'FAIL'} {label} -> {m!r}")
    if not ok:
        fails.append(f"{label} not blocked")

m = block("terminal", {"command": "ls -la"})
print(f"{'OK  ' if m is None else 'FAIL'} benign ls -> {m!r}")
if m is not None:
    fails.append("benign command was blocked")

os.environ.pop("UE_UNATTENDED", None)
m = block("terminal", {"command": "git push --force origin main"})
print(f"{'OK  ' if m is None else 'FAIL'} inert-when-not-unattended -> {m!r}")
if m is not None:
    fails.append("not inert outside an unattended run")

print("=" * 60)
if fails:
    print("RESULT: RED — Hermes integration smoke FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("RESULT: GREEN — ANS deny + never-ASK flow end-to-end through Hermes's real dispatch")
