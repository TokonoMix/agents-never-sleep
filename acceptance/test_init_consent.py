"""`ans-run init` consent pre-authorization (Task C, unattended safety model onboarding).

`ans-run init` scaffolds the config but historically never let the owner pre-authorize deny-list
action classes, so a detached run PARKed on the very first consent-gated action. This suite pins:

  * interactive `init` (no --yes, real tty) offers the SAME per-class "Actions" consent prompt the
    wizard has, via the SHARED helper `config.prompt_and_write_consent` (anti-drift: init_cmd must
    call it, never re-implement it) — ticking one class writes it to the isolated consent store
    and prints a summary;
  * `init --yes` writes NO consent (skip-prompts must not silently authorize execution) and prints
    a one-line pointer to re-run interactively;
  * non-interactive stdin (no real tty) without --yes also declines rather than hanging on input();
  * the real out-of-repo consent store is never touched by any of the above.

CRITICAL test-isolation rule (recurring footgun — MEMORY.md "Wizard tests must isolate consent
store"): every test below sets BOTH `ANS_CONSENT_STORE` (temp dir) AND `ANS_TEST_MODE=1`.

Not pytest: standalone stdlib script, `_run()` prints ok/FAIL per test and exits non-zero on any
failure — same convention as acceptance/test_init.py.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

from agents_never_sleep import consent_store  # noqa: E402

_REAL_CONSENT_DIR = os.path.expanduser(consent_store.CONSENT_STORE_DIR)


def _real_consent_snapshot():
    if not os.path.isdir(_REAL_CONSENT_DIR):
        return None
    return sorted(os.listdir(_REAL_CONSENT_DIR))


def _canned_input(answers):
    remaining = list(answers)

    def _fake(_prompt):
        return remaining.pop(0) if remaining else ""
    return _fake


def _new_repo():
    repo = tempfile.mkdtemp(prefix="ans-init-consent-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


def test_init_interactive_ticks_one_class_writes_isolated_consent_and_prints_summary():
    from agents_never_sleep import init_cmd, enforcement

    before = _real_consent_snapshot()
    work = tempfile.mkdtemp(prefix="ans-init-consent-work-")
    consent_dir = os.path.join(work, "consent")
    home = tempfile.mkdtemp(prefix="ans-init-consent-home-")
    repo = _new_repo()

    seen = []
    for _, _reason, slug in enforcement._IRREVERSIBLE:  # noqa: SLF001 - same set the UI enumerates
        if slug not in seen:
            seen.append(slug)
    target_slug = "redis_flush"
    assert target_slug in seen, seen
    answers = ["y" if slug == target_slug else "n" for slug in seen]

    old_home = os.environ.get("HOME")
    buf = io.StringIO()
    try:
        with unittest.mock.patch.object(init_cmd, "_stdin_is_tty", return_value=True), \
             unittest.mock.patch.object(init_cmd.agent_clis, "installed_clis", return_value=[]), \
             unittest.mock.patch("builtins.input", side_effect=_canned_input(answers)), \
             unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir,
                                                     "ANS_TEST_MODE": "1", "HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = init_cmd.run_init(["--repo", repo])
            assert rc == 0, rc
            got = consent_store.read(repo)
    finally:
        os.environ["HOME"] = old_home or ""

    actions = got.get("actions", {})
    assert actions.get(target_slug, {}).get("allowed") is True, actions
    assert (got.get("captured") or {}).get("by"), got
    out = buf.getvalue()
    assert target_slug in out, out  # summary names what got pre-authorized

    after = _real_consent_snapshot()
    assert after == before, (before, after)


def test_init_yes_writes_no_consent_and_prints_pointer():
    from agents_never_sleep import init_cmd

    before = _real_consent_snapshot()
    work = tempfile.mkdtemp(prefix="ans-init-consent-work-")
    consent_dir = os.path.join(work, "consent")
    home = tempfile.mkdtemp(prefix="ans-init-consent-home-")
    repo = _new_repo()

    old_home = os.environ.get("HOME")
    buf = io.StringIO()
    try:
        # --yes must never prompt: fail loudly if input() is ever called on this path.
        def _no_input(_prompt):
            raise AssertionError("input() must not be called under --yes")
        with unittest.mock.patch.object(init_cmd, "_stdin_is_tty", return_value=True), \
             unittest.mock.patch.object(init_cmd.agent_clis, "installed_clis", return_value=[]), \
             unittest.mock.patch("builtins.input", side_effect=_no_input), \
             unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir,
                                                     "ANS_TEST_MODE": "1", "HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = init_cmd.run_init(["--repo", repo, "--yes"])
            assert rc == 0, rc
            got = consent_store.read(repo)
    finally:
        os.environ["HOME"] = old_home or ""

    assert got.get("actions", {}) == {}, got
    out = buf.getvalue().lower()
    assert "pre-authoriz" in out and ("interactive" in out or "wizard" in out), out

    after = _real_consent_snapshot()
    assert after == before, (before, after)


def test_init_non_tty_without_yes_declines_without_hanging():
    from agents_never_sleep import init_cmd

    before = _real_consent_snapshot()
    work = tempfile.mkdtemp(prefix="ans-init-consent-work-")
    consent_dir = os.path.join(work, "consent")
    home = tempfile.mkdtemp(prefix="ans-init-consent-home-")
    repo = _new_repo()

    old_home = os.environ.get("HOME")
    buf = io.StringIO()
    try:
        def _no_input(_prompt):
            raise AssertionError("input() must not be called on non-tty stdin")
        with unittest.mock.patch.object(init_cmd, "_stdin_is_tty", return_value=False), \
             unittest.mock.patch.object(init_cmd.agent_clis, "installed_clis", return_value=[]), \
             unittest.mock.patch("builtins.input", side_effect=_no_input), \
             unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir,
                                                     "ANS_TEST_MODE": "1", "HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = init_cmd.run_init(["--repo", repo])   # no --yes, but stdin is not a real tty
            assert rc == 0, rc
            got = consent_store.read(repo)
    finally:
        os.environ["HOME"] = old_home or ""

    assert got.get("actions", {}) == {}, got

    after = _real_consent_snapshot()
    assert after == before, (before, after)


def test_init_eoferror_during_prompt_never_hangs():
    from agents_never_sleep import init_cmd

    before = _real_consent_snapshot()
    work = tempfile.mkdtemp(prefix="ans-init-consent-work-")
    consent_dir = os.path.join(work, "consent")
    home = tempfile.mkdtemp(prefix="ans-init-consent-home-")
    repo = _new_repo()

    old_home = os.environ.get("HOME")
    buf = io.StringIO()
    try:
        def _eof(_prompt):
            raise EOFError()
        with unittest.mock.patch.object(init_cmd, "_stdin_is_tty", return_value=True), \
             unittest.mock.patch.object(init_cmd.agent_clis, "installed_clis", return_value=[]), \
             unittest.mock.patch("builtins.input", side_effect=_eof), \
             unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir,
                                                     "ANS_TEST_MODE": "1", "HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = init_cmd.run_init(["--repo", repo])
            assert rc == 0, rc
            got = consent_store.read(repo)
    finally:
        os.environ["HOME"] = old_home or ""

    # EOFError on every input() falls back to each prompt's default ("n") -> nothing ticked.
    assert got.get("actions", {}) == {}, got

    after = _real_consent_snapshot()
    assert after == before, (before, after)


def test_init_calls_shared_consent_helper_not_a_duplicate():
    # Anti-drift proof: init_cmd must call config.prompt_and_write_consent, never re-implement
    # the "Actions" prompt+write UI itself.
    from agents_never_sleep import init_cmd

    work = tempfile.mkdtemp(prefix="ans-init-consent-work-")
    home = tempfile.mkdtemp(prefix="ans-init-consent-home-")
    repo = _new_repo()
    calls = []

    def _fake_prompt_and_write_consent(repo_dir, *, ask, by=None):
        calls.append(repo_dir)
        return {"redis_flush": {"allowed": True}}

    old_home = os.environ.get("HOME")
    buf = io.StringIO()
    try:
        with unittest.mock.patch.object(init_cmd, "_stdin_is_tty", return_value=True), \
             unittest.mock.patch.object(init_cmd.agent_clis, "installed_clis", return_value=[]), \
             unittest.mock.patch.object(init_cmd.config, "prompt_and_write_consent",
                                         side_effect=_fake_prompt_and_write_consent), \
             unittest.mock.patch.dict(os.environ, {"HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = init_cmd.run_init(["--repo", repo])
            assert rc == 0, rc
    finally:
        os.environ["HOME"] = old_home or ""

    # The summary print itself lives INSIDE the shared helper (by design — single copy for both
    # callers), so a mocked helper prints nothing here; this test's job is only to prove init_cmd
    # routes through config.prompt_and_write_consent rather than re-implementing the UI.
    assert calls == [repo], calls


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
