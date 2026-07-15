#!/usr/bin/env python3
"""ANS-OSS — bin/ans-run launcher: pre-token GO/NO-GO preflight + atomic tree lock +
TOFU config trust + CLI allowlist + preset-based agent selection.

Proves the guarantees the launcher exists for:
  * GO/NO-GO is decided BEFORE the agent boots — wrong user, missing CLI, failing blocking
    host-check, unwritable repo, low disk or invalid config refuse with exit 64;
  * a repo-supplied config is EXECUTABLE INPUT: untrusted or changed configs never run
    headless (TOFU), argv[0] outside the known-CLI allowlist is refused unless the trusted
    config explicitly allows it, and presets without a confirmed autonomy decision refuse;
  * mutual exclusion is ATOMIC — two simultaneous starts on the same working tree yield
    exactly one winner (the loser exits 65), and the kernel releases the lock the moment
    the run process dies (crash included), never via pidfiles.

Exit 0 = GREEN.
"""
# py3.9 compat (requires-python >= 3.9): PEP 604 annotations must not evaluate at def time.
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
ANS_RUN = os.path.join(SKILL_ROOT, "bin", "ans-run")

EX_NOGO = 64
EX_BUSY = 65

# Per-suite-run trust store: tests share one store file outside any repo.
TRUST_DIR = tempfile.mkdtemp(prefix="ue-launcher-trust-")
TRUST_STORE = os.path.join(TRUST_DIR, "trusted.json")


def _write_config(repo: str, launcher: dict) -> None:
    cfg_dir = os.path.join(repo, ".claude")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "agents-never-sleep.json"), "w") as fh:
        json.dump({"launcher": launcher}, fh)


def _new_repo(agent_script: str, launcher_extra: dict | None = None,
              write_config: bool = True) -> str:
    repo = tempfile.mkdtemp(prefix="ue-launcher-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("sandbox\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    agent = os.path.join(repo, "fake-agent.sh")
    with open(agent, "w") as fh:
        fh.write(agent_script)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)

    creds = os.path.join(repo, "fake-creds.json")
    with open(creds, "w") as fh:
        fh.write('{"FAKE": "placeholder"}')

    if write_config:
        launcher = {
            "agent_cmd": [agent],
            "allow_custom_agent": True,
            "credentials_paths": [creds],
            "min_disk_mb": 1,
        }
        launcher.update(launcher_extra or {})
        _write_config(repo, launcher)
    return repo


def _run(repo: str, *extra: str, timeout: int = 30,
         env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ANS_TRUST_STORE"] = TRUST_STORE
    env["ANS_TEST_MODE"] = "1"  # the store override is only honored under the test flag
    env.update(env_extra or {})
    return subprocess.run([sys.executable, ANS_RUN, "--repo", repo, *extra],
                          capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)


def _trusted_repo(agent_script: str, launcher_extra: dict | None = None) -> str:
    repo = _new_repo(agent_script, launcher_extra)
    res = _run(repo, "--trust")
    assert res.returncode == 0, f"--trust failed: {res.stdout}{res.stderr}"
    return repo


SLEEPER = "#!/bin/sh\nsleep 4\nexit 0\n"
INSTANT_FAIL = "#!/bin/sh\nexit 7\n"
# Dumps CLAUDE_UNATTENDED into a file beside itself so a detached test can inspect what the spawned
# agent actually saw, then exits fast (no lingering process to wait out).
ENV_DUMPER = '#!/bin/sh\necho "${CLAUDE_UNATTENDED:-UNSET}" > "$(dirname "$0")/env-dump.txt"\nexit 0\n'
# Same idea for UE_CONSENT (t03: the frozen out-of-repo consent snapshot).
CONSENT_ENV_DUMPER = ('#!/bin/sh\necho "${UE_CONSENT:-UNSET}" > "$(dirname "$0")/consent-dump.txt"\n'
                      'exit 0\n')


def test_untrusted_config_headless_is_nogo(failures):
    repo = _new_repo(SLEEPER)  # config present, never trusted
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[tofu] untrusted config: expected {EX_NOGO}, got "
                        f"{res.returncode}: {res.stdout}")
    if "--trust" not in res.stdout + res.stderr:
        failures.append("[tofu] refusal does not teach the --trust fix")


def test_changed_config_invalidates_trust(failures):
    repo = _trusted_repo(SLEEPER)
    if _run(repo, "--check").returncode != 0:
        failures.append("[tofu-change] trusted repo should be GO")
        return
    # Mutate the config -> hash changes -> trust must drop.
    _write_config(repo, {"agent_cmd": [os.path.join(repo, "fake-agent.sh")],
                         "allow_custom_agent": True, "min_disk_mb": 1,
                         "checks": [{"name": "sneaky", "command": "true"}]})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[tofu-change] changed config: expected {EX_NOGO}, got "
                        f"{res.returncode}: {res.stdout}")
    if _run(repo, "--trust").returncode != 0 or _run(repo, "--check").returncode != 0:
        failures.append("[tofu-change] re-trust after review should restore GO")


