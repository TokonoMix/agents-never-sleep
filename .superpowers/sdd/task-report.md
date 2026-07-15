# Task report — ship the `hooks/` tree in the wheel

Status: **DONE**

Worktree: `/var/www/projects/iipnprojects/agents-never-sleep-wt-wheelhooks`
Branch: `feat/package-hooks-in-wheel`

## What changed

1. **`agents_never_sleep/init_cmd.py`** — added `_PACKAGED_HOOKS_ROOT` (this module's own
   directory) and a single resolver `_hooks_root()`: returns the repo-root `hooks/` dir when it
   exists (checkout — root wins, identical to pre-existing behavior), else the wheel-packaged
   `agents_never_sleep/hooks/` fallback. Routed `_hooks_source_ready()` and `_load_snippet()`
   through it (both previously hardcoded `_PACKAGE_ROOT`). Fixed `_hook_script_exists()` to skip
   a leading bare `bash` interpreter token before taking the script-path token (needed because the
   wired command form changed — see next point).
2. **Exec-bit independence** — changed every hook-wiring command template under `hooks/` from
   direct invocation (`<script> [args]`, requires `+x`) to explicit `bash <script> [args]`, so a
   wheel install (package-data loses the `+x` bit) still runs:
   `hooks/settings-snippet.json` (claude — 3 commands), `hooks/platforms/gemini/settings.json`,
   `hooks/platforms/codex/hooks.json` (both auto-wired by `install-hooks`, load-bearing), plus the
   four manual-copy platforms for consistency: `windsurf/hooks.json`,
   `copilot/agents-never-sleep.json`, `crush/crush.json`, `cursor/hooks.json`. The `.sh` scripts
   themselves (`deny_ask.sh`, `deny_irreversible.sh`, `enforce.sh`, `stop_guard.sh`) are
   **byte-untouched** — only the wiring metadata changed, so checkout enforcement behavior is
   identical (a script with a `#!/usr/bin/env bash` shebang behaves the same invoked directly or
   via `bash script`).
   - Also updated the stale `chmod +x` install instructions this obsoletes:
     `hooks/README.md`, `hooks/platforms/README.md`, `hooks/platforms/crush/README.md`.
   - **Backward-compat / upgrade path traced**: a pre-existing checkout install that already wired
     hooks in the OLD (no-`bash`) form is unaffected — `hooks_wired()` short-circuits
     `run_install_hooks()` to a no-op once already wired, and `_hook_script_exists()` never
     checked the `+x` bit (only file existence), so the old entries keep denying exactly as
     before. No duplicate entries are produced on a re-run.
3. **`agents_never_sleep/hooks/`** (NEW, committed) — a full copy of the (now-edited) repo-root
   `hooks/` tree, 21 files, shipped as wheel package-data. This is the fallback source
   `_hooks_root()` resolves to when there's no repo-root `hooks/` next to the install (i.e. a pip
   install). Never the canonical copy — repo-root `hooks/` still wins whenever it exists.
4. **`pyproject.toml`** — `[tool.setuptools.package-data]` for `agents_never_sleep` now includes
   `"hooks/**"` alongside the existing `"py.typed"`.
5. **`acceptance/test_hooks_packaged.py`** (NEW) — drift guard: asserts the packaged
   `agents_never_sleep/hooks/` tree exists, has the identical file set to repo-root `hooks/`, and
   every file is byte-identical (via `filecmp.cmp`), failing loudly naming the offending file(s).
