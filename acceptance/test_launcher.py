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
                          capture_output=True, text=True, timeout=timeout, env=env)


def _trusted_repo(agent_script: str, launcher_extra: dict | None = None) -> str:
    repo = _new_repo(agent_script, launcher_extra)
    res = _run(repo, "--trust")
    assert res.returncode == 0, f"--trust failed: {res.stdout}{res.stderr}"
    return repo


SLEEPER = "#!/bin/sh\nsleep 4\nexit 0\n"
INSTANT_FAIL = "#!/bin/sh\nexit 7\n"


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
            "agents": {"claude": {"cmd": ["claude", "-p"], "autonomy_confirmed": True,
                                  "env": {}}},
            "default_agent": "claude", "min_disk_mb": 1,
        })
        _run(repo, "--trust")
        res = _run(repo, "--check",
                   env_extra={"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        if res.returncode != want:
            failures.append(f"[probe rc={probe_rc}] expected {want}, got "
                            f"{res.returncode}: {res.stdout}")


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
        "agents": {"claude": {"cmd": ["claude", "-p"], "autonomy_confirmed": True,
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
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
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
    procs = [subprocess.Popen([sys.executable, ANS_RUN, "--repo", repo, "go"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=env)
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
    # An agent that dies instantly must not leave a stale lock behind (no pidfiles).
    repo = _trusted_repo(INSTANT_FAIL)
    res = _run(repo, "boom")
    if res.returncode != 7:
        failures.append(f"[crash] expected agent rc 7 surfaced, got {res.returncode}")
    res2 = _run(repo, "--check")
    if res2.returncode != 0:
        failures.append(f"[crash-release] lock stale after agent death: {res2.returncode}")


def test_bg_start_reports_log_and_pid(failures):
    repo = _trusted_repo(SLEEPER)
    res = _run(repo, "go")
    if res.returncode != 0:
        failures.append(f"[bg] expected 0, got {res.returncode}: {res.stdout}{res.stderr}")
        return
    if "Started in background" not in res.stdout or "watch:" not in res.stdout:
        failures.append(f"[bg] missing PID/log/watch hint: {res.stdout}")
    logs_dir = os.path.join(repo, ".unattended", "logs")
    if not (os.path.isdir(logs_dir) and os.listdir(logs_dir)):
        failures.append("[bg] no log file created under .unattended/logs")
    time.sleep(5)  # let the sleeper finish so the tmp repo can be cleaned up


def main() -> int:
    failures = []
    test_untrusted_config_headless_is_nogo(failures)
    test_changed_config_invalidates_trust(failures)
    test_custom_agent_requires_allowlist_optin(failures)
    test_no_config_launch_is_nogo(failures)
    test_preset_selection_and_autonomy_gate(failures)
    test_known_cli_capability_probe(failures)
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
