#!/usr/bin/env python3
"""Consent-store ACCEPTANCE test (t02, unattended safety model Part B).

Proves the out-of-repo consent store that pre-authorizes deny-list action classes for a run:
  * round-trips actions + captured metadata,
  * lives OUTSIDE the repo (store dir never derives from repo_dir content — same TOFU
    threat model as trust.py: the repo must not be able to relocate its own consent gate),
  * fails safe on a missing/corrupt store (never raises, never wedges a run),
  * ignores an env override of the store dir unless ANS_TEST_MODE=1 (loud-warn-and-ignore,
    mirroring trust.py's ANS_TRUST_STORE guard exactly),
  * the config wizard writes consent ONLY when interactive and ONLY to the out-of-repo store
    (never into cfg / save_config — the unattended agent must not be able to author its own
    consent).
Exit 0 = GREEN. NOT pytest — a plain main()/failures list, like the rest of acceptance/.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

# ANS_TEST_MODE=1 for the whole module: every consent_store / trust store override below must
# be honored, and the suite must NEVER touch the real ~/.config/agents-never-sleep tree.
os.environ["ANS_TEST_MODE"] = "1"

from agents_never_sleep import consent_store  # noqa: E402
from agents_never_sleep import config  # noqa: E402
from agents_never_sleep.preflight import CapabilityProfile  # noqa: E402


def _canned_input(answers):
    """Feed answers to input() in order; extra input() calls beyond the list get "" (default)."""
    remaining = list(answers)

    def _fake(_prompt):
        return remaining.pop(0) if remaining else ""
    return _fake


def test_round_trip(failures):
    work = tempfile.mkdtemp(prefix="ans-consent-test-")
    store = os.path.join(work, "store")
    repo = os.path.join(work, "repo")
    os.makedirs(repo, exist_ok=True)
    try:
        with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": store}):
            consent_store.write(repo, actions={"send_email": {"allowed": True}}, by="tester")
            got = consent_store.read(repo)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if got.get("actions") != {"send_email": {"allowed": True}}:
        failures.append(f"[round-trip] actions mismatch: {got.get('actions')!r}")
    captured = got.get("captured") or {}
    if captured.get("by") != "tester":
        failures.append(f"[round-trip] captured.by mismatch: {captured!r}")
    if not captured.get("at_utc"):
        failures.append(f"[round-trip] captured.at_utc must be present: {captured!r}")


def test_store_isolation(failures):
    work = tempfile.mkdtemp(prefix="ans-consent-test-")
    store = os.path.join(work, "store")
    repo = os.path.join(work, "repo", "my-project")
    os.makedirs(repo, exist_ok=True)
    try:
        with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": store}):
            consent_store.write(repo, actions={"force_push": {"allowed": True}}, by="tester")
            # The written file must live under the override store dir...
            found = []
            for root, _dirs, files in os.walk(store):
                for f in files:
                    found.append(os.path.join(root, f))
            if len(found) != 1:
                failures.append(f"[isolation] expected exactly one file under the store dir; found {found!r}")
            elif repo in found[0]:
                failures.append(f"[isolation] consent file path must not contain repo_dir: {found[0]!r}")
            # ...and nothing must have been created inside repo_dir itself.
            repo_contents = os.listdir(repo)
            if repo_contents:
                failures.append(f"[isolation] repo_dir must stay untouched; found {repo_contents!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_fail_safe(failures):
    work = tempfile.mkdtemp(prefix="ans-consent-test-")
    store = os.path.join(work, "store")
    try:
        with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": store}):
            # Missing repo -> {}.
            missing = consent_store.read(os.path.join(work, "never-written-repo"))
            if missing != {}:
                failures.append(f"[fail-safe] missing store must read as {{}}; got {missing!r}")

            # Corrupt JSON at the resolved store path -> {}.
            repo = os.path.join(work, "repo2")
            os.makedirs(repo, exist_ok=True)
            os.makedirs(store, exist_ok=True)
            repo_id = consent_store._repo_id(repo)  # noqa: SLF001 - white-box path construction
            corrupt_path = os.path.join(store, f"{repo_id}.json")
            with open(corrupt_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            corrupt = consent_store.read(repo)
            if corrupt != {}:
                failures.append(f"[fail-safe] corrupt JSON must read as {{}}; got {corrupt!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_override_ignored_without_test_mode(failures):
    work = tempfile.mkdtemp(prefix="ans-consent-test-")
    store = os.path.join(work, "store")
    try:
        env = dict(os.environ)
        env.pop("ANS_TEST_MODE", None)
        env["ANS_CONSENT_STORE"] = store
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            resolved = consent_store._store_dir()  # noqa: SLF001 - checking resolution, no write
        if resolved == store:
            failures.append(
                "[override-guard] ANS_CONSENT_STORE must be IGNORED without ANS_TEST_MODE=1; "
                f"resolved to the override anyway: {resolved!r}")
        if not resolved.endswith(os.path.join(".config", "agents-never-sleep", "consent")):
            failures.append(f"[override-guard] expected the real ~/.config path; got {resolved!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_wizard_noninteractive_writes_nothing(failures):
    work = tempfile.mkdtemp(prefix="ans-consent-wizard-")
    store = os.path.join(work, "store")
    profile = CapabilityProfile(has_tokonomix=True, has_paperclip=True)
    try:
        with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=False), \
             unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": store}):
            config.run_wizard(os.path.join(work, "repo"), profile)
            got = consent_store.read(os.path.join(work, "repo"))
        if got != {}:
            failures.append(f"[wizard-unattended] must never write consent; got {got!r}")
        if os.path.isdir(store) and os.listdir(store):
            failures.append(f"[wizard-unattended] must not create anything under the store dir: "
                             f"{os.listdir(store)!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_wizard_interactive_writes_one_slug(failures):
    from agents_never_sleep import enforcement

    work = tempfile.mkdtemp(prefix="ans-consent-wizard-")
    consent_dir = os.path.join(work, "consent-store")
    trust_dir = os.path.join(work, "trusted.json")
    repo = os.path.join(work, "repo")
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)

    seen = []
    for _, _reason, slug in enforcement._IRREVERSIBLE:  # noqa: SLF001 - deriving the same set the wizard must
        if slug not in seen:
            seen.append(slug)
    target_slug = "redis_flush"
    if target_slug not in seen:
        target_slug = seen[0]
    per_slug_answers = ["y" if slug == target_slug else "n" for slug in seen]

    # header/autonomy(y) -> ambiguity(hybrid) -> keyless-offer choose "3" (skip, has_tokonomix=False)
    # -> one y/n per unique irreversible-action slug.
    answers = ["y", "hybrid", "3"] + per_slug_answers

    try:
        with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=True), \
             unittest.mock.patch("agents_never_sleep.agent_clis.installed_clis", return_value=[]), \
             unittest.mock.patch("agents_never_sleep.onboarding.credential_present", return_value=False), \
             unittest.mock.patch("builtins.input", side_effect=_canned_input(answers)), \
             unittest.mock.patch.dict(os.environ, {"ANS_TRUST_STORE": trust_dir,
                                                    "ANS_CONSENT_STORE": consent_dir}), \
             contextlib.redirect_stdout(io.StringIO()):
            config.run_wizard(repo, profile)
            got = consent_store.read(repo)

        actions = got.get("actions", {})
        if actions.get(target_slug, {}).get("allowed") is not True:
            failures.append(f"[wizard-interactive] {target_slug!r} must be allowed:True; got {actions!r}")
        others_allowed = {s: v for s, v in actions.items() if s != target_slug and v.get("allowed")}
        if others_allowed:
            failures.append(f"[wizard-interactive] only {target_slug!r} was ticked 'y'; "
                             f"also allowed: {others_allowed!r}")
        if not (got.get("captured") or {}).get("by"):
            failures.append(f"[wizard-interactive] captured.by must be set; got {got.get('captured')!r}")

        # CRITICAL invariant: consent must never leak into cfg / the in-repo config file.
        cfg_path = config.config_path(repo)
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                saved_cfg = json.load(fh)
            if "consent" in saved_cfg or "actions" in saved_cfg:
                failures.append("[wizard-interactive] consent must NOT be written into the "
                                 f"in-repo config: {saved_cfg!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_wizard_interactive_all_declined_writes_no_file(failures):
    """Every slug answered 'n' -> ticked is empty -> consent_store.write must be SKIPPED
    entirely (no empty-actions file created), not just written with an empty actions dict."""
    from agents_never_sleep import enforcement

    work = tempfile.mkdtemp(prefix="ans-consent-wizard-")
    consent_dir = os.path.join(work, "consent-store")
    trust_dir = os.path.join(work, "trusted.json")
    repo = os.path.join(work, "repo")
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)

    seen = []
    for _, _reason, slug in enforcement._IRREVERSIBLE:  # noqa: SLF001
        if slug not in seen:
            seen.append(slug)
    answers = ["y", "hybrid", "3"] + ["n"] * len(seen)

    try:
        with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=True), \
             unittest.mock.patch("agents_never_sleep.agent_clis.installed_clis", return_value=[]), \
             unittest.mock.patch("agents_never_sleep.onboarding.credential_present", return_value=False), \
             unittest.mock.patch("builtins.input", side_effect=_canned_input(answers)), \
             unittest.mock.patch.dict(os.environ, {"ANS_TRUST_STORE": trust_dir,
                                                    "ANS_CONSENT_STORE": consent_dir}), \
             contextlib.redirect_stdout(io.StringIO()):
            config.run_wizard(repo, profile)
            got = consent_store.read(repo)

        if got != {}:
            failures.append(f"[wizard-all-declined] read() must be {{}}; got {got!r}")
        if os.path.isdir(consent_dir) and os.listdir(consent_dir):
            failures.append("[wizard-all-declined] no file should be created when nothing was "
                             f"ticked; found {os.listdir(consent_dir)!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    failures = []
    test_round_trip(failures)
    test_store_isolation(failures)
    test_fail_safe(failures)
    test_override_ignored_without_test_mode(failures)
    test_wizard_noninteractive_writes_nothing(failures)
    test_wizard_interactive_writes_one_slug(failures)
    test_wizard_interactive_all_declined_writes_no_file(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — consent store not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — consent store round-trips, stays out-of-repo, fails safe, "
          "ignores an untrusted override, and the wizard writes it ONLY interactively and ONLY "
          "to the out-of-repo store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