def test_custom_agent_requires_allowlist_optin(failures):
    repo = _trusted_repo(SLEEPER, {"allow_custom_agent": False})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[allowlist] custom argv0 without opt-in: expected {EX_NOGO}, "
                        f"got {res.returncode}: {res.stdout}")
    if "allow_custom_agent" not in res.stdout:
        failures.append("[allowlist] refusal does not name the opt-in key")


def test_no_config_launch_is_nogo(failures):
    repo = _new_repo(SLEEPER, write_config=False)
    res = _run(repo, "go")
    if res.returncode != EX_NOGO:
        failures.append(f"[no-config] expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}")
    res2 = _run(repo, "--trust")
    if res2.returncode != EX_NOGO:
        failures.append(f"[no-config] --trust with nothing to trust: expected {EX_NOGO}, "
                        f"got {res2.returncode}")


def test_preset_selection_and_autonomy_gate(failures):
    agent_rel = "fake-agent.sh"
    def presets(repo):
        agent = os.path.join(repo, agent_rel)
        return {
            "agents": {
                "good": {"cmd": [agent], "autonomy_confirmed": True, "env": {}},
                "unconfirmed": {"cmd": [agent], "autonomy_confirmed": False, "env": {}},
            },
            "default_agent": "good",
            "agent_cmd": None,
        }
    repo = _new_repo(SLEEPER)
    _write_config(repo, {**presets(repo), "allow_custom_agent": True, "min_disk_mb": 1})
    _run(repo, "--trust")

    if _run(repo, "--check").returncode != 0:
        failures.append("[preset] default confirmed preset should be GO")
    res = _run(repo, "--agent", "unconfirmed", "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[preset] unconfirmed preset: expected {EX_NOGO}, got "
                        f"{res.returncode}: {res.stdout}")
    res2 = _run(repo, "--agent", "missing", "--check")
    if res2.returncode != EX_NOGO or "available" not in res2.stdout:
        failures.append(f"[preset] missing preset should refuse and list available: "
                        f"{res2.returncode}: {res2.stdout}")


def test_known_cli_capability_probe(failures):
    # A fake `claude` on PATH: allowlisted, probe must pass on rc 0 and gate on rc != 0.
    for probe_rc, want in ((0, 0), (2, EX_NOGO)):
        repo = _new_repo(SLEEPER)
        bin_dir = os.path.join(repo, "fakebin")
        os.makedirs(bin_dir, exist_ok=True)
        fake = os.path.join(bin_dir, "claude")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\n"
                     f'[ "$1" = "--version" ] && exit {probe_rc}\n'
                     "sleep 1\nexit 0\n")
        os.chmod(fake, 0o755)
        _write_config(repo, {
            "agents": {"claude": {"cmd": ["claude", "-p", "--permission-mode",
                                          "acceptEdits"],
                                  "autonomy_confirmed": True, "env": {}}},
            "default_agent": "claude", "min_disk_mb": 1,
        })
        _run(repo, "--trust")
        res = _run(repo, "--check",
                   env_extra={"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        if res.returncode != want:
            failures.append(f"[probe rc={probe_rc}] expected {want}, got "
                            f"{res.returncode}: {res.stdout}")


def _fake_claude_repo(cmd: list) -> tuple:
    """A repo whose 'claude' preset runs a fake claude on PATH (probe passes). Returns
    (repo, path_env) so a --check can resolve the fake binary."""
    repo = _new_repo(SLEEPER)
    bin_dir = os.path.join(repo, "fakebin")
    os.makedirs(bin_dir, exist_ok=True)
    fake = os.path.join(bin_dir, "claude")
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\n[ \"$1\" = \"--version\" ] && exit 0\nsleep 1\nexit 0\n")
    os.chmod(fake, 0o755)
    _write_config(repo, {
        "agents": {"claude": {"cmd": cmd, "autonomy_confirmed": True, "env": {}}},
        "default_agent": "claude", "min_disk_mb": 1,
    })
    _run(repo, "--trust")
    return repo, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]}


