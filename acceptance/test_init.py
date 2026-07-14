"""`ans-run init` / `ans-run install-hooks` subcommand dispatch.

`launcher.main()` treats an unrecognised first argv token as the agent prompt (the default
`ans-run PROMPT...` path). `init` and `install-hooks` must be routed to `init_cmd` BEFORE that
prompt path claims them, so `ans-run init --repo X` never gets sent to an agent CLI as a prompt.
"""
import json
import os
import pathlib
import sys
import tempfile
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)


def test_dispatch_routes_init_and_install_hooks():
    # `init`/`install-hooks` as first token route to init_cmd, NOT the prompt path.
    from agents_never_sleep import init_cmd
    assert hasattr(init_cmd, "run_init")
    assert hasattr(init_cmd, "run_install_hooks")
    # launcher.main dispatches on argv[1] without treating it as a prompt:
    from agents_never_sleep import launcher
    seen = {}

    def _fake_run_init(argv):
        seen["init"] = argv
        return 0
    orig = launcher.init_cmd.run_init
    launcher.init_cmd.run_init = _fake_run_init
    try:
        sys.argv = ["ans-run", "init", "--repo", "/tmp/x"]
        rc = launcher.main()
    finally:
        launcher.init_cmd.run_init = orig   # module-level patch — must not leak into later tests
    assert rc == 0 and seen.get("init") == ["--repo", "/tmp/x"], seen


def test_dispatch_routes_install_hooks():
    # Same routing, for the `install-hooks` subcommand.
    from agents_never_sleep import launcher
    seen = {}

    def _fake_run_install_hooks(argv):
        seen["install-hooks"] = argv
        return 0
    orig = launcher.init_cmd.run_install_hooks
    launcher.init_cmd.run_install_hooks = _fake_run_install_hooks
    try:
        sys.argv = ["ans-run", "install-hooks", "--yes"]
        rc = launcher.main()
    finally:
        launcher.init_cmd.run_install_hooks = orig   # module-level patch — must not leak into later tests
    assert rc == 0 and seen.get("install-hooks") == ["--yes"], seen


def test_select_harness_single_and_none():
    from agents_never_sleep import init_cmd
    assert init_cmd.select_harness(["gemini"], assume_yes=True, out=lambda *a, **k: None) == "gemini"
    assert init_cmd.select_harness([], assume_yes=True, out=lambda *a, **k: None) is None


def test_select_harness_many_yes_prefers_claude():
    from agents_never_sleep import init_cmd
    got = init_cmd.select_harness(["gemini", "claude"], assume_yes=True, out=lambda *a, **k: None)
    assert got == "claude", got


def test_select_harness_eoferror_fallback_to_top_ranked():
    # EOFError on closed/non-TTY stdin: no hang, fall back to highest-maturity candidate.
    from agents_never_sleep import init_cmd
    got = init_cmd.select_harness(
        ["gemini", "claude"],
        assume_yes=False,
        ask=lambda *a, **k: (_ for _ in ()).throw(EOFError()),
        out=lambda *a, **k: None
    )
    # claude ranks higher (live-verified first), so EOFError should default to it.
    assert got == "claude", got


def test_maturity_lines_flags_unverified():
    # Assert on the EXACT suffix, not a substring both branches satisfy ("NOT live-verified"
    # contains "live-verified"). Pass the agent-CLI name — the capabilities key.
    from agents_never_sleep import init_cmd
    claude = " ".join(init_cmd.maturity_lines("claude")).lower()
    gemini = " ".join(init_cmd.maturity_lines("gemini")).lower()
    assert "(live-verified)" in claude and "not live-verified" not in claude
    assert "not live-verified" in gemini  # honest caveat present for the unproven adapter


def test_detect_gates_go_and_rust():
    from agents_never_sleep import preflight
    go = tempfile.mkdtemp(); open(os.path.join(go, "go.mod"), "w").write("module x\n")
    rs = tempfile.mkdtemp(); open(os.path.join(rs, "Cargo.toml"), "w").write("[package]\n")
    go_gates = dict(preflight._detect_gates(go))
    rs_gates = dict(preflight._detect_gates(rs))
    assert go_gates.get("go-test") == ["go", "test", "./..."], go_gates
    assert rs_gates.get("cargo-test") == ["cargo", "test"], rs_gates


def test_run_init_scaffolds_and_never_touches_any_harness_config():
    # The invariant names THREE global harness configs — assert ALL are byte-unchanged, not just Claude.
    from agents_never_sleep import init_cmd, config
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo], check=True)
    home = tempfile.mkdtemp()
    guarded = {
        ".claude/settings.json": '{"permissions":{}}',
        ".gemini/settings.json": '{"hooks":{}}',
        ".codex/hooks.json": '{"hooks":[]}',
    }
    before = {}
    for rel, body in guarded.items():
        pth = pathlib.Path(home, rel); pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(body); before[rel] = pth.read_bytes()
    env_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        rc = init_cmd.run_init(["--repo", repo, "--yes"])
    finally:
        os.environ["HOME"] = env_home or ""
    assert rc == 0, rc
    cfg = json.load(open(config.config_path(repo)))
    assert cfg["schema_version"]  # a real config landed (assert a real field, not mere existence)
    assert cfg["launcher"]["default_agent"] in (None, *cfg["launcher"]["agents"].keys())
    for rel, prior in before.items():
        assert pathlib.Path(home, rel).read_bytes() == prior, f"ans init MUST NOT touch ~/{rel}"


