#!/usr/bin/env python3
"""ANS-OSS F4 — Tokonomix-delegated routing (managed tier): token-ref indirection for
preset `env` values in bin/ans-run.

Proves the guarantees the managed tier exists for:
  * `env:VAR` resolves from the LAUNCHER's environment into the child env, BEFORE the
    capability probe (the probe sees the resolved value — probe == spawn rule);
  * a missing `env:VAR` is a blocking NO-GO (exit 64), never a silent empty value;
  * a `vault:` ref without the vault integration enabled is a blocking NO-GO;
  * a LITERAL high-entropy value (a key pasted into the repo config) is flagged with a
    warning but still runs, and the literal value never appears in the launcher output;
  * the resolved env reaches the actually-spawned process (asserted via a fake agent
    that writes the env var it received to a file).

Exit 0 = GREEN.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
ANS_RUN = os.path.join(SKILL_ROOT, "bin", "ans-run")

EX_NOGO = 64

# Per-suite-run trust store: tests share one store file outside any repo.
TRUST_DIR = tempfile.mkdtemp(prefix="ue-managed-trust-")
TRUST_STORE = os.path.join(TRUST_DIR, "trusted.json")

# A harmless stand-in for "a key pasted literally": long, token-shaped, high-entropy.
FAKE_LITERAL_KEY = "ZJ3kQ9xPm2Lr7VtB8nWc4YhD6fGs1AeU"
RESOLVED_VALUE = "tok-resolved-value-12345"


def _write_config(repo: str, launcher: dict, top_extra: dict | None = None) -> None:
    cfg_dir = os.path.join(repo, ".claude")
    os.makedirs(cfg_dir, exist_ok=True)
    doc = {"launcher": launcher}
    doc.update(top_extra or {})
    with open(os.path.join(cfg_dir, "agents-never-sleep.json"), "w") as fh:
        json.dump(doc, fh)


def _new_repo(agent_script: str) -> str:
    repo = tempfile.mkdtemp(prefix="ue-managed-")
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
    return repo


def _preset_repo(agent_script: str, env: dict, top_extra: dict | None = None) -> str:
    """A trusted repo with ONE preset 'managed' using the given env map. The fake agent is
    path-bearing, so allow_custom_agent is set; agent_probe_args gives it a probe."""
    repo = _new_repo(agent_script)
    _write_config(repo, {
        "agents": {"managed": {"cmd": [os.path.join(repo, "fake-agent.sh")],
                               "autonomy_confirmed": True, "env": env}},
        "default_agent": "managed",
        "allow_custom_agent": True,
        "agent_probe_args": "--version",
        "min_disk_mb": 1,
        "credentials_paths": [os.path.join(repo, "README.md")],
    }, top_extra)
    res = _run(repo, "--trust")
    assert res.returncode == 0, f"--trust failed: {res.stdout}{res.stderr}"
    return repo


def _run(repo: str, *extra: str, env_extra: dict | None = None,
         timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ANS_TRUST_STORE"] = TRUST_STORE
    env["ANS_TEST_MODE"] = "1"
    env.pop("SRC_TOKEN", None)  # tests control this var explicitly
    env.update(env_extra or {})
    return subprocess.run([sys.executable, ANS_RUN, "--repo", repo, *extra],
                          capture_output=True, text=True, timeout=timeout, env=env)


# Probe passes ONLY when the resolved value reached the probe's environment — this is the
# "the probe sees it" assertion: a launcher that resolved after the probe would exit 9 here.
PROBE_CHECKS_ENV = ("#!/bin/sh\n"
                    'if [ "$1" = "--version" ]; then\n'
                    f'  [ "$GATEWAY_KEY" = "{RESOLVED_VALUE}" ] && exit 0 || exit 9\n'
                    "fi\n"
                    "exit 0\n")

# The spawned agent writes the env var it actually received next to itself.
ECHO_TO_FILE = ("#!/bin/sh\n"
                '[ "$1" = "--version" ] && exit 0\n'
                'printf "%s" "$GATEWAY_KEY" > "$(dirname "$0")/seen-env.txt"\n'
                "exit 0\n")


def test_env_ref_resolves_and_probe_sees_it(failures):
    repo = _preset_repo(PROBE_CHECKS_ENV, {"GATEWAY_KEY": "env:SRC_TOKEN"})
    res = _run(repo, "--check", env_extra={"SRC_TOKEN": RESOLVED_VALUE})
    if res.returncode != 0:
        failures.append(f"[env-ref] expected GO with SRC_TOKEN set, got "
                        f"{res.returncode}: {res.stdout}{res.stderr}")
    if "resolved via env" not in res.stdout:
        failures.append(f"[env-ref] resolution not reported: {res.stdout}")
    if RESOLVED_VALUE in res.stdout + res.stderr:
        failures.append("[env-ref] the RESOLVED VALUE leaked into launcher output")


def test_missing_env_ref_is_nogo(failures):
    repo = _preset_repo(PROBE_CHECKS_ENV, {"GATEWAY_KEY": "env:SRC_TOKEN"})
    res = _run(repo, "--check")  # SRC_TOKEN deliberately unset
    if res.returncode != EX_NOGO:
        failures.append(f"[missing-env] expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}")
    if "SRC_TOKEN" not in res.stdout:
        failures.append(f"[missing-env] refusal does not name the unresolved ref: "
                        f"{res.stdout}")


def test_vault_ref_without_vault_integration_is_nogo(failures):
    repo = _preset_repo(PROBE_CHECKS_ENV,
                        {"GATEWAY_KEY": "vault:secret/tokonomix/key#token"})
    res = _run(repo, "--check")  # no integrations.vault in the config
    if res.returncode != EX_NOGO:
        failures.append(f"[vault-ref] expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}")
    if "vault" not in res.stdout.lower():
        failures.append(f"[vault-ref] refusal does not explain the vault gate: {res.stdout}")


def test_literal_high_entropy_value_warns_but_runs(failures):
    # The probe must NOT require GATEWAY_KEY here — use a permissive agent.
    agent = "#!/bin/sh\nexit 0\n"
    repo = _preset_repo(agent, {"OPENAI_API_KEY": FAKE_LITERAL_KEY,
                                "OPENAI_BASE_URL": "https://gateway.example.test/v1"})
    res = _run(repo, "--check")
    if res.returncode != 0:
        failures.append(f"[literal] expected GO (warning only), got {res.returncode}: "
                        f"{res.stdout}")
    if "LITERAL secret" not in res.stdout:
        failures.append(f"[literal] high-entropy literal not flagged: {res.stdout}")
    if "(with warnings)" not in res.stdout:
        failures.append(f"[literal] GO verdict should carry the warning marker: {res.stdout}")
    if FAKE_LITERAL_KEY in res.stdout + res.stderr:
        failures.append("[literal] the literal value itself leaked into launcher output")
    # The plain URL next to it must NOT be flagged (no false positive on non-secrets).
    if res.stdout.count("LITERAL secret") != 1:
        failures.append(f"[literal] expected exactly one flagged value: {res.stdout}")


def test_failed_ref_never_falls_back_to_launcher_var(failures):
    # Council finding (2026-06-10): config maps GATEWAY_KEY to env:SRC_TOKEN which is
    # UNSET, while the launcher's own environment carries a real same-named GATEWAY_KEY.
    # A REAL launch (not --check) must exit 64 and the agent must NEVER run — a silent
    # fallback to the inherited launcher value would ship the wrong credential.
    repo = _preset_repo(ECHO_TO_FILE, {"GATEWAY_KEY": "env:SRC_TOKEN"})
    res = _run(repo, "go", env_extra={"GATEWAY_KEY": "launcher-own-value-99"})
    if res.returncode != EX_NOGO:
        failures.append(f"[no-fallback] expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}{res.stderr}")
    if os.path.exists(os.path.join(repo, "seen-env.txt")):
        failures.append("[no-fallback] the agent RAN despite an unresolved token-ref")


def test_empty_env_ref_gates_like_missing(failures):
    # An empty-string env var must gate exactly like an unset one — never a silent
    # empty value in the child env.
    repo = _preset_repo(PROBE_CHECKS_ENV, {"GATEWAY_KEY": "env:SRC_TOKEN"})
    res = _run(repo, "--check", env_extra={"SRC_TOKEN": ""})
    if res.returncode != EX_NOGO:
        failures.append(f"[empty-env] expected {EX_NOGO}, got {res.returncode}: "
                        f"{res.stdout}")


def test_resolved_env_reaches_spawned_process(failures):
    repo = _preset_repo(ECHO_TO_FILE, {"GATEWAY_KEY": "env:SRC_TOKEN"})
    res = _run(repo, "--fg", "go", env_extra={"SRC_TOKEN": RESOLVED_VALUE})
    if res.returncode != 0:
        failures.append(f"[spawn] --fg run failed: {res.returncode}: "
                        f"{res.stdout}{res.stderr}")
        return
    seen = os.path.join(repo, "seen-env.txt")
    try:
        with open(seen, "r", encoding="utf-8") as fh:
            got = fh.read()
    except OSError:
        failures.append("[spawn] fake agent never ran (no seen-env.txt)")
        return
    if got != RESOLVED_VALUE:
        failures.append(f"[spawn] spawned process saw {got!r}, expected the resolved value")


def main() -> int:
    failures = []
    test_env_ref_resolves_and_probe_sees_it(failures)
    test_missing_env_ref_is_nogo(failures)
    test_vault_ref_without_vault_integration_is_nogo(failures)
    test_literal_high_entropy_value_warns_but_runs(failures)
    test_failed_ref_never_falls_back_to_launcher_var(failures)
    test_empty_env_ref_gates_like_missing(failures)
    test_resolved_env_reaches_spawned_process(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — managed-tier env guarantees not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — env:/vault: token-refs resolve into probe+spawn env, "
          "missing refs are a blocking NO-GO, pasted literals are flagged, and no "
          "resolved value ever reaches launcher output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
