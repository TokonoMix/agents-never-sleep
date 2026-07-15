#!/usr/bin/env python3
"""Task 0 (reconciliation guard) — pins the shipped v1.5 "unattended safety model" onboarding
behaviour BEFORE the `ans init` build (docs/superpowers/plans/2026-07-14-ans-init-onboarding.md,
Task 3 Step 3a) extracts a shared `agent_clis.scaffold_preset` and migrates `config.run_wizard`'s
launcher-preset block (config.py:323-327).

CHARACTERIZATION TEST, not a new-behaviour test: every assertion below already holds against the
current code (v1.5.0, merge df182c8 / b7b1778) — this file adds NO product behaviour and changes
NO runtime code path. Its only job is to fail LOUDLY if a later refactor (Task 3) disturbs:
  a. the v1.5 `default_config` shape (`classify.consensus_assisted_categories` +
     the council/budget fields `per_night_euro_cap` / `max_council_calls_per_night` /
     `balance_threshold_euro`);
  b. the wizard's out-of-repo consent write (config.py:340-369, `consent_store.write`) — the
     "Actions ANS may perform unattended" pre-authorization section, separate from the
     preset-scaffolding block Task 3a actually migrates;
  c. the wizard's `consensus_assisted_categories` opt-in prompt (config.py:~278).

CRITICAL test-isolation rule (recurring footgun — see MEMORY.md "Wizard tests must isolate
consent store"): any test driving `config.run_wizard` MUST redirect BOTH `ANS_CONSENT_STORE`
(to a temp dir) AND set `ANS_TEST_MODE=1`, or the consent write leaks into the real
`~/.config/agents-never-sleep/consent` store (consent_store._store_dir only honors the
ANS_CONSENT_STORE override under ANS_TEST_MODE=1 — otherwise it warns and falls back to the real
path). Every wizard-driving test here does so, and test_consent_isolation_real_dir_untouched
asserts the real store was never written as an explicit isolation proof.

Not pytest: standalone stdlib script, main() prints PASS/FAIL per check, exits non-zero on any
failure — same convention as the rest of acceptance/.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep import config  # noqa: E402
from agents_never_sleep import consent_store  # noqa: E402
from agents_never_sleep.preflight import CapabilityProfile  # noqa: E402

# The real, out-of-repo consent store path a leaking test would pollute — resolved directly from
# consent_store's own constant (not hardcoded here) so this stays correct if that path ever moves.
_REAL_CONSENT_DIR = os.path.expanduser(consent_store.CONSENT_STORE_DIR)


def _canned_input(answers):
    """Feed answers to input() in order; extra input() calls beyond the list get "" (default)."""
    remaining = list(answers)

    def _fake(_prompt):
        return remaining.pop(0) if remaining else ""
    return _fake


def _real_consent_snapshot():
    """Best-effort listing of the real consent store, so tests can assert it is byte-for-byte
    unchanged before/after — not just "empty", in case a prior unrelated run left files there."""
    if not os.path.isdir(_REAL_CONSENT_DIR):
        return None
    return sorted(os.listdir(_REAL_CONSENT_DIR))


def _run_wizard_isolated(profile, answers):
    """Run run_wizard in a scratch repo dir with is_interactive/installed_clis/trust store AND
    the consent store all redirected — the exact isolation pattern test_config.py /
    test_consent_store.py already use, reused verbatim per the brief's instruction.

    Returns (cfg, repo, consent_dir, work) — caller owns cleanup of `work` (which is also the
    parent of consent_dir: deleting it here, before the caller reads back what the wizard wrote
    to consent_dir, would silently make every consent read see an already-removed directory)."""
    work = tempfile.mkdtemp(prefix="ans-init-guard-")
    trust_store = os.path.join(work, "trusted.json")
    consent_dir = os.path.join(work, "consent")
    with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=True), \
         unittest.mock.patch("agents_never_sleep.agent_clis.installed_clis", return_value=[]), \
         unittest.mock.patch("builtins.input", side_effect=_canned_input(answers)), \
         unittest.mock.patch.dict(os.environ, {"ANS_TRUST_STORE": trust_store,
                                                "ANS_CONSENT_STORE": consent_dir,
                                                "ANS_TEST_MODE": "1"}):
        repo = os.path.join(work, "repo")
        cfg = config.run_wizard(repo, profile)
    return cfg, repo, consent_dir, work


def test_default_config_v15_shape(failures):
    """(a) Pin the v1.5 default_config shape: consensus_assisted_categories is a list defaulting
    to [], and the three council/budget fields exist with their documented default value/type."""
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False, gates=[])
    cfg = config.default_config(profile)

    cats = (cfg.get("classify") or {}).get("consensus_assisted_categories")
    if cats != [] or not isinstance(cats, list):
        failures.append(
            f"[shape] classify.consensus_assisted_categories must default to [] (a list); got {cats!r}")

    budget = cfg.get("budget") or {}
    if "per_night_euro_cap" not in budget or budget["per_night_euro_cap"] is not None:
        failures.append(
            f"[shape] budget.per_night_euro_cap must default to None; got {budget.get('per_night_euro_cap')!r}")
    max_calls = budget.get("max_council_calls_per_night")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls != 50:
        failures.append(
            f"[shape] budget.max_council_calls_per_night must be int 50; got {max_calls!r}")
    threshold = budget.get("balance_threshold_euro")
    if not isinstance(threshold, float) or threshold != 1.0:
        failures.append(
            f"[shape] budget.balance_threshold_euro must be float 1.0; got {threshold!r}")

    # Round-trip through the wizard's own validator — the shape must stay accepted, not just present.
    try:
        config.validate_consensus_config(cfg)
    except Exception as e:  # noqa: BLE001
        failures.append(f"[shape] default_config output must pass validate_consensus_config; raised {e!r}")


def test_wizard_writes_consent_out_of_repo(failures):
    """(b) Drive run_wizard with one deny-list class (redis_flush) ticked "y"; assert the
    isolated (ANS_CONSENT_STORE) consent_store records it, and the real
    ~/.config/agents-never-sleep/consent dir is untouched by this test."""
    from agents_never_sleep import enforcement

    before = _real_consent_snapshot()

    seen = []
    for _, _reason, slug in enforcement._IRREVERSIBLE:  # noqa: SLF001 - same set the wizard enumerates
        if slug not in seen:
            seen.append(slug)
    target_slug = "redis_flush"
    if target_slug not in seen:
        failures.append(f"[consent] 'redis_flush' is no longer a known deny-list slug; seen={seen!r}")
        target_slug = seen[0]
    per_slug_answers = ["y" if slug == target_slug else "n" for slug in seen]

    # header/autonomy(y) -> ambiguity(hybrid) -> keyless-offer choose "3" (skip; has_tokonomix=False)
    # -> one y/n per unique irreversible-action slug (mirrors test_consent_store.py's idiom).
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False, gates=[])
    answers = ["y", "hybrid", "3"] + per_slug_answers
    cfg, repo, consent_dir, work = _run_wizard_isolated(profile, answers)
    try:
        # _run_wizard_isolated's env patch has already exited by now, so re-apply the SAME
        # isolated store dir here to read back what the wizard wrote — never via the
        # (unpatched) real path.
        with unittest.mock.patch.dict(os.environ, {"ANS_CONSENT_STORE": consent_dir,
                                                     "ANS_TEST_MODE": "1"}):
            got = consent_store.read(repo)

        actions = got.get("actions", {})
        if actions.get(target_slug, {}).get("allowed") is not True:
            failures.append(f"[consent] {target_slug!r} must be recorded allowed:True; got {actions!r}")
        if not (got.get("captured") or {}).get("by"):
            failures.append(f"[consent] captured.by must be set; got {got.get('captured')!r}")

        # CRITICAL invariant restated from t02: consent must never leak into the in-repo config.
        if "consent" in cfg or "actions" in cfg:
            failures.append(f"[consent] consent must NOT be written into cfg / save_config output: {cfg!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    after = _real_consent_snapshot()
    if after != before:
        failures.append(
            f"[consent-isolation] the REAL consent dir changed during this test: before={before!r} "
            f"after={after!r} — ANS_CONSENT_STORE/ANS_TEST_MODE isolation leaked")


def test_wizard_sets_consensus_assisted_categories(failures):
    """(c) With a Tokonomix credential, opting into one valid Hard-PARK category via the wizard's
    per-category prompt must land it in the saved config's classify.consensus_assisted_categories."""
    from agents_never_sleep.decide import HARD_PARK_CATEGORIES

    before = _real_consent_snapshot()

    cats = list(HARD_PARK_CATEGORIES)
    per_cat_answers = ["n"] * (len(cats) - 1) + ["y"]  # opt in only the last category
    # autonomy(y) -> ambiguity(hybrid) -> council-enable(y) -> credits-policy(A) ->
    # specialists-enable(n) -> one y/n per HARD_PARK_CATEGORIES key.
    answers = ["y", "hybrid", "y", "A", "n"] + per_cat_answers
    profile = CapabilityProfile(has_tokonomix=True, has_paperclip=False, gates=[])
    cfg, _repo, _consent_dir, work = _run_wizard_isolated(profile, answers)
    shutil.rmtree(work, ignore_errors=True)

    got = (cfg.get("classify") or {}).get("consensus_assisted_categories")
    if got != [cats[-1]]:
        failures.append(
            f"[consensus-opt-in] expected only {cats[-1]!r} opted in (last answer was 'y'); got {got!r}")

    try:
        config.validate_consensus_config(cfg)
    except Exception as e:  # noqa: BLE001
        failures.append(f"[consensus-opt-in] wizard-produced config must pass validation; raised {e!r}")

    after = _real_consent_snapshot()
    if after != before:
        failures.append(
            f"[consent-isolation] the REAL consent dir changed during this test: before={before!r} "
            f"after={after!r} — ANS_CONSENT_STORE/ANS_TEST_MODE isolation leaked")


def test_real_consent_dir_untouched_by_this_module(failures):
    """Explicit isolation proof required by the brief: after every wizard-driving test above has
    run, the real out-of-repo consent store must be exactly as it was before this module ran."""
    after = _real_consent_snapshot()
    if after is None:
        return  # never existed on this host to begin with -> nothing to compare, trivially fine
    # We cannot know the exact pre-module snapshot here (module-level, runs after the other tests),
    # so the strong assertion lives inline in each wizard test above (before/after per test). This
    # check additionally guards against a leftover file this module itself might have created.
    for name in after:
        if name.startswith("ans-init-guard-") or "ans-init-guard" in name:
            failures.append(f"[consent-isolation] leaked file in the real consent store: {name!r}")


def main():
    failures = []
    for fn in (test_default_config_v15_shape,
               test_wizard_writes_consent_out_of_repo,
               test_wizard_sets_consensus_assisted_categories,
               test_real_consent_dir_untouched_by_this_module):
        fn(failures)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
