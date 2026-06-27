#!/usr/bin/env bash
# PreToolUse deny-hook — the ONLY backstop under bypassPermissions.
#
# Blocks genuinely IRREVERSIBLE / outward-facing actions during an unattended run, so the agent's
# 2am judgment (which is exactly what fails) is not the last line of defense. It is:
#   * env-gated: inert unless CLAUDE_UNATTENDED=1 (your normal interactive sessions are untouched),
#   * narrowly scoped: it does NOT block the harness's own reversibility ops (local `git reset
#     --hard` / `git clean` inside a repo) — only destructive/outward things.
#
# Hook contract: reads the PreToolUse JSON on stdin, prints a deny decision to block, exits 0 to allow.
set -euo pipefail

# Inert outside unattended runs.
if [[ "${CLAUDE_UNATTENDED:-}" != "1" ]]; then
  exit 0
fi

payload="$(cat)"

# Pull tool name + the command/string fields we care about (Bash command, file paths).
read -r tool cmd <<EOF
$(printf '%s' "$payload" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("UNKNOWN ");sys.exit(0)
ti=d.get("tool_input") or {}
blob=" ".join(str(ti.get(k,"")) for k in ("command","content","new_string","file_path","path","url"))
print(d.get("tool_name","UNKNOWN"), blob.replace("\n"," "))
')
EOF

deny() {
  printf '%s' "$payload" >/dev/null
  cat <<JSON
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"agents-never-sleep: blocked an irreversible/outward action ($1). Park it for human review instead."}}
JSON
  exit 0
}

# Irreversible / outward patterns. Deliberately NOT matching local `git reset --hard` / `git clean`
# (those are the harness's revert mechanism inside a working tree).
shopt -s nocasematch
case "$cmd" in
  *"git push"*"--force"*|*"git push"*" -f"*|*"git push"*"--force-with-lease"*) deny "force-push" ;;
  *"git push"*":"*|*"git push"*"--delete"*)                                    deny "remote branch/tag delete" ;;
  *"git push --mirror"*)                                                         deny "mirror push" ;;
  *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf \$HOME"*)                                   deny "recursive delete of a root/home path" ;;
  *"drop database"*|*"drop table"*|*"truncate table"*)                          deny "destructive SQL" ;;
  *"mkfs"*|*" dd "*"of=/dev/"*|*"shred "*)                                       deny "disk-destructive command" ;;
  *"vault delete"*|*"vault kv delete"*|*"vault kv destroy"*)                     deny "Vault secret deletion" ;;
  *"vault kv put"*|*"vault write"*"rotate"*)                                     deny "Vault secret write/rotate" ;;
  *"sendmail"*|*"mailx"*|*" mail -s"*)                                           deny "sending real email" ;;
  *"systemctl stop"*|*"systemctl disable"*|*"docker rm "*|*"docker volume rm"*)  deny "service/volume teardown" ;;
esac

# Default: allow.
exit 0