def test_detached_interactive_permission_mode_is_nogo(failures):
    # A detached run has stdin closed; a preset whose known-CLI argv keeps the CLI's own
    # permission prompts on would hang on the first tool call. The guard inspects the argv
    # (not just autonomy_confirmed) and refuses a detached launch — but a non-interactive
    # flag proceeds, and --fg (human attached) is allowed.
    repo, penv = _fake_claude_repo(["claude", "-p"])  # cmd_safe: permissions ON
    res = _run(repo, "--check", env_extra=penv)
    if res.returncode != EX_NOGO:
        failures.append(f"[perm] detached interactive preset: expected {EX_NOGO}, got "
                        f"{res.returncode}: {res.stdout}")
    if "permission mode" not in res.stdout or "hang" not in res.stdout:
        failures.append(f"[perm] refusal must explain the detached-hang reason: {res.stdout}")

    # --fg = a human is attached → interactive is their choice → not blocked (note only).
    res_fg = _run(repo, "--check", "--fg", env_extra=penv)
    if res_fg.returncode != 0:
        failures.append(f"[perm] --fg interactive preset should be GO, got "
                        f"{res_fg.returncode}: {res_fg.stdout}")

    # Both non-interactive flags are GO (won't hang), but the messaging must distinguish
    # edit-only autonomy (acceptEdits: shell/network still gated → inert, not hung) from
    # full autonomy (--dangerously-skip-permissions: functions end-to-end detached).
    for ok_cmd, want_copy in (
            (["claude", "-p", "--permission-mode", "acceptEdits"], "FILE EDITS only"),
            (["claude", "-p", "--dangerously-skip-permissions"], "full autonomy")):
        repo_ok, penv_ok = _fake_claude_repo(ok_cmd)
        res_ok = _run(repo_ok, "--check", env_extra=penv_ok)
        if res_ok.returncode != 0:
            failures.append(f"[perm] non-interactive {ok_cmd}: expected GO, got "
                            f"{res_ok.returncode}: {res_ok.stdout}")
        if want_copy not in res_ok.stdout:
            failures.append(f"[perm] {ok_cmd} should report '{want_copy}': {res_ok.stdout}")


def test_permission_marker_table_matches_cmd_variants(failures):
    # Unit: the marker check agrees with agent_clis' own cmd_safe/cmd_unattended split for
    # every known CLI, handles the "--flag=value" form, and returns None for a custom CLI.
    sys.path.insert(0, SKILL_ROOT)
    from agents_never_sleep.agent_clis import (AGENT_CLIS, is_noninteractive_permission)
    for name, spec in AGENT_CLIS.items():
        if is_noninteractive_permission(spec["cmd_unattended"]) is not True:
            failures.append(f"[marker] cmd_unattended for '{name}' should read non-interactive")
        if is_noninteractive_permission(spec["cmd_safe"]) is not False:
            failures.append(f"[marker] cmd_safe for '{name}' should read interactive")
    if is_noninteractive_permission(["claude", "-p", "--permission-mode=acceptEdits"]) is not True:
        failures.append("[marker] --permission-mode=acceptEdits (=form) should read non-interactive")
    if is_noninteractive_permission(["claude", "-p", "--permission-mode", "plan"]) is not False:
        failures.append("[marker] --permission-mode plan should read interactive")
    if is_noninteractive_permission(["my-agent", "run"]) is not None:
        failures.append("[marker] unknown/custom CLI should be None (cannot judge)")


def _isolated_home() -> str:
    return tempfile.mkdtemp(prefix="ue-launcher-home-")


def test_enforcement_hooks_note_when_unwired_but_still_go(failures):
    # Task B (consensus T1): the preflight issued a GO without ever checking whether the ANS
    # deny-hooks are wired into the resolved harness's host config. unwired hooks must get a
    # NON-BLOCKING NOTE naming the remedy — and the run must still GO. A gate here would be
    # exactly the friction the trusted-operator design avoids.
    repo, penv = _fake_claude_repo(["claude", "-p", "--dangerously-skip-permissions"])
    env = dict(penv)
    env["HOME"] = _isolated_home()  # empty HOME -> no ~/.claude/settings.json -> unwired
    res = _run(repo, "--check", env_extra=env)
    if res.returncode != 0:
        failures.append(f"[hooks-note-unwired] unwired hooks must still be GO: "
                        f"{res.returncode}: {res.stdout}")
    if "install-hooks --harness claude" not in res.stdout:
        failures.append(f"[hooks-note-unwired] missing remedy-naming NOTE: {res.stdout}")


def test_enforcement_hooks_no_note_when_wired(failures):
    repo, penv = _fake_claude_repo(["claude", "-p", "--dangerously-skip-permissions"])
    env = dict(penv)
    env["HOME"] = _isolated_home()
    full_env = dict(os.environ)
    full_env.update(env)
    wire = subprocess.run([sys.executable, ANS_RUN, "install-hooks", "--harness", "claude",
                           "--yes"], capture_output=True, text=True, timeout=30, env=full_env)
    if wire.returncode != 0:
        failures.append(f"[hooks-note-wired] install-hooks setup failed: "
                        f"{wire.returncode}: {wire.stdout}{wire.stderr}")
        return
    res = _run(repo, "--check", env_extra=env)
    if res.returncode != 0:
        failures.append(f"[hooks-note-wired] wired hooks should still be GO: "
                        f"{res.returncode}: {res.stdout}")
    if "install-hooks --harness claude" in res.stdout:
        failures.append(f"[hooks-note-wired] wired hooks must NOT emit the remedy NOTE: "
                        f"{res.stdout}")