6. **`acceptance/test_install_hooks.py`** — added:
   - `test_hooks_root_prefers_checkout_when_present` (root-first contract)
   - `test_hooks_root_falls_back_to_packaged_copy_when_checkout_hooks_missing`
   - `test_install_hooks_uses_packaged_copy_when_checkout_hooks_missing` (end-to-end via
     `run_install_hooks`, checkout root faked away)
   - `test_hook_script_exists_handles_bash_prefixed_command`
   - Extended `test_install_hooks_degrades_gracefully_when_hooks_source_missing` to fake BOTH
     `_PACKAGE_ROOT` and the new `_PACKAGED_HOOKS_ROOT` (its old premise — "a pip install ships no
     hooks at all" — is exactly what this task reverses; it now tests "genuinely no hooks source
     anywhere," same assertions, unweakened).

Not touched (per brief): `driver.py`, `.github/workflows/release.yml`.

## TDD

Every behavior change above was test-first: wrote the drift-guard test and the four
`init_cmd`-resolver tests, ran them, watched them fail for the expected reason (missing
`agents_never_sleep/hooks/` dir; `AttributeError: _PACKAGED_HOOKS_ROOT`; bare-assert failure on
the bash-prefix case), then implemented `_hooks_root()` / `_hook_script_exists()` / the packaged
copy until green.

## Gate output

### Gate #1 — `bash acceptance/run_all.sh` (checkout unaffected)

Baseline (before any change): `ALL ACCEPTANCE SUITES GREEN` (57 suites).
Final (after all changes, incl. doc sync): `ALL ACCEPTANCE SUITES GREEN` (58 suites — +1 for the
new `test_hooks_packaged.py`). Ran three times across the change (post-implementation, post
blank-line cleanup, post doc-sync) — green every time.

### Gate #2 — wheel contents

Built with `python3 -m build --wheel` (via a dedicated build venv with `build` installed — the
system Python is externally-managed/PEP 668):

```
Successfully built agents_never_sleep-1.7.0-py3-none-any.whl
```

Brief's exact check:
```
WHEEL OK: hooks packaged
```

Extended completeness check (every repo-root `hooks/` file present in the wheel, nothing extra —
catches gaps a two-path spot-check would miss, e.g. the mixed-extension `hooks/platforms/hermes/`
tree with `.py`/`.yaml`/`.md`):
```
wheel: dist/agents_never_sleep-1.7.0-py3-none-any.whl
source hooks/ file count: 21
wheel hooks/ file count:  21
WHEEL COMPLETENESS OK: every repo-root hooks/ file is packaged, nothing extra
```

### Gate #3 — fresh-venv end-to-end (the crux)

Script: `gate3_e2e.sh` (scratchpad, not committed — a one-off verification script, not a repo
asset). Isolation notes:
- Fresh `python3 -m venv`, wheel installed via that venv's own `pip` (no `--no-index`/system
  fallback needed — the package has zero runtime deps).