def test_run_init_stops_unless_force():
    from agents_never_sleep import init_cmd, config
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo], check=True)
    assert init_cmd.run_init(["--repo", repo, "--yes"]) == 0
    marker = config.config_path(repo)
    stamp = os.path.getmtime(marker)
    assert init_cmd.run_init(["--repo", repo, "--yes"]) != 0   # second run refuses
    assert os.path.getmtime(marker) == stamp, "config must be byte-untouched without --force"
    assert init_cmd.run_init(["--repo", repo, "--yes", "--force"]) == 0


def test_run_init_requires_git():
    from agents_never_sleep import init_cmd
    plain = tempfile.mkdtemp()  # not a git repo
    assert init_cmd.run_init(["--repo", plain, "--yes"]) != 0


def test_installed_clis_subset_of_allowlist():
    # Documents WHY run_init filters `installed` to ALLOWLIST: today it's a no-op (installed_clis
    # returns only AGENT_CLIS keys == ALLOWLIST). If someone later widens installed_clis, THIS fails
    # loudly — so the filter is not mistaken for dead code. (Consensus round 3 recommendation.)
    from agents_never_sleep import agent_clis
    assert set(agent_clis.installed_clis()) <= set(agent_clis.ALLOWLIST)


def test_interactive_records_trust_in_ans_store_not_home():
    # Interactive init records TOFU trust so a detached run can start — and it writes ONLY the ANS
    # trust store, never ~/.claude. Force the interactive branch deterministically.
    from agents_never_sleep import init_cmd, trust, config
    repo = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", repo], check=True)
    home = tempfile.mkdtemp(); (pathlib.Path(home, ".claude")).mkdir()
    settings = pathlib.Path(home, ".claude", "settings.json"); settings.write_text("{}")
    before = settings.read_bytes()
    orig = init_cmd.agent_clis.installed_clis
    init_cmd.agent_clis.installed_clis = lambda: ["claude"]   # single → no prompt
    old = os.environ.get("HOME"); os.environ["HOME"] = home
    try:
        rc = init_cmd.run_init(["--repo", repo])   # NO --yes → interactive path
        assert rc == 0
        # is_trusted resolves the trust-store path from HOME at call time (like record_trust) —
        # check it while HOME still points at the test home, not the real one restored below.
        assert trust.is_trusted(repo, config.config_path(repo)) if hasattr(trust, "is_trusted") else True
    finally:
        os.environ["HOME"] = old or ""
        init_cmd.agent_clis.installed_clis = orig
    assert settings.read_bytes() == before, "interactive trust write must NOT touch ~/.claude"
    # confirm the trust store lives under the ANS config dir, not HOME/.claude:
    assert not any(pathlib.Path(home, ".claude").glob("*trust*"))


def test_yes_without_trust_does_not_record_trust():
    # Security crux: "skip prompts" (--yes) must NOT silently mean "authorize execution" (trust).
    # A bare --yes (no --trust) must leave the repo untrusted in the ANS trust store.
    from agents_never_sleep import init_cmd, trust, config
    repo = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", repo], check=True)
    home = tempfile.mkdtemp()
    old = os.environ.get("HOME"); os.environ["HOME"] = home
    try:
        rc = init_cmd.run_init(["--repo", repo, "--yes"])
        assert rc == 0, rc
        # Check BEFORE restoring HOME — trust paths resolve HOME at call time.
        assert not trust.is_trusted(repo, config.config_path(repo)), \
            "--yes without --trust must NOT record trust (skip-prompts != authorize-execution)"
    finally:
        os.environ["HOME"] = old or ""


def test_yes_with_trust_records_trust():
    # The explicit CI one-shot opt-in: --yes --trust DOES record trust, so a detached run can start.
    from agents_never_sleep import init_cmd, trust, config
    repo = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", repo], check=True)
    home = tempfile.mkdtemp()
    old = os.environ.get("HOME"); os.environ["HOME"] = home
    try:
        rc = init_cmd.run_init(["--repo", repo, "--yes", "--trust"])
        assert rc == 0, rc
        assert trust.is_trusted(repo, config.config_path(repo)), \
            "--yes --trust must record trust for the repo"
    finally:
        os.environ["HOME"] = old or ""


def test_demo_tickets_are_inert():
    from agents_never_sleep import init_cmd, config, tickets
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo], check=True)
    init_cmd.run_init(["--repo", repo, "--yes"])
    examples = os.path.join(repo, ".claude", "ans-examples")
    written = [f for f in os.listdir(examples) if f.endswith(".md")]
    assert 2 <= len(written) <= 3, written
    # Prove non-discovery against the REAL default source <repo>/tickets (run.py:631), with a genuine
    # ticket present — load_tickets must return the real one and NEVER an example.
    tickets_dir = os.path.join(repo, "tickets")
    os.makedirs(tickets_dir, exist_ok=True)
    open(os.path.join(tickets_dir, "real.md"), "w").write("---\ntitle: real\n---\nbody\n")
    loaded = tickets.load_tickets(tickets_dir)
    titles = [t.title for t in loaded]
    assert titles == ["real"], titles  # exactly the real ticket; zero examples leaked in


def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"ok   {name}")
            except AssertionError as e: fails += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    _run()
