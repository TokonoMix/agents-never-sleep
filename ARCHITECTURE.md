# agents-never-sleep — Public API Surface

This document defines the **public interface** of the ANS harness — the contract. The *policy* for
how this contract may change (the per-surface Stable-vs-Experimental classification, the deprecation
rules, and the checkable v1.0 roadmap) lives in **`SEMVER.md`**. Anything classified Stable there
follows SemVer once 1.0 is tagged: breaking changes require a major version bump.

Installed entry points (after `pip install agents-never-sleep`): `ans` = `python3 -m agents_never_sleep.run`
(the loop, §1); `ans-run` = the launcher (§6).

---

## 1. CLI interface (`python3 -m agents_never_sleep.run` / `ans`)

### `next` — get the next ticket to work

```
python3 -m agents_never_sleep.run next [--repo <path>] [--tickets <dir>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | `.` | Absolute or relative path to the working repository. |
| `--tickets` | `tickets` | Directory of `.md` ticket files (or omit to use Paperclip). |

**Output:** one JSON object to stdout.

Stable response shapes:

```jsonc
{"status": "PROCEED",  "ticket": {"id": "...", "body": "...", "path": "..."}, "attempt": N, "snapshot": "<sha>", "instructions": "...", "council": {...}, "specialists": {...}}
{"status": "DRAINED"}          // all tickets processed; run complete
{"status": "HALTED",   "reason": "..."}  // irreversible danger; operator must intervene
{"status": "LOW_YIELD","report_path": "..."}  // too many parks/fails; morning report written
{"status": "NON_DESTRUCTIVE"}  // unattended, no config found; wizard must run first
{"status": "ERROR",    "error": "..."}   // transient; fix and retry
```

### `complete` — record the outcome for the in-flight ticket

```
python3 -m agents_never_sleep.run complete [--repo <path>] [--tickets <dir>]
    --attempted "<summary>"
    [--cannot-implement]
    [--council-verdict pass|concerns|error]
    [--council-cost <eur>]
    [--review-coverage "<who ran>"]
    [--specialist-concerns <comma-separated-roles>]
    [--specialist-cost <eur>]
```

**Output:** one JSON object to stdout.

Stable response shapes:

```jsonc
{"status": "RECORDED", "ticket_id": "...", "state": "DONE|DONE_LOW_CONFIDENCE|...", "why": "...", "bad": false, "next": "call `next`"}
{"status": "ERROR",    "error": "..."}
```

### `report` — write morning report and exit

```
python3 -m agents_never_sleep.run report [--repo <path>] [--report <path>]
```

Writes a Markdown morning report and exits 0.

---

## 2. Ticket format (`.md` files)

Tickets are Markdown files with an optional YAML front-matter block.

```markdown
---
id: unique-kebab-case-id        # optional; defaults to filename without .md
title: Human-readable title      # optional
blast_radius: low|medium|high    # optional hint (harness auto-classifies)
expected_outcome: DONE|PARKED_DECISION|...  # optional; for test scaffolding
---

Free-form ticket body describing what to implement.
The body is the only required content.
```

Stable guarantees:
- `id` must be unique within a tickets directory.
- Front-matter is YAML, indented with spaces, fenced by `---` lines.
- The harness never modifies ticket files.

---

## 3. Gate interface

A **gate** is a shell command that the harness runs after every edit. Exit 0 = green. Any non-zero
exit = red. The harness classifies reds as introduced-by-diff vs pre-existing via snapshot comparison.

Configured in `.claude/agents-never-sleep.json`:

```jsonc
{
  "gate": {
    "command": ["npm", "test"],   // required: array of strings
    "timeout_s": 120,             // optional; default 120
    "cwd": null                   // optional; defaults to repo root
  }
}
```

The harness guarantees:
- Gate runs in a non-interactive environment (no TTY).
- Gate output is captured and included in outcomes for diff-based classification.
- Gate timeout triggers `BLOCKED_ENV` (never a run halt).
- A gate that was already failing before your edit is classified as pre-existing and does NOT
  block the ticket from landing as DONE (it is noted in the morning report as a blind spot).

---

## 4. Outcome states (stable values)

| State | Meaning |
|---|---|
| `DONE` | Ticket implemented, gate green. |
| `DONE_LOW_CONFIDENCE` | Implemented but council raised concerns or was not run. Needs daylight review. |
| `PARKED_DECISION` | Ticket requires a human decision before implementation. Parked cleanly. |
| `PARKED_FOUNDATIONAL` | Ticket depends on a not-yet-completed prerequisite. |
| `BLOCKED_ENV` | Gate timed out or environment issue — not a code bug. |
| `FAILED_RETRYABLE` | Gate caught a bug introduced by the edit; reverted and can retry. |
| `FAILED_BUG_IN_AGENT` | Repeated failures suggest a systematic problem. |

---

## 5. Config file (`.claude/agents-never-sleep.json`)

Located at `<repo>/.claude/agents-never-sleep.json`. Created by the interactive wizard.
Non-breaking additions (new optional keys) are allowed in minor versions.
Removals or type changes to existing keys require a major version bump.

Stable top-level keys: `gate`, `budget`, `integrations`, `council`, `specialists`, `launcher`.

---

## 6. Launcher CLI (`ans-run` / `bin/ans-run`)

The pre-token GO/NO-GO preflight + atomic per-working-tree lock. Same code whether invoked as the
pip-installed `ans-run` console script or via `bin/ans-run` in a checkout.

```
ans-run [--repo <path>] [--agent <preset>] [--fg] [--check] [--trust] -- PROMPT...
```

| Flag | Meaning |
|---|---|
| `--repo` | Working tree (default: cwd). |
| `--agent` | Named preset from the config's `launcher.agents`. |
| `--fg` | Run in foreground; the agent's exit code propagates. |
| `--check` | Preflight report only — never starts a run, never disturbs a live one. |
| `--trust` | Record TOFU trust for the repo's current config and exit. |

**Exit codes (stable):** `0` = started / GO / trust recorded; `64` = NO-GO (a blocking preflight
check failed, or no prompt); `65` = working tree busy (another run holds the lock).

Agent selection uses human-confirmed presets only — no launch-time platform detection. A repo
config is executable input and is trust-on-first-use. See `bin/ans-run` / `agents_never_sleep/launcher.py` and
SKILL.md for the full security posture.
