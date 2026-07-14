# Tutorial — scheduling unattended runs (cron, systemd, wrappers, watchdog)

> **30-second version.** ANS never schedules itself — starting an unattended run is always your
> explicit act. This tutorial shows the four scheduling layers, outermost first: **cron or systemd**
> starts the run at a time you chose; a generic **retry wrapper** restarts the whole invocation when
> it exits on a transient failure; the **heartbeat watchdog** restarts a run that is alive but hung;
> and the **env contract** (`CLAUDE_UNATTENDED=1`, `UE_RUN_INCOMPLETE`) makes the enforcement hooks
> and the harness agree on where the run's state lives. Each layer catches a failure the layer
> inside it cannot see. See [scheduling](../scheduling.md) for how ANS orders tickets *within* a run,
> [the launcher doc](../launcher.md) for the preflight itself, and the [glossary](../glossary.md)
> for any term used here.

Every command, flag, and env var below is verified against the real code (`agents_never_sleep/`,
`bin/ans-run`, `hooks/`, v1.0.0).

## 0. Before you schedule anything

A scheduled launch is headless, so everything interactive must already be done:

- **Config exists** — `.claude/agents-never-sleep.json`, written by the first interactive run's
  wizard. Unattended with no config degrades to non-destructive-only.
- **Trust is recorded** — the config describes commands the launcher executes, so a new or changed
  config is refused headless (TOFU). Record trust once, interactively:
  `bin/ans-run --trust --repo /path/to/project`.
- **Autonomy is confirmed** — the agent preset needs `autonomy_confirmed: true`, and its argv needs
  the CLI's real non-interactive permission flag; a detached run with an interactive permission
  mode is a blocking NO-GO (it would hang silently on the first tool prompt).
- **Dry-run the preflight** — `bin/ans-run --check --repo /path/to/project` prints the full GO/NO-GO
  report without launching anything.
- **Consent is decided (if needed)** — any deny-list class you want pre-authorized for this scheduled
  run (e.g. `send_email` for a legitimate notification) must be granted during the interactive wizard
  session, before the job is scheduled headless — the unattended run cannot grant it later.

Install: `pip install agents-never-sleep` (live on PyPI since v1.4.0), or pinned from GitHub:
`pip install git+https://github.com/TokonoMix/agents-never-sleep@v1.0.0`.

## 1. The command you are scheduling

Schedule `bin/ans-run` (or the pip-installed `ans-run` console script), not the agent CLI directly.
It is a pre-token GO/NO-GO gate: it refuses to start when the environment cannot support an
unattended run, and an atomic working-tree lock guarantees at most one run per working tree — so an
overlapping cron firing exits with code 65 instead of corrupting a run in progress.

```bash
bin/ans-run --repo /path/to/project "work the backlog with the agents-never-sleep skill"
```

Exit codes: `0` = started (with `--fg`, the agent's own exit code propagates), `64` = NO-GO,
`65` = working tree busy. Background mode (the default) writes the agent's output to
`.unattended/logs/` and returns after spawning; `--fg` makes the launcher *become* the agent
process, so the caller tracks the whole run's lifetime and exit code.

## 2. cron — the user's own crontab, no sudo

Prefer the target user's own crontab (`crontab -e` as that user). No sudo rule is needed, the run
inherits that user's HOME (where the agent CLI's credentials live), and the launcher's identity
check passes without a re-exec:

```cron
# m  h  dom mon dow  command
0 22 * * 1-5  cd /path/to/project && /path/to/checkout/bin/ans-run --repo . "work the backlog with the agents-never-sleep skill" >> "$HOME/ans-cron.log" 2>&1
```

The `cd` into the repo is deliberate — it makes `$PWD` equal `--repo`, which the sentinel contract
in section 6 depends on. If your job cannot `cd` first, export `UE_RUN_INCOMPLETE` instead (see
section 6).

### The root-guard, and the only sudoers rule that is acceptable

If a launch starts as root (e.g. the system crontab) and the config sets `launcher.target_user`,
the launcher re-execs itself as that user via `sudo -H -u <target_user>` before doing anything
else — unattended runs must not run as root. **Prefer avoiding this entirely** by scheduling as
the target user. Where the re-exec is genuinely needed, grant a **command-scoped** sudoers rule
pointing at a root-owned, non-writable path:

