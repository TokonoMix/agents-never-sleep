#!/usr/bin/env bash
# Install this clone's local git pre-commit redaction hook — one command, idempotent.
#
# Symlinks the tracked `scripts/hooks/pre-commit` into this clone's git hooks directory, so the
# public-surface redaction gate (`scripts/redact_lint.sh`) runs at COMMIT time, mirroring the CI
# surface-parity gate. A leak is then caught before it ever enters local history, not only after
# push. Plain bash, no framework, no dependencies. Re-runnable. Worktree- / core.hooksPath-aware.
#
#   bash scripts/install-git-hooks.sh          # install (refuses to clobber a foreign hook)
#   bash scripts/install-git-hooks.sh --force  # replace whatever pre-commit is already there
#
# NOTE: git hooks are never distributed by `git clone` and `--no-verify` bypasses them, so this
# local hook is the early catch — CI remains the authoritative, non-bypassable gate.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
root="$(pwd)"
src="$root/scripts/hooks/pre-commit"
hooks_dir="$(git rev-parse --git-path hooks)"   # resolves worktrees and core.hooksPath
target="$hooks_dir/pre-commit"

if [ ! -f "$src" ]; then
  echo "install-git-hooks: source hook missing: $src" >&2
  exit 1
fi
mkdir -p "$hooks_dir"

force=0
[ "${1:-}" = "--force" ] && force=1

if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$(readlink -f "$src")" ]; then
  echo "install-git-hooks: already installed ($target -> scripts/hooks/pre-commit)"
  exit 0
fi
if { [ -e "$target" ] || [ -L "$target" ]; } && [ "$force" != 1 ]; then
  echo "install-git-hooks: refusing to overwrite existing $target (not ours)." >&2
  echo "  Inspect it, then re-run with --force to replace it." >&2
  exit 1
fi

chmod +x "$src" 2>/dev/null || true
ln -snf "$src" "$target"
echo "install-git-hooks: installed $target -> scripts/hooks/pre-commit"
echo "  commits now run scripts/redact_lint.sh first (mirrors CI surface-parity)."