def test_enforcement_hooks_note_when_referenced_script_missing(failures):
    # Moved/deleted install: the settings file still references the ANS hook markers, but the
    # script path no longer exists on disk -> the strengthened detector must treat this as NOT
    # wired (dead enforcement), and the preflight must still NOTE it (still GO).
    repo, penv = _fake_claude_repo(["claude", "-p", "--dangerously-skip-permissions"])
    home = _isolated_home()
    env = dict(penv)
    env["HOME"] = home
    settings_dir = os.path.join(home, ".claude")
    os.makedirs(settings_dir, exist_ok=True)
    missing = os.path.join(home, "moved-away", "agents-never-sleep", "hooks")
    data = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                  "command": os.path.join(missing, "stop_guard.sh")}]}],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command",
                                                "command": os.path.join(missing, "deny_irreversible.sh")}]},
                {"matcher": "AskUserQuestion", "hooks": [{"type": "command",
                                                           "command": os.path.join(missing, "deny_ask.sh")}]},
            ],
        }
    }
    with open(os.path.join(settings_dir, "settings.json"), "w") as fh:
        json.dump(data, fh)
    res = _run(repo, "--check", env_extra=env)
    if res.returncode != 0:
        failures.append(f"[hooks-note-missing-script] must still be GO: "
                        f"{res.returncode}: {res.stdout}")
    if "install-hooks --harness claude" not in res.stdout:
        failures.append(f"[hooks-note-missing-script] missing remedy-naming NOTE "
                        f"(moved/deleted install must count as unwired): {res.stdout}")


def test_capability_restriction_appends_declared_tokens(failures):
    # Ticket 05-B: a preset's opt-in `capabilities` list is appended to the agent argv (restricts
    # the loaded MCP/tool set); absent = full set (no-op); a malformed value is a NO-GO.
    sys.path.insert(0, SKILL_ROOT)
    from agents_never_sleep.launcher import Report, resolve_agent
    caps = ["--strict-mcp-config", "--mcp-config", "/min.json"]
    cfg = {"agents": {"claude": {"cmd": ["claude", "-p", "--dangerously-skip-permissions"],
                                 "autonomy_confirmed": True, "capabilities": caps}},
           "default_agent": "claude"}
    argv, _env, _name = resolve_agent(cfg, True, "claude", Report())
    if argv[-3:] != caps:
        failures.append(f"[cap] declared capabilities not appended: {argv}")

    cfg_none = {"agents": {"claude": {"cmd": ["claude", "-p"], "autonomy_confirmed": True}},
                "default_agent": "claude"}
    argv2, _e, _n = resolve_agent(cfg_none, True, "claude", Report())
    if argv2 != ["claude", "-p"]:
        failures.append(f"[cap] absent capabilities must leave argv unchanged (full set): {argv2}")

    cfg_bad = {"agents": {"claude": {"cmd": ["claude", "-p"], "autonomy_confirmed": True,
                                     "capabilities": "not-a-list"}}, "default_agent": "claude"}
    rep_bad = Report()
    resolve_agent(cfg_bad, True, "claude", rep_bad)
    if not rep_bad.failed:
        failures.append("[cap] a non-list capabilities value must be a NO-GO")


def test_repo_shipped_binary_rejected(failures):
    # SECURITY: a hostile repo ships its own ./claude; basename would match the allowlist.
    # A path-bearing argv0 must NOT pass the allowlist (needs allow_custom_agent).
    repo = _new_repo(SLEEPER)
    evil = os.path.join(repo, "claude")
    with open(evil, "w") as fh:
        fh.write("#!/bin/sh\ntouch " + os.path.join(repo, "PWNED") + "\nexit 0\n")
    os.chmod(evil, 0o755)
    _write_config(repo, {
        "agents": {"x": {"cmd": ["./claude", "-p"], "autonomy_confirmed": True, "env": {}}},
        "default_agent": "x", "allow_custom_agent": False, "min_disk_mb": 1,
    })
    _run(repo, "--trust")
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[repo-bin] path-bearing ./claude must be refused: "
                        f"{res.returncode}: {res.stdout}")
    if os.path.exists(os.path.join(repo, "PWNED")):
        failures.append("[repo-bin] the repo-shipped binary EXECUTED via the probe — bypass")


def test_preset_path_does_not_swap_probe_target(failures):
    # SECURITY: a preset PATH must not let the probe validate /usr/bin/claude while the
    # spawn runs a repo-shipped one. We resolve under the child env, so a repo-PATH claude
    # is what gets probed — and its probe (exit 3) gates the launch.
    repo = _new_repo(SLEEPER)
    evilbin = os.path.join(repo, "evilbin")
    os.makedirs(evilbin, exist_ok=True)
    fake = os.path.join(evilbin, "claude")
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\n[ \"$1\" = \"--version\" ] && exit 3\nexit 0\n")
    os.chmod(fake, 0o755)
    _write_config(repo, {
        "agents": {"claude": {"cmd": ["claude", "-p", "--permission-mode", "acceptEdits"],
                              "autonomy_confirmed": True,
                              "env": {"PATH": evilbin + ":/usr/bin:/bin"}}},
        "default_agent": "claude", "min_disk_mb": 1,
    })
    _run(repo, "--trust")
    res = _run(repo, "--check")
    # The repo-PATH claude is resolved and probed; its rc=3 → NO-GO (probe == spawn target).
    if res.returncode != EX_NOGO:
        failures.append(f"[preset-path] probe must validate the SAME binary the spawn "
                        f"runs: {res.returncode}: {res.stdout}")
    if "preset env sets PATH" not in res.stdout:
        failures.append("[preset-path] preset PATH should be flagged as resolution-changing")