```
ops ALL=(ansrunner) NOPASSWD: /usr/local/bin/ans-run
```

Never `NOPASSWD: ALL` — that hands an autonomous, shell-executing agent a passwordless
privilege-escalation primitive.

## 3. systemd — a unit with `User=`, fired by a timer

The systemd equivalent of "run as the target user, no sudo" is `User=` in the service unit. Use
`--fg` so the service's lifetime and exit status are the run's:

```ini
# /etc/systemd/system/ans-backlog.service
[Unit]
Description=ANS unattended backlog run

[Service]
Type=oneshot
User=ansrunner
WorkingDirectory=/path/to/project
ExecStart=/path/to/checkout/bin/ans-run --repo /path/to/project --fg "work the backlog with the agents-never-sleep skill"
```

```ini
# /etc/systemd/system/ans-backlog.timer
[Unit]
Description=Start the ANS backlog run on schedule

[Timer]
OnCalendar=Mon..Fri 22:00
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now ans-backlog.timer`. `WorkingDirectory=` pointing at the repo satisfies the
sentinel contract the same way cron's `cd` does. Note that `--fg` bypasses the launcher's own
watchdog wrap (section 5) — the process *is* the agent — so under systemd you either accept
"restart = next timer firing" or compose the watchdog explicitly as shown below.

## 4. Overload resilience — a generic retry wrapper

An agent CLI talks to a hosted API; a sustained overload or outage can end the invocation with a
non-zero exit long before the backlog is drained. Because ANS keeps per-ticket state durable, a
restarted run **resumes**: DONE and parked tickets are skipped and the run continues from the next
open ticket — which is what makes an outer retry loop safe at all.

Wrap the launch in a plain exponential-backoff loop (this is deliberately generic — use whatever
retry tooling your host already has, the mechanism is the same):

```bash
#!/usr/bin/env bash
# retry-run — restart the wrapped command on failure, with exponential backoff.
attempt=1; max=5; delay=30
while true; do
  "$@"; rc=$?
  [ "$rc" -eq 0 ] && exit 0
  [ "$attempt" -ge "$max" ] && exit "$rc"
  sleep "$delay"
  attempt=$((attempt + 1)); delay=$((delay * 2)); [ "$delay" -gt 300 ] && delay=300
done
```

```cron
0 22 * * 1-5  cd /path/to/project && retry-run /path/to/checkout/bin/ans-run --repo . --fg "work the backlog with the agents-never-sleep skill" >> "$HOME/ans-cron.log" 2>&1
```

Two honest caveats. First, this bounds how long a *transient* failure can end the run early; it
does not make the run succeed. Second, a blind restart-on-any-nonzero also repeats *genuine*
failures — if your agent CLI documents distinct exit codes for transient conditions, restrict the
retry to those and let real errors surface immediately.

## 5. The heartbeat watchdog — catching a hang, not a crash

A retry wrapper only sees an *exit*. The failure it cannot see is a run that is **alive but
wedged** — the process exists, no progress happens, nothing ever exits. ANS's answer is the
heartbeat watchdog (`agents_never_sleep/watchdog.py`, design in [WATCHDOG.md](../../WATCHDOG.md)):
it runs the agent as a child, polls the heartbeat file the harness touches at every
`next`/`complete` boundary, and on a stale heartbeat kills and restarts the child **resumable**, up
to `--max-restarts`. A clean exit — including a genuine crash — passes through untouched; the
watchdog restarts hangs, not failures.

You get it two ways:

- **Default with `ans-run` background mode.** A detached `ans-run` launch is wrapped in the
  watchdog automatically, sized from your budget config
  (`stale ≈ per_ticket_timeout_s × (per_ticket_fix_iterations + 1) + 1800`). Opt out with
  `--no-watchdog` or `"launcher": {"watchdog": {"enabled": false}}`.
- **Opt-in composition around any command** — including a `--fg` launch under systemd, or a
  different wrapper stack:

```bash
python3 -m agents_never_sleep.watchdog \
  --heartbeat /path/to/project/.unattended/state/heartbeat.json \
  --stale 2400 --max-restarts 3 \
  -- bin/ans-run --repo /path/to/project --fg "work the backlog with the agents-never-sleep skill"
```

