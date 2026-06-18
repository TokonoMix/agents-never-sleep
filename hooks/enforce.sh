#!/usr/bin/env bash
# Cross-platform enforcement launcher — the single command every NON-Claude platform hook calls.
#
# A platform's hook config points at:   <SKILL_DIR>/hooks/enforce.sh <platform> <event>
# e.g.                                   .../hooks/enforce.sh cursor pre_tool
#
# It cd's to the skill root (so `python3 -m harness.enforce` can resolve the package) and execs the
# dispatcher, passing stdin through untouched. harness.enforce is itself env-gated (UE_UNATTENDED=1 /
# CLAUDE_UNATTENDED=1) and fails OPEN, so this launcher needs no gating of its own.
# NB: deliberately NOT `set -e`/`exec` — a broken install (python missing, import error) must FAIL
# OPEN, never wedge a tool call. harness.enforce only ever intentionally returns 0 (allow) or 2
# (Windsurf deny); we preserve a real 2 and map any other/crash exit to 0 (allow).
cd "$(dirname "$0")/.." 2>/dev/null || exit 0   # skill root = parent of hooks/
python3 -m harness.enforce "$@"
rc=$?
[ "$rc" = 2 ] && exit 2 || exit 0
