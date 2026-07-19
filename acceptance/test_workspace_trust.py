#!/usr/bin/env python3
"""Fix 2 — auto-trust the target workspace at launch.

Claude Code raises an interactive 'do you trust this folder?' dialog for a workspace whose
`hasTrustDialogAccepted` is not true in ~/.claude.json, and while untrusted it IGNORES the project
allow-list — so every tool call prompts. In an unattended run no human is there to answer, and the
run HANGS (the field failure: a whole night lost to one unanswered prompt). `ans-run` therefore marks
the target repo trusted at launch, ADDITIVELY and FAIL-SAFE.

Cases:
  (a) MISSING ~/.claude.json     -> created, target repo pre-trusted, 0600.
  (b) entry present but false    -> flipped to true; all OTHER projects/fields preserved.
  (c) entry already true         -> idempotent no-op; file content otherwise unchanged.
  (d) malformed JSON             -> NOT raised, NOT clobbered (left byte-identical).
  (e) only the TARGET path changes (a sibling's false stays false).
  (f) wiring: a real `ans-run` launch trusts the workspace via the ANS_CLAUDE_JSON override.

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
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.launcher import ensure_workspace_trusted  # noqa: E402

ANS_RUN = os.path.join(SKILL_ROOT, "bin", "ans-run")


def _cfg(tmp):
    return os.path.join(tmp, ".claude.json")


def test_missing_file_created_pretrusted(failures):
    tmp = tempfile.mkdtemp(prefix="ue-trust-")
    path = _cfg(tmp)
    os.environ["ANS_CLAUDE_JSON"] = path
    try:
        ensure_workspace_trusted("/some/repo")
    finally:
        os.environ.pop("ANS_CLAUDE_JSON", None)
    if not os.path.exists(path):
        failures.append("[missing] ~/.claude.json was not created")
        return
    data = json.load(open(path))
    if data.get("projects", {}).get("/some/repo", {}).get("hasTrustDialogAccepted") is not True:
        failures.append(f"[missing] target repo not pre-trusted: {data}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        failures.append(f"[missing] created file mode is {oct(mode)}, expected 0o600")


def test_flip_false_preserves_everything_else(failures):
    tmp = tempfile.mkdtemp(prefix="ue-trust-")
    path = _cfg(tmp)
    original = {
        "numStartups": 42,
        "projects": {
            "/target/repo": {"hasTrustDialogAccepted": False, "allowedTools": ["Bash(git:*)"]},
            "/other/repo": {"hasTrustDialogAccepted": False, "lastCost": 1.23},
        },
    }
    with open(path, "w") as fh:
        json.dump(original, fh)
    os.environ["ANS_CLAUDE_JSON"] = path
    try:
        ensure_workspace_trusted("/target/repo")
    finally:
        os.environ.pop("ANS_CLAUDE_JSON", None)
    data = json.load(open(path))
    tgt = data["projects"]["/target/repo"]
    if tgt.get("hasTrustDialogAccepted") is not True:
        failures.append("[flip] target not flipped to true")
    if tgt.get("allowedTools") != ["Bash(git:*)"]:
        failures.append("[flip] target's other fields not preserved")
    if data.get("numStartups") != 42:
        failures.append("[flip] top-level sibling field lost")
    # (e) the OTHER project must be untouched
    other = data["projects"]["/other/repo"]
    if other.get("hasTrustDialogAccepted") is not False or other.get("lastCost") != 1.23:
        failures.append(f"[flip] a non-target project entry was mutated: {other}")


def test_already_true_is_noop(failures):
    tmp = tempfile.mkdtemp(prefix="ue-trust-")
    path = _cfg(tmp)
    original = {"projects": {"/t": {"hasTrustDialogAccepted": True, "x": 1}}, "keep": "me"}
    with open(path, "w") as fh:
        json.dump(original, fh)
    before = open(path).read()
    os.environ["ANS_CLAUDE_JSON"] = path
    try:
        ensure_workspace_trusted("/t")
    finally:
        os.environ.pop("ANS_CLAUDE_JSON", None)
    data = json.load(open(path))
    if data != original:
        failures.append(f"[noop] already-trusted file was modified: before={before!r} after={data!r}")


def test_malformed_json_not_clobbered(failures):
    tmp = tempfile.mkdtemp(prefix="ue-trust-")
    path = _cfg(tmp)
    junk = "{ this is not valid json ,,, "
    with open(path, "w") as fh:
        fh.write(junk)
    os.environ["ANS_CLAUDE_JSON"] = path
    try:
        # must NOT raise
        ensure_workspace_trusted("/repo")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[malformed] raised instead of failing safe: {exc}")
    finally:
        os.environ.pop("ANS_CLAUDE_JSON", None)
    if open(path).read() != junk:
        failures.append("[malformed] a file we could not parse was clobbered")


def test_concurrent_writers_do_not_lose_entries(failures):
    """Racing launches on the shared ~/.claude.json must not last-writer-wins away each other's
    entry — the read-modify-write is serialized under an exclusive flock. N threads each trust a
    DISTINCT repo against one config file; all N must survive."""
    import threading
    tmp = tempfile.mkdtemp(prefix="ue-trust-race-")
    path = _cfg(tmp)
    with open(path, "w") as fh:
        json.dump({"projects": {}}, fh)
    n = 24
    start = threading.Barrier(n)

    def worker(i):
        start.wait()  # release all threads together to maximize contention
        ensure_workspace_trusted(f"/repo/{i}")

    os.environ["ANS_CLAUDE_JSON"] = path
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        os.environ.pop("ANS_CLAUDE_JSON", None)
    data = json.load(open(path))
    present = data.get("projects", {})
    missing = [f"/repo/{i}" for i in range(n)
               if present.get(f"/repo/{i}", {}).get("hasTrustDialogAccepted") is not True]
    if missing:
        failures.append(f"[race] {len(missing)}/{n} entries lost under concurrent writers: {missing[:5]}")


def test_ans_run_launch_trusts_workspace(failures):
    """(f) Wiring: a real `ans-run` preflight marks the repo trusted in the (redirected) config."""
    repo = tempfile.mkdtemp(prefix="ue-trust-repo-")
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", repo, *a], check=True)
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("x\n")
    subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)
    # A fake agent + minimal trusted-config so preflight reaches GO; we only care that trust was set.
    agent = os.path.join(repo, "fa.sh")
    with open(agent, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
    creds = os.path.join(repo, "c.json")
    with open(creds, "w") as fh:
        fh.write('{"F": "x"}')
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "agents-never-sleep.json"), "w") as fh:
        json.dump({"launcher": {"agent_cmd": [agent], "allow_custom_agent": True,
                                "credentials_paths": [creds], "min_disk_mb": 1}}, fh)

    trust_dir = tempfile.mkdtemp(prefix="ue-trust-store-")
    claude_json = os.path.join(tempfile.mkdtemp(prefix="ue-trust-cfg-"), ".claude.json")
    env = dict(os.environ, ANS_TRUST_STORE=os.path.join(trust_dir, "t.json"),
               ANS_TEST_MODE="1", ANS_CLAUDE_JSON=claude_json)
    subprocess.run([sys.executable, ANS_RUN, "--repo", repo, "--trust"], env=env,
                   capture_output=True, text=True)
    res = subprocess.run([sys.executable, ANS_RUN, "--repo", repo, "go"], env=env,
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    real_repo = os.path.realpath(repo)
    if not os.path.exists(claude_json):
        failures.append(f"[wiring] ans-run did not create the redirected ~/.claude.json: {res.stdout}{res.stderr}")
        return
    data = json.load(open(claude_json))
    if data.get("projects", {}).get(real_repo, {}).get("hasTrustDialogAccepted") is not True:
        failures.append(f"[wiring] ans-run launch did not trust the workspace: {data}")
    if "trust: marked workspace" not in res.stdout:
        failures.append(f"[wiring] no trust preflight log line: {res.stdout}")


def main() -> int:
    failures = []
    test_missing_file_created_pretrusted(failures)
    test_flip_false_preserves_everything_else(failures)
    test_already_true_is_noop(failures)
    test_malformed_json_not_clobbered(failures)
    test_concurrent_writers_do_not_lose_entries(failures)
    test_ans_run_launch_trusts_workspace(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — workspace auto-trust preflight not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — workspace trusted additively & fail-safe; missing created, "
          "false flipped, true no-op, malformed untouched, siblings preserved, wired into ans-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