Flags (defaults from the code): `--stale 2400`, `--poll 30`, `--max-restarts 3`, `--grace 300`
(startup grace before staleness counts), `--alert '<shell command>'` run when restarts are
exhausted, `--alert-timeout 30` so the alert itself cannot hang. **`--stale` must exceed the
worst-case gap between heartbeats** — the agent only beats at `next`/`complete` boundaries, so a
whole ticket's implement→gate→review span passes silently; size it above
`per_ticket_timeout_s × (fix_iterations + 1)` plus margin, or the watchdog false-restarts a healthy
run mid-ticket.

**Exit 75** means "restarts exhausted, gave up" — a stable convention your outer scheduling can
react to (alert, page, skip the next firing). The watchdog also sets `CLAUDE_UNATTENDED=1` and
`UE_HEARTBEAT` for its child automatically.

## 6. The env contract — `CLAUDE_UNATTENDED=1` and `UE_RUN_INCOMPLETE`

Two env vars make the enforcement layer and the harness agree at 2am:

- **`CLAUDE_UNATTENDED=1`** activates the [Claude Code enforcement hooks](../../hooks/README.md)
  (Stop-guard, deny-irreversible, deny-ask); without it they are inert, so your interactive
  sessions are untouched. The watchdog sets it for its child; in any launch path that skips the
  watchdog (cron of the agent CLI directly, `--fg` without composition), export it yourself.
- **`UE_RUN_INCOMPLETE`** pins the run-incomplete sentinel path. The Stop-hook checks
  `${UE_RUN_INCOMPLETE:-$PWD/.unattended/run-incomplete}` and the driver writes the same path — they
  agree automatically **only when the agent runs from the repo root** (`$PWD` == `--repo`). When
  your scheduled job's CWD can differ from the repo, export
  `UE_RUN_INCOMPLETE=/path/to/project/.unattended/run-incomplete` at launch. This is enforced, not
  advisory: `next` hard-fails (`status: "ERROR"`) when it detects unattended mode + CWD ≠ `--repo` +
  `UE_RUN_INCOMPLETE` unset, so a never-stop guarantee that would have silently broken breaks
  loudly instead.
- **`UE_CONSENT` / `UE_CONSENT_AUDIT`** are frozen into the agent's env by the launcher itself (from the
  out-of-repo consent store) after the TOFU trust check — nothing to set in your cron/systemd unit; do
  not set these by hand.

## 7. Long backlogs — `fresh_session_every`

One agent session driving a long backlog degrades as its context accumulates. The opt-in fix is
`"launcher": {"fresh_session_every": N}` (integer ≥ 0, **default 0 = off**): the launcher
supervises a bounded respawn loop (cap 500) that gives each agent session a budget of N tickets and
spawns a fresh session while the run-incomplete sentinel says work remains. The details — how the
early stop is coordinated without weakening never-stop — are in
[scheduling](../scheduling.md#context-strategy-for-long-backlogs--fresh_session_every) and SKILL.md.
For a scheduled run this changes nothing about cron/systemd/watchdog wiring: the supervising loop
runs inside the one process you scheduled.

## Scope and limitations

Only **Claude Code** is the live-verified enforcement platform; adapters for other agent CLIs are
built to their documented hook contracts and are not live-verified. The layers here bound specific
failure modes (missed start, transient exit, hang) — none of them guarantees the backlog completes;
the run report is where you read what actually happened. The retry wrapper is an example, not a
product: prefer your host's existing retry tooling if it already distinguishes transient exits.

---

*Verified against `agents_never_sleep/` (v1.0.0): `launcher.py` (`--repo/--agent/--fg/--check/
--trust/--no-watchdog`, exit codes 0/64/65, TOFU trust, root-guard re-exec, default watchdog wrap
and its stale-default formula, `fresh_session_every` + `SESSION_RESPAWN_CAP = 500`), `watchdog.py`
(flag defaults, exit 75, `CLAUDE_UNATTENDED`/`UE_HEARTBEAT` injection, hang-vs-exit semantics),
`run.py` (the `UE_RUN_INCOMPLETE` hard-fail), `hooks/stop_guard.sh` (sentinel path, env gating),
`bin/ans-run` (shim), and SKILL.md (sudoers guidance).*