def test_trust_store_override_ignored_without_test_flag(failures):
    # SECURITY: ANS_TRUST_STORE relocates the gate — honored only with ANS_TEST_MODE=1.
    # Without the flag, a pre-seeded store must NOT make an untrusted config trusted.
    repo = _new_repo(SLEEPER)
    env = dict(os.environ)
    env.pop("ANS_TEST_MODE", None)
    env["ANS_TRUST_STORE"] = TRUST_STORE  # this store is where _run records trust
    # Pre-seed: trust the repo in the override store, then run WITHOUT the test flag.
    _run(repo, "--trust")  # records into TRUST_STORE (test flag on inside _run)
    res = subprocess.run([sys.executable, ANS_RUN, "--repo", repo, "--check"],
                         capture_output=True, text=True, timeout=30, env=env)
    if res.returncode != EX_NOGO:
        failures.append(f"[store-override] override store must be ignored without "
                        f"ANS_TEST_MODE: expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}{res.stderr}")


def test_check_healthy_repo_is_go(failures):
    repo = _trusted_repo(SLEEPER)
    res = _run(repo, "--check")
    if res.returncode != 0:
        failures.append(f"[check-go] expected 0, got {res.returncode}: {res.stdout}")
    if "== GO" not in res.stdout:
        failures.append(f"[check-go] no GO verdict in report: {res.stdout}")


def test_missing_agent_cli_is_nogo(failures):
    repo = _trusted_repo(SLEEPER, {"agent_cmd": ["definitely-not-a-real-cli-xyz"]})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[no-cli] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_configured_credentials_missing_is_nogo(failures):
    repo = _trusted_repo(SLEEPER, {"credentials_paths": ["/nonexistent/creds.json"]})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[no-creds] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_target_user_mismatch_is_nogo(failures):
    repo = _trusted_repo(SLEEPER, {"target_user": "nonexistent-ans-user"})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[user] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_blocking_host_check_gates_nonblocking_warns(failures):
    repo = _trusted_repo(SLEEPER, {"checks": [
        {"name": "always-fails", "command": "exit 1", "blocking": True}]})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[blk-check] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")

    repo2 = _trusted_repo(SLEEPER, {"checks": [
        {"name": "soft-fail", "command": "exit 1", "blocking": False}]})
    res2 = _run(repo2, "--check")
    if res2.returncode != 0:
        failures.append(f"[soft-check] expected 0, got {res2.returncode}: {res2.stdout}")
    if "(with warnings)" not in res2.stdout:
        failures.append(f"[soft-check] expected warning verdict: {res2.stdout}")


