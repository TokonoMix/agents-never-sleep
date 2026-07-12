#!/usr/bin/env bash
# PreToolUse deny-hook — the ONLY backstop under bypassPermissions.
#
# Blocks genuinely IRREVERSIBLE / outward-facing actions during an unattended run, so the agent's
# 2am judgment (which is exactly what fails) is not the last line of defense. It is:
#   * env-gated: inert unless CLAUDE_UNATTENDED=1 (your normal interactive sessions are untouched),
#   * narrowly scoped: it does NOT block the harness's own reversibility ops (local `git reset
#     --hard` / `git clean` inside a repo) — only destructive/outward things.
#
# This file used to keep its own bash case/glob copy of the irreversible pattern list, which could
# drift from the Python copy every other platform's hook shares. It now delegates the actual decision
# to `agents_never_sleep.enforcement` (the single source of truth) via the same cross-platform
# dispatcher every non-Claude hook already calls (hooks/enforce.sh) — there is only one pattern list
# left to maintain, and it is regex-based (robust to whitespace, flag reordering/bundling, and
# long- vs short-flag spelling; see enforcement.py's docstring for what it still can't catch).
#
# Hook contract: reads the PreToolUse JSON on stdin, prints a deny decision to block, exits 0 to allow.
set -euo pipefail

cd "$(dirname "$0")/.." 2>/dev/null || exit 0   # skill root = parent of hooks/, for `python3 -m`
# A broken install (python missing, import error) must FAIL OPEN, never wedge a tool call.
# `enforce.py claude pre_tool` carries its DENY in stdout JSON and only ever exits 0 by intent, so the
# `|| true` swallows a CRASH's non-zero — which `set -e` would otherwise propagate, making the `exit 0`
# below dead code. Fail-open then holds regardless of the harness's own exit-code handling.
python3 -m agents_never_sleep.enforce claude pre_tool || true
exit 0
