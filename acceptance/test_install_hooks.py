"""`ans-run install-hooks` — real diff+confirm write into the host settings file, and the
`hooks_wired()` detector it relies on to close the loop.

All tests run under an isolated $HOME (tempdir) so they can never touch the operator's real
~/.claude/settings.json. One test snapshots the REAL file (if present) before overriding HOME
and asserts it is byte-identical afterwards, as an extra belt-and-braces check.
"""
import json
import os
import pathlib
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _with_home(fn):
    """Run fn(home) under an isolated HOME, always restoring the real HOME afterwards."""
    home = tempfile.mkdtemp()
    old = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        return fn(home)
    finally:
        os.environ["HOME"] = old or ""


def test_hooks_wired_false_when_settings_file_missing():
    from agents_never_sleep import init_cmd

    def run(home):
        assert init_cmd.hooks_wired("claude", home=home) is False
    _with_home(run)


def test_hooks_wired_false_on_malformed_json():
    from agents_never_sleep import init_cmd

    def run(home):
        p = pathlib.Path(home, ".claude", "settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json")
        assert init_cmd.hooks_wired("claude", home=home) is False
    _with_home(run)


def test_hooks_wired_false_on_unrelated_settings():
    from agents_never_sleep import init_cmd

    def run(home):
        p = pathlib.Path(home, ".claude", "settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]},
                                  "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                  "command": "/some/other/hook.sh"}]}]}}))
        assert init_cmd.hooks_wired("claude", home=home) is False
    _with_home(run)


def test_hooks_wired_false_when_referenced_script_missing():
    # Moved/deleted install (Task B): the settings file names every ANS hook marker, but the
    # script path it points at no longer exists on disk. This must count as NOT wired — a
    # settings file surviving an install move/delete is dead enforcement, not live enforcement.
    from agents_never_sleep import init_cmd

    def run(home):
        p = pathlib.Path(home, ".claude", "settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
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
        p.write_text(json.dumps(data))
        assert init_cmd.hooks_wired("claude", home=home) is False
    _with_home(run)


def test_install_hooks_yes_writes_and_hooks_wired_true_after():
    from agents_never_sleep import init_cmd

    def run(home):
        rc = init_cmd.run_install_hooks(["--harness", "claude", "--yes"])
        assert rc == 0, rc
        assert init_cmd.hooks_wired("claude", home=home) is True
        data = json.loads(pathlib.Path(home, ".claude", "settings.json").read_text())
        cmds = json.dumps(data)
        assert "stop_guard.sh" in cmds
        assert "deny_irreversible.sh" in cmds
        assert "deny_ask.sh" in cmds
        # placeholder must be gone, replaced by a real absolute path to THIS install's hooks/
        assert "ABSOLUTE/PATH/TO" not in cmds
        assert REPO_ROOT in cmds
    _with_home(run)


def test_install_hooks_recovers_when_settings_has_stale_dead_entry_for_same_marker():
    # Moved/deleted-install recovery flow (Task B): the settings file already has a STALE entry
    # for a marker (old moved-away script path) when install-hooks runs. The merge appends the
    # fresh LIVE entry alongside it (same marker, different command), so the file ends up with
    # BOTH a dead and a live command for that marker. hooks_wired()'s self-verify must count the
    # marker as wired because the live one exists, not bail out because the stale one it happens
    # to hit first doesn't. Before the fix, next()-first-match picked the stale entry and
    # hooks_wired() stayed False forever — a dead end in exactly this recovery path.
    from agents_never_sleep import init_cmd

    def run(home):
        p = pathlib.Path(home, ".claude", "settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
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
        p.write_text(json.dumps(data))
        assert init_cmd.hooks_wired("claude", home=home) is False  # sanity: stale-only is not wired

        rc = init_cmd.run_install_hooks(["--harness", "claude", "--yes"])
        assert rc == 0, rc
        assert init_cmd.hooks_wired("claude", home=home) is True

        # both the stale dead entry and the fresh live entry must be present afterward — the
        # merge is additive (different command strings), not a replace.
        after = json.loads(p.read_text())
        cmds = json.dumps(after)
        assert missing in cmds, "stale entry must survive the merge unchanged"
        assert REPO_ROOT in cmds, "fresh live entry must have been added"
    _with_home(run)


def test_install_hooks_second_run_is_idempotent_no_duplicates():
    from agents_never_sleep import init_cmd

    def run(home):
        assert init_cmd.run_install_hooks(["--harness", "claude", "--yes"]) == 0
        first = pathlib.Path(home, ".claude", "settings.json").read_text()
        assert init_cmd.run_install_hooks(["--harness", "claude", "--yes"]) == 0
        second = pathlib.Path(home, ".claude", "settings.json").read_text()
        assert first == second, "second install-hooks run must be a byte-identical no-op"
        data = json.loads(second)
        # exactly one PreToolUse/Bash entry, exactly one deny_irreversible.sh command in it —
        # a prior bug wrote a bare/duplicate entry; guard against regressing that.
        bash_groups = [g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"]
        assert len(bash_groups) == 1, bash_groups
        deny_cmds = [h["command"] for h in bash_groups[0]["hooks"] if "deny_irreversible.sh" in h["command"]]
        assert len(deny_cmds) == 1, deny_cmds
    _with_home(run)


def test_install_hooks_preserves_existing_permissions_and_unrelated_hook():
    from agents_never_sleep import init_cmd

    def run(home):
        p = pathlib.Path(home, ".claude", "settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm -rf /:*)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "/opt/other/pre_edit.sh"}]}
                ]
            },
        }
        p.write_text(json.dumps(existing, indent=2))
        assert init_cmd.run_install_hooks(["--harness", "claude", "--yes"]) == 0
        data = json.loads(p.read_text())
        assert data["permissions"] == existing["permissions"], "permissions block must survive byte-for-byte"
        matchers = {g.get("matcher") for g in data["hooks"]["PreToolUse"]}
        assert "Edit" in matchers, "pre-existing unrelated PreToolUse hook must survive the merge"
        edit_group = next(g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "Edit")
        assert edit_group["hooks"] == [{"type": "command", "command": "/opt/other/pre_edit.sh"}]
        assert "Bash" in matchers  # the new ANS hook was added alongside it
    _with_home(run)


def test_install_hooks_declines_without_yes_and_never_hangs():
    # Non-interactive (no TTY under the test harness) + no --yes must decline and return promptly —
    # never call input() and block. Nothing is written.
    from agents_never_sleep import init_cmd

    def run(home):
        rc = init_cmd.run_install_hooks(["--harness", "claude"])
        assert rc == 0, rc
        assert not pathlib.Path(home, ".claude", "settings.json").exists()
        assert init_cmd.hooks_wired("claude", home=home) is False
    _with_home(run)


def test_install_hooks_unknown_harness_rejected():
    from agents_never_sleep import init_cmd
    assert init_cmd.run_install_hooks(["--harness", "nope"]) != 0


def test_install_hooks_no_flag_lists_all_targets_and_writes_nothing():
    from agents_never_sleep import init_cmd
    import io, contextlib

    def run(home):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = init_cmd.run_install_hooks([])
        assert rc == 0, rc
        out = buf.getvalue()
        for name in init_cmd._BLAST_RADIUS:
            assert name in out, f"{name} missing from no-flag target listing"
        assert not pathlib.Path(home, ".claude", "settings.json").exists()
    _with_home(run)


def test_install_hooks_accepts_cursor_target_without_writing():
    # cursor is a valid, accepted target (enforcement-only platform) but is project-local with a
    # different write model — install-hooks must accept it (rc==0) without guessing a repo path or
    # writing anything under HOME.
    from agents_never_sleep import init_cmd

    def run(home):
        rc = init_cmd.run_install_hooks(["--harness", "cursor", "--yes"])
        assert rc == 0, rc
        assert not pathlib.Path(home, ".cursor").exists()
    _with_home(run)


def test_gemini_and_codex_also_wire_and_verify():
    from agents_never_sleep import init_cmd

    def run(home):
        assert init_cmd.run_install_hooks(["--harness", "gemini", "--yes"]) == 0
        assert init_cmd.hooks_wired("gemini", home=home) is True
        assert init_cmd.run_install_hooks(["--harness", "codex", "--yes"]) == 0
        assert init_cmd.hooks_wired("codex", home=home) is True
        # blast radius: writing gemini/codex must not touch claude's settings file.
        assert not pathlib.Path(home, ".claude", "settings.json").exists()
    _with_home(run)


def test_real_home_settings_untouched_by_this_suite():
    # Snapshot the OPERATOR's real ~/.claude/settings.json (if present) using the real HOME,
    # captured before any test in this module could have overridden it, then verify at the end
    # this suite never mutated it.
    real_home = os.path.expanduser("~")
    real_settings = pathlib.Path(real_home, ".claude", "settings.json")
    if not real_settings.exists():
        return  # nothing to protect on this machine — not a failure
    before = real_settings.read_bytes()
    # Run one full install-hooks cycle under a fake HOME to make sure it couldn't have leaked.
    from agents_never_sleep import init_cmd

    def run(home):
        init_cmd.run_install_hooks(["--harness", "claude", "--yes"])
    _with_home(run)
    after = real_settings.read_bytes()
    assert before == after, "install-hooks must never touch the REAL ~/.claude/settings.json"


def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    _run()