- **Caught and fixed a false-pass risk during development**: the verification script must `cd`
  into a directory containing no `agents_never_sleep/` package before running any `python3 -c`,
  because `python3 -c` prepends the ambient CWD to `sys.path` — if run from inside either checkout
  (main or this worktree, both have a repo-root `agents_never_sleep/` right there), `import
  agents_never_sleep` would silently resolve to the CHECKOUT's copy instead of the venv's
  pip-installed one, making the gate pass without proving anything about the wheel. Observed this
  empirically on the first run (invoked from the main checkout's directory): the venv's own
  python resolved `agents_never_sleep.__file__` to `/var/www/projects/iipnprojects/agents-never-sleep/agents_never_sleep`
  instead of the venv's site-packages, and the script's own sanity check caught it
  ("a repo-root-shaped hooks/ exists next to site-packages — not a clean wheel-only test") before
  any false-positive gate result could be reported. Fixed by `cd`-ing into a fresh `mktemp -d`
  (containing no Python packages) before any Python invocation. Confirmed clean on every run
  after the fix (`$HOME` itself was already isolated via the script's own `$FAKE_HOME` in both
  runs — the bug was only in `sys.path` resolution for the diagnostic import, not in what
  install-hooks wrote).
- `CLAUDE_UNATTENDED=1` set explicitly (the hook is inert otherwise) and the venv's `bin/` put on
  `PATH` (so `python3 -m agents_never_sleep.enforce` inside `deny_irreversible.sh` resolves
  against the pip-installed package).
- Denial asserted on **stdout JSON** (`hookSpecificOutput.permissionDecision == "deny"`), not exit
  code — `deny_irreversible.sh` always exits 0 by design (fail-open contract); same detection
  logic as `acceptance/test_irreversible_hook.py`'s `_denied()`.

Full output (final run, against the doc-synced final code / rebuilt wheel):

```
== Gate #3: fresh-venv end-to-end ==
work dir:  /tmp/tmp.VJo4A7aRTl
repo:      /var/www/projects/iipnprojects/agents-never-sleep-wt-wheelhooks

-- locate built wheel --
wheel: /var/www/projects/iipnprojects/agents-never-sleep-wt-wheelhooks/dist/agents_never_sleep-1.7.0-py3-none-any.whl

-- create fresh venv (isolated from repo/system python) --
venv: /tmp/tmp.VJo4A7aRTl/venv

-- pip install the built wheel into the fresh venv --
installed.
Name: agents-never-sleep
Version: 1.7.0
Summary: Run a backlog to completion UNATTENDED — a portable Agent Skill + stdlib harness giving a coding agent durable per-ticket state, an ASK/PARK/HALT autonomy contract, deterministic test-gates, git-backed reversibility and a run report.
Home-page:
Author: Tokonomix.ai

-- sanity: no repo-root hooks/ reachable from the venv's install location --
site-packages agents_never_sleep dir: /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep
confirmed: no repo-root hooks/ next to site-packages (this venv can ONLY use the packaged fallback)

-- run 'ans-run install-hooks --harness claude --yes' under an isolated $HOME --
install-hooks — target: /tmp/tmp.VJo4A7aRTl/home/.claude/settings.json  (GLOBAL — affects every Claude Code session)
--- /tmp/tmp.VJo4A7aRTl/home/.claude/settings.json
+++ /tmp/tmp.VJo4A7aRTl/home/.claude/settings.json
@@ -1 +1,34 @@
-{}
+{
+  "hooks": {
+    "Stop": [
+      {
+        "hooks": [
+          {
+            "type": "command",
+            "command": "bash /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep/hooks/stop_guard.sh"
+          }
+        ]
+      }
+    ],
+    "PreToolUse": [
+      {
+        "matcher": "Bash",
+        "hooks": [
+          {
+            "type": "command",
+            "command": "bash /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep/hooks/deny_irreversible.sh"
+          }
+        ]
+      },
+      {
+        "matcher": "AskUserQuestion",
+        "hooks": [
+          {
+            "type": "command",
+            "command": "bash /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep/hooks/deny_ask.sh"
+          }
+        ]
+      }
+    ]
+  }
+}

✓ enforcement hooks wired for claude
exit code: 0
(a) OK — install-hooks exited 0

-- assert settings were written: /tmp/tmp.VJo4A7aRTl/home/.claude/settings.json --
[... written settings, identical to diff above ...]
(a) OK — settings file written

-- extract the referenced deny_irreversible.sh path from the written settings --
referenced script path: /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep/hooks/deny_irreversible.sh
(b) OK — referenced hook script EXISTS on disk in the venv's site-packages
    (confirmed: path is under site-packages/agents_never_sleep/, i.e. the packaged copy)

-- (c) invoke the script for real: does it DENY a dangerous command? --
hook exit code: 0
hook stdout: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "agents-never-sleep: blocked an irreversible/outward action (force-push). Park it for human review instead."}}
(c) OK — 'git push --force origin main' was DENIED (hookSpecificOutput.permissionDecision=deny)

-- control: confirm a benign command is ALLOWED (sanity, not a false-positive denier) --
benign hook stdout: ''

==================================================
GATE #3: ALL GREEN
  (a) install-hooks exited 0 and wrote /tmp/tmp.VJo4A7aRTl/home/.claude/settings.json
  (b) referenced script exists on disk: /tmp/tmp.VJo4A7aRTl/venv/lib/python3.12/site-packages/agents_never_sleep/hooks/deny_irreversible.sh
  (c) deny_irreversible.sh DENIES 'git push --force origin main' end to end
==================================================
```

## On the "repo-root `hooks/` stays canonical and UNCHANGED" guardrail

Worth stating explicitly since 7 files under `hooks/` (+3 READMEs) show as modified: the
**enforcement scripts** (`deny_ask.sh`, `deny_irreversible.sh`, `enforce.sh`, `stop_guard.sh`) —
the actual decision logic exercised by `test_irreversible_hook.py` / `test_ask_hook.py` /
`test_stop_hook.py` / `test_enforce_platforms.py`, all of which invoke these scripts directly —
are **byte-untouched**. What changed is the *wiring metadata* (`settings-snippet.json` + platform
JSON snippets): the command template gained an explicit `bash ` prefix, per the brief's design
step 4 ("the wired command form must work whether or not the script file is executable... change
the wired command form to invoke via `bash <script>`... keep the platforms/* snippets
consistent"). This is functionally identical for the checkout (a `#!/usr/bin/env bash`-shebanged
script runs the same invoked directly or via `bash`), required for the wheel case, and was the
explicit design instruction — not a deviation from "canonical and unchanged."

## Concerns / follow-ups (none blocking)

- `agents_never_sleep/hooks/` is a second copy of ~1KB–a few KB per file, drift-guarded but still
  a maintenance surface: any future edit to repo-root `hooks/` must re-copy into
  `agents_never_sleep/hooks/` or `test_hooks_packaged.py` goes red. Worth a one-line note in
  `CONTRIBUTING.md` if that isn't already covered elsewhere — did not add this (out of the task's
  explicit scope; flagging per Rule #9 rather than doing it unasked).
- The four manual-copy platform snippets (windsurf/copilot/crush/cursor) have no automated test
  coverage of their command-string contents (same as before this change) — only their *dispatcher
  shape* is tested via `test_enforce_platforms.py`, which calls `agents_never_sleep.enforce`
  directly and never reads these JSON files. The `bash ` prefix added to them is a mechanical,
  low-risk change but is unverified beyond visual inspection + the doc updates.
- **Enforcement-at-runtime depends on which `python3` Claude Code spawns the hook with, not just
  on `install-hooks` having written correct paths.** `deny_irreversible.sh` does
  `python3 -m agents_never_sleep.enforce claude pre_tool || true; exit 0` — fail-open by design.
  Gate #3(c) proved the deny fires with the installing venv's `bin/` on `PATH` (matching how a
  normal `pip install` + activate leaves a shell). If a wheel-install user's Claude Code session
  instead runs under a *different* `python3` that can't `import agents_never_sleep` (a bare
  system Python, a different venv), the import fails, `|| true` swallows it, and the hook
  silently ALLOWS instead of denying — no error surfaced. The source-checkout path sidesteps this
  by `cd`-ing to the repo root before `python3 -m` (so any `python3` with the checkout on-disk
  resolves it via CWD), which a wheel install has no equivalent for. This is inherent to how the
  wheel-fallback enforcement path resolves its interpreter, not a defect introduced by this
  change, and is out of this task's explicit scope (packaging + resolver + exec-bit only) — flagged
  here so it doesn't get mistaken for "enforcement is guaranteed once install-hooks succeeds."

---

# Follow-up — visible warning for the silent fail-open enforcement gap

Status: **DONE**

Worktree: `/var/www/projects/iipnprojects/agents-never-sleep-wt-wheelhooks`
Branch: `feat/package-hooks-in-wheel`

## Why

The previous entry in this report flagged (non-blocking) that enforcement depends on which
`python3` Claude Code spawns the hook with, not just on `install-hooks` writing correct paths:
`deny_irreversible.sh` runs `python3 -m agents_never_sleep.enforce ... || true`, so if that
`python3` can't `import agents_never_sleep` (wheel install, wrong venv on PATH), the import fails,
`|| true` swallows it, and the hook silently ALLOWS a dangerous command — no error surfaced
anywhere. A code review flagged this as a SILENT FAIL-OPEN that must not ship invisible for a
safety tool.

## Fix

`agents_never_sleep/init_cmd.py`:
- Added `_enforcement_python_can_import()` — runs a fresh `python3 -c "import agents_never_sleep"`
  subprocess (deliberately NOT `sys.executable`, which would always pass since install-hooks
  itself runs from an environment that has the package — that would mask exactly the gap this
  checks for) and returns whether it succeeded. Best-effort: any failure to even run `python3`
  (not found, timeout) also counts as "can't import".
- `run_install_hooks()`: after a successful wire (settings written, `hooks_wired()` verified
  True), calls the check and prints either a positive confirmation
  ("enforcement will run") or a `WARNING: enforcement may FAIL OPEN ...` message explaining the
  mechanism and the fix (run ans-run/the harness from the same env/venv where
  agents-never-sleep is installed, or use the source-checkout install). This is a visible warning
  only — exit code stays `EX_OK`; wiring itself did succeed, so this is not a NO-GO.

Did NOT touch the hook scripts (`enforce.sh` etc.) — a robust cross-layout interpreter fix is
separate design work, explicitly out of scope; this only makes the gap visible at wire time.

## Tests added (`acceptance/test_install_hooks.py`)

- `test_install_hooks_warns_when_enforcement_python_cannot_import` — monkeypatches
  `init_cmd._enforcement_python_can_import` to `False`, asserts `rc == 0` (wiring still
  succeeded), `hooks_wired()` still `True`, and the WARNING/"FAIL OPEN" text is printed.
- `test_install_hooks_confirms_when_enforcement_python_can_import` — monkeypatches it to `True`,
  asserts no WARNING and the positive "enforcement will run" confirmation is printed.
- `test_enforcement_python_can_import_reflects_real_python3` — sanity on the real (unmocked)
  helper: in this dev environment `python3` on PATH can import the package (editable-installed),
  so it must report `True` — proves the subprocess probe isn't a stub that's always False.

## Gate output

`python3 acceptance/test_install_hooks.py` → 22/22 `ok`, exit 0 (includes the 3 new tests above).

`bash acceptance/run_all.sh` → `ALL ACCEPTANCE SUITES GREEN`, exit 0.

## Concerns / follow-ups (none blocking)

- The check only runs at `install-hooks` wire time, not at every hook invocation — it can go stale
  if the operator later removes/breaks the package in the `python3` environment it found. That's
  inherent to a wire-time check and was the explicit scope here (make the gap visible, not
  eliminate it — the robust fix is a separate cross-layout interpreter design task).