def test_no_prompt_is_nogo(failures):
    repo = _trusted_repo(SLEEPER)
    res = _run(repo)
    if res.returncode != EX_NOGO:
        failures.append(f"[no-prompt] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_invalid_config_json_is_nogo(failures):
    repo = _trusted_repo(SLEEPER)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w") as fh:
        fh.write("{not json")
    _run(repo, "--trust")  # even a trusted-but-invalid config must refuse
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[bad-json] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_low_disk_is_nogo(failures):
    repo = _trusted_repo(SLEEPER, {"min_disk_mb": 10**9})
    res = _run(repo, "--check")
    if res.returncode != EX_NOGO:
        failures.append(f"[low-disk] expected {EX_NOGO}, got {res.returncode}: {res.stdout}")


def test_unwritable_repo_is_nogo(failures):
    # root (euid 0) bypasses the write bit, so chmod 555 does not make the repo
    # unwritable → false RED. SKIP under root (mirrors launcher.py:213/658).
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  SKIP test_unwritable_repo_is_nogo: chmod-555 is a no-op under root (euid 0)")
        return
    repo = _trusted_repo(SLEEPER)
    os.chmod(repo, 0o555)
    try:
        res = _run(repo, "--check")
        if res.returncode != EX_NOGO:
            failures.append(f"[unwritable] expected {EX_NOGO}, got {res.returncode}: "
                            f"{res.stdout}")
    finally:
        os.chmod(repo, 0o755)


def test_fg_propagates_rc_and_holds_lock(failures):
    # rc propagation: the launcher execs the agent, so its exit code surfaces unchanged.
    repo = _trusted_repo(INSTANT_FAIL)
    res = _run(repo, "--fg", "boom")
    if res.returncode != 7:
        failures.append(f"[fg-rc] expected agent rc 7, got {res.returncode}: {res.stdout}")

    # lock lifetime: while a --fg run lives, a concurrent start must be refused; the exec
    # path keeps the lock via an inherited FD (a dropped set_inheritable would break this).
    repo2 = _trusted_repo(SLEEPER)
    env = dict(os.environ); env["ANS_TRUST_STORE"] = TRUST_STORE; env["ANS_TEST_MODE"] = "1"
    fg = subprocess.Popen([sys.executable, ANS_RUN, "--repo", repo2, "--fg", "go"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
                          stdin=subprocess.DEVNULL)
    time.sleep(1.5)  # let preflight finish and the exec happen
    res2 = _run(repo2, "again")
    if res2.returncode != EX_BUSY:
        failures.append(f"[fg-lock] expected {EX_BUSY} while --fg run lives, got "
                        f"{res2.returncode}: {res2.stdout}")
    fg.wait(timeout=30)
    res3 = _run(repo2, "--check")
    if res3.returncode != 0:
        failures.append(f"[fg-release] lock stale after --fg run ended: {res3.returncode}")


def test_simultaneous_starts_exactly_one_wins(failures):
    repo = _trusted_repo(SLEEPER)
    env = dict(os.environ); env["ANS_TRUST_STORE"] = TRUST_STORE; env["ANS_TEST_MODE"] = "1"
    # --no-watchdog: the winner's SLEEPER agent holds the lock directly and releases it the
    # moment it exits (bare-spawn lifecycle), so the lock-release timing below is deterministic;
    # the mutual-exclusion guarantee itself is watchdog-independent (lock taken in preflight).
    procs = [subprocess.Popen([sys.executable, ANS_RUN, "--repo", repo, "--no-watchdog", "go"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=env, stdin=subprocess.DEVNULL)
             for _ in range(2)]
    codes = [p.wait(timeout=30) for p in procs]
    if sorted(codes) != [0, EX_BUSY]:
        outs = [p.stdout.read() for p in procs]
        failures.append(f"[race] expected exactly one winner [0, {EX_BUSY}], got {codes}: {outs}")

    # While the winner's agent is still running, a third start must also be refused.
    res3 = _run(repo, "again")
    if res3.returncode != EX_BUSY:
        failures.append(f"[race-3rd] expected {EX_BUSY}, got {res3.returncode}: {res3.stdout}")

    # After the agent finishes, the kernel has released the lock — a new start wins.
    time.sleep(5)
    res4 = _run(repo, "--check")
    if res4.returncode != 0:
        failures.append(f"[race-release] expected lock released, got {res4.returncode}: "
                        f"{res4.stdout}")


def test_lock_released_after_agent_death(failures):
    # BARE-spawn lifecycle (--no-watchdog): an agent that dies instantly surfaces its rc and
    # must not leave a stale lock behind (no pidfiles). Under the DEFAULT watchdog wrap the
    # supervisor absorbs the crash (its own lifecycle is proven in test_watchdog.py), so this
    # immediate-rc/lock-release guarantee is asserted on the bare path.
    repo = _trusted_repo(INSTANT_FAIL)
    res = _run(repo, "--no-watchdog", "boom")
    if res.returncode != 7:
        failures.append(f"[crash] expected agent rc 7 surfaced, got {res.returncode}")
    res2 = _run(repo, "--check")
    if res2.returncode != 0:
        failures.append(f"[crash-release] lock stale after agent death: {res2.returncode}")


def test_default_watchdog_surfaces_instant_crash(failures):
    # Under the DEFAULT watchdog wrap, an agent that crashes instantly must still surface its
    # non-zero rc from ans-run (the watchdog polls the child fast and passes a crash through) —
    # NOT a false "Started in background". Proves the early-exit surfacing survives the wrap.
    repo = _trusted_repo(INSTANT_FAIL)
    res = _run(repo, "boom")
    if res.returncode != 7:
        failures.append(f"[wd-crash] default-wrap instant crash should surface rc 7, got "
                        f"{res.returncode}: {res.stdout}{res.stderr}")


def test_run_log_open_rejects_symlink(failures):
    # L4 (post-audit): the run-log holds the agent's RAW, unredacted stdout/stderr — a pre-planted
    # symlink at the computed log path must not redirect that write to an attacker-chosen file.
    # open_log() picks a deterministic (repo, cfg) -> path; plant a symlink there and open it with
    # the EXACT flags launcher.py uses, proving O_NOFOLLOW is actually wired in, not just present
    # in a comment.
    sys.path.insert(0, SKILL_ROOT)
    from agents_never_sleep.launcher import open_log
    work = tempfile.mkdtemp(prefix="ue-nofollow-")
    repo = os.path.join(work, "repo")
    os.makedirs(repo)
    target = os.path.join(work, "attacker-owned-file")
    log_path = open_log(repo, {})
    os.symlink(target, log_path)

    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        failures.append(f"[nofollow] open() followed a pre-planted symlink at {log_path!r}")
    except OSError:
        pass  # expected: O_NOFOLLOW refuses to open through the symlink

    if os.path.exists(target):
        failures.append("[nofollow] the symlink target was created/written despite O_NOFOLLOW")

    # Sanity: WITHOUT O_NOFOLLOW the same symlink WOULD be followed — proves the test is actually
    # discriminating, not just always-pass.
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    if not os.path.exists(target):
        failures.append("[nofollow] test sanity check failed: symlink should be followed without "
                        "O_NOFOLLOW")


def test_bg_start_reports_log_and_pid(failures):
    repo = _trusted_repo(SLEEPER)
    res = _run(repo, "go")
    if res.returncode != 0:
        failures.append(f"[bg] expected 0, got {res.returncode}: {res.stdout}{res.stderr}")
        return
    if "Started in background" not in res.stdout or "watch:" not in res.stdout:
        failures.append(f"[bg] missing PID/log/watch hint: {res.stdout}")
    # Ticket 03: the DEFAULT background launch is wrapped in the heartbeat watchdog.
    if "watchdog" not in res.stdout:
        failures.append(f"[bg] default launch should report the watchdog wrap: {res.stdout}")
    logs_dir = os.path.join(repo, ".unattended", "logs")
    if not (os.path.isdir(logs_dir) and os.listdir(logs_dir)):
        failures.append("[bg] no log file created under .unattended/logs")

    # The opt-out reports the bare launch (watchdog OFF).
    repo2 = _trusted_repo(SLEEPER)
    res2 = _run(repo2, "--no-watchdog", "go")
    if res2.returncode != 0 or "watchdog OFF" not in res2.stdout:
        failures.append(f"[bg] --no-watchdog should report watchdog OFF: "
                        f"{res2.returncode}: {res2.stdout}")
    time.sleep(5)  # let the sleeper finish so the tmp repo can be cleaned up


def _read_env_dump(repo: str, timeout: int = 15) -> str | None:
    path = os.path.join(repo, "env-dump.txt")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as fh:
                return fh.read().strip()
        time.sleep(0.2)
    return None


def test_trusted_target_user_requires_trust(failures):
    # L3 (post-audit): the root-guard must not read launcher.target_user from an UNTRUSTED config —
    # a malicious repo could otherwise pick which user identity a root-launched process assumes,
    # ahead of (and regardless of) the ordinary TOFU gate. trusted_target_user() is the single
    # decision point; prove it returns None pre-trust and the real value post-trust.
    sys.path.insert(0, SKILL_ROOT)
    from agents_never_sleep.launcher import trusted_target_user
    os.environ["ANS_TRUST_STORE"] = TRUST_STORE
    os.environ["ANS_TEST_MODE"] = "1"

    repo = _new_repo(SLEEPER, {"target_user": "some-target-user"})
    config_path = os.path.join(repo, ".claude", "agents-never-sleep.json")

    pre = trusted_target_user(repo, config_path)
    if pre is not None:
        failures.append(f"[root-guard] untrusted config's target_user was honored: {pre!r}")

    res = _run(repo, "--trust")
    if res.returncode != 0:
        failures.append(f"[root-guard] --trust failed: {res.stdout}{res.stderr}")

    post = trusted_target_user(repo, config_path)
    if post != "some-target-user":
        failures.append(f"[root-guard] trusted config's target_user not honored: {post!r}")

    # Changing the config (even just re-adding the same key) invalidates trust — the stale target
    # from before the edit must not survive either.
    _write_config(repo, {"agent_cmd": [os.path.join(repo, "fake-agent.sh")],
                         "allow_custom_agent": True, "min_disk_mb": 1,
                         "target_user": "a-different-user"})
    changed = trusted_target_user(repo, config_path)
    if changed is not None:
        failures.append(f"[root-guard] config change should invalidate trust, got {changed!r}")


def _read_consent_dump(repo: str, timeout: int = 15) -> str | None:
    path = os.path.join(repo, "consent-dump.txt")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as fh:
                return fh.read().strip()
        time.sleep(0.2)
    return None


def test_consent_frozen_into_child_env(failures):
    # t03: UE_CONSENT must carry the out-of-repo consent snapshot (keyed to the PRIMARY repo,
    # via consent_store.read) into the agent-CLI env, parallel to CLAUDE_UNATTENDED (M1) — present
    # on the plain detached path even when no consent was ever recorded (empty "{}", never unset).
    import unittest.mock

    from agents_never_sleep import consent_store

    consent_dir = tempfile.mkdtemp(prefix="ue-launcher-consent-")

    repo = _trusted_repo(CONSENT_ENV_DUMPER)
    with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir, "ANS_TEST_MODE": "1"}):
        consent_store.write(repo, actions={"redis_flush": {"allowed": True}}, by="tester")
    res = _run(repo, "--no-watchdog", "go", env_extra={"ANS_CONSENT_STORE": consent_dir})
    if res.returncode != 0:
        failures.append(f"[consent-freeze] expected 0, got {res.returncode}: {res.stdout}{res.stderr}")
    seen = _read_consent_dump(repo)
    try:
        got = json.loads(seen) if seen and seen != "UNSET" else None
    except ValueError:
        got = None
    if got != {"redis_flush": {"allowed": True}}:
        failures.append(f"[consent-freeze] UE_CONSENT should carry the recorded actions map, got {seen!r}")

    # A repo with no consent ever recorded still gets UE_CONSENT set — to an EMPTY map, never unset —
    # so enforce.py's env-only read never falls through to some other (agent-writable) source.
    repo2 = _trusted_repo(CONSENT_ENV_DUMPER)
    res2 = _run(repo2, "--no-watchdog", "go", env_extra={"ANS_CONSENT_STORE": consent_dir})
    if res2.returncode != 0:
        failures.append(f"[consent-freeze-empty] expected 0, got {res2.returncode}: {res2.stdout}{res2.stderr}")
    seen2 = _read_consent_dump(repo2)
    if seen2 != "{}":
        failures.append(f"[consent-freeze-empty] UE_CONSENT should be '{{}}' with no consent recorded, got {seen2!r}")


def test_claude_unattended_forced_on_all_detached_paths(failures):
    # M1 (post-audit): CLAUDE_UNATTENDED=1 used to reach the spawned agent only via watchdog.py's
    # own child env, so --no-watchdog (and fresh-session mode, which never touches watchdog.py at
    # all) could silently launch the agent with every CLAUDE_UNATTENDED-gated deny hook disarmed.
    # The launcher now forces it upstream of every detached branch — prove each branch actually
    # sees "1", not just the default watchdog-wrapped path that happened to work before.

    # --no-watchdog: the bare direct-spawn path.
    repo = _trusted_repo(ENV_DUMPER)
    res = _run(repo, "--no-watchdog", "go")
    if res.returncode != 0:
        failures.append(f"[unattended-nowd] expected 0, got {res.returncode}: {res.stdout}{res.stderr}")
    seen = _read_env_dump(repo)
    if seen != "1":
        failures.append(f"[unattended-nowd] CLAUDE_UNATTENDED should be '1', got {seen!r}")

    # Default (watchdog-wrapped) path — regression guard, this one worked before the fix too.
    repo2 = _trusted_repo(ENV_DUMPER)
    res2 = _run(repo2, "go")
    if res2.returncode != 0:
        failures.append(f"[unattended-wd] expected 0, got {res2.returncode}: {res2.stdout}{res2.stderr}")
    seen2 = _read_env_dump(repo2)
    if seen2 != "1":
        failures.append(f"[unattended-wd] CLAUDE_UNATTENDED should be '1', got {seen2!r}")

    # fresh-session mode: a THIRD detached path that bypasses watchdog.py entirely.
    repo3 = _trusted_repo(ENV_DUMPER, {"fresh_session_every": 1})
    res3 = _run(repo3, "go")
    if res3.returncode != 0:
        failures.append(f"[unattended-fresh] expected 0, got {res3.returncode}: "
                        f"{res3.stdout}{res3.stderr}")
    seen3 = _read_env_dump(repo3)
    if seen3 != "1":
        failures.append(f"[unattended-fresh] CLAUDE_UNATTENDED should be '1', got {seen3!r}")


def main() -> int:
    failures = []
    test_untrusted_config_headless_is_nogo(failures)
    test_changed_config_invalidates_trust(failures)
    test_custom_agent_requires_allowlist_optin(failures)
    test_no_config_launch_is_nogo(failures)
    test_preset_selection_and_autonomy_gate(failures)
    test_known_cli_capability_probe(failures)
    test_detached_interactive_permission_mode_is_nogo(failures)
    test_permission_marker_table_matches_cmd_variants(failures)
    test_enforcement_hooks_note_when_unwired_but_still_go(failures)
    test_enforcement_hooks_no_note_when_wired(failures)
    test_enforcement_hooks_note_when_referenced_script_missing(failures)
    test_default_watchdog_surfaces_instant_crash(failures)
    test_capability_restriction_appends_declared_tokens(failures)
    test_repo_shipped_binary_rejected(failures)
    test_preset_path_does_not_swap_probe_target(failures)
    test_trust_store_override_ignored_without_test_flag(failures)
    test_check_healthy_repo_is_go(failures)
    test_missing_agent_cli_is_nogo(failures)
    test_configured_credentials_missing_is_nogo(failures)
    test_target_user_mismatch_is_nogo(failures)
    test_blocking_host_check_gates_nonblocking_warns(failures)
    test_no_prompt_is_nogo(failures)
    test_invalid_config_json_is_nogo(failures)
    test_low_disk_is_nogo(failures)
    test_unwritable_repo_is_nogo(failures)
    test_fg_propagates_rc_and_holds_lock(failures)
    test_simultaneous_starts_exactly_one_wins(failures)
    test_lock_released_after_agent_death(failures)
    test_bg_start_reports_log_and_pid(failures)
    test_run_log_open_rejects_symlink(failures)
    test_claude_unattended_forced_on_all_detached_paths(failures)
    test_consent_frozen_into_child_env(failures)
    test_trusted_target_user_requires_trust(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — launcher guarantees not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — TOFU gate, allowlist, preset/autonomy gate, capability "
          "probe, pre-token NO-GO paths, atomic single-winner lock and kernel release "
          "all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
