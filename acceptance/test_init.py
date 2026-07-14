"""`ans-run init` / `ans-run install-hooks` subcommand dispatch.

`launcher.main()` treats an unrecognised first argv token as the agent prompt (the default
`ans-run PROMPT...` path). `init` and `install-hooks` must be routed to `init_cmd` BEFORE that
prompt path claims them, so `ans-run init --repo X` never gets sent to an agent CLI as a prompt.
"""
import os
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
    launcher.init_cmd.run_init = _fake_run_init
    sys.argv = ["ans-run", "init", "--repo", "/tmp/x"]
    rc = launcher.main()
    assert rc == 0 and seen.get("init") == ["--repo", "/tmp/x"], seen


def test_dispatch_routes_install_hooks():
    # Same routing, for the `install-hooks` subcommand.
    from agents_never_sleep import launcher
    seen = {}

    def _fake_run_install_hooks(argv):
        seen["install-hooks"] = argv
        return 0
    launcher.init_cmd.run_install_hooks = _fake_run_install_hooks
    sys.argv = ["ans-run", "install-hooks", "--yes"]
    rc = launcher.main()
    assert rc == 0 and seen.get("install-hooks") == ["--yes"], seen


def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"ok   {name}")
            except AssertionError as e: fails += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    _run()
