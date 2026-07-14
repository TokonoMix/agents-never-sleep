#!/usr/bin/env python3
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agents_never_sleep import config  # noqa: E402
from agents_never_sleep.preflight import CapabilityProfile  # noqa: E402


class _Profile:
    has_tokonomix = False
    # default_config() reads profile.gates directly (no getattr fallback) to build the
    # "gates" section; every other attr it reads defensively via getattr(..., False).
    gates = []


def _canned_input(answers):
    """Feed answers to input() in order; extra input() calls beyond the list get "" (default)."""
    remaining = list(answers)

    def _fake(_prompt):
        return remaining.pop(0) if remaining else ""
    return _fake


def _run_wizard_isolated(profile, answers):
    """Run run_wizard in a scratch repo dir with is_interactive/installed_clis/trust store
    all redirected so the test never touches the real home dir or a real tty."""
    work = tempfile.mkdtemp(prefix="ans-wizard-test-")
    trust_store = os.path.join(work, "trusted.json")
    consent_store = os.path.join(work, "consent")
    try:
        # ANS_CONSENT_STORE too: the wizard's new pre-authorize-actions section (t02) writes the
        # out-of-repo consent store when any slug is ticked — extra "y" answers here spill into
        # those prompts, so without this redirect the test would leak files into the real
        # ~/.config/agents-never-sleep/consent tree.
        with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=True), \
             unittest.mock.patch("agents_never_sleep.agent_clis.installed_clis", return_value=[]), \
             unittest.mock.patch("builtins.input", side_effect=_canned_input(answers)), \
             unittest.mock.patch.dict(os.environ, {"ANS_TRUST_STORE": trust_store,
                                                    "ANS_CONSENT_STORE": consent_store,
                                                    "ANS_TEST_MODE": "1"}):
            return config.run_wizard(os.path.join(work, "repo"), profile)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_default_has_empty_consensus_list(failures):
    c = config.default_config(_Profile())
    val = (c.get("classify") or {}).get("consensus_assisted_categories")
    if val != []:
        failures.append(f"default consensus_assisted_categories must be []; got {val!r}")


def test_validate_accepts_known_categories(failures):
    try:
        config.validate_consensus_config(
            {"classify": {"consensus_assisted_categories": ["db_schema_or_migration"]}})
    except Exception as e:  # noqa: BLE001
        failures.append(f"known category must validate; raised {e!r}")


def test_validate_rejects_typo(failures):
    try:
        config.validate_consensus_config(
            {"classify": {"consensus_assisted_categories": ["db_schema"]}})
        failures.append("a misspelled category must be a hard error, not silently ignored")
    except ValueError:
        pass


def test_validate_rejects_requirement_meaning(failures):
    try:
        config.validate_consensus_config(
            {"classify": {"consensus_assisted_categories": ["requirement_meaning"]}})
        failures.append("requirement_meaning must be rejected (always eligible by definition)")
    except ValueError:
        pass


def test_validate_rejects_non_list_including_falsy(failures):
    for bad in ({}, "", 0, False, "db_schema_or_migration"):
        try:
            config.validate_consensus_config(
                {"classify": {"consensus_assisted_categories": bad}})
            failures.append(f"non-list value {bad!r} must raise ValueError, not silently pass")
        except ValueError:
            pass
    # A genuinely-absent key must still pass (no false positive on the common case).
    for ok in ({}, {"classify": {}}, {"classify": {"consensus_assisted_categories": []}}):
        try:
            config.validate_consensus_config(ok)
        except Exception as e:  # noqa: BLE001
            failures.append(f"absent/empty config {ok!r} must NOT raise; raised {e!r}")


def test_wizard_no_tokonomix_skips_consensus_questions(failures):
    """Plan 2 §3: with no Tokonomix credential, the wizard must not ask the specialist or
    per-category consensus-opt-in questions at all, and consensus_assisted_categories stays []."""
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)
    # Any "y" answers here would be wrong picks IF the (gated) questions were asked anyway —
    # if the gate is missing, this would wrongly opt every category in.
    cfg = _run_wizard_isolated(profile, ["y", "hybrid", "y", "y", "y", "y", "y", "y", "y"])
    got = (cfg.get("classify") or {}).get("consensus_assisted_categories")
    if got != []:
        failures.append(f"[no-tokonomix] consensus_assisted_categories must stay []; got {got!r}")


def test_wizard_tokonomix_specialist_and_category_opt_in(failures):
    """With a Tokonomix credential, the wizard asks an explicit specialist-enable question and
    one opt-in question per HARD_PARK_CATEGORIES key (default no), and only opted keys land in
    consensus_assisted_categories — which must stay a validate_consensus_config-accepted set."""
    from agents_never_sleep.decide import HARD_PARK_CATEGORIES
    profile = CapabilityProfile(has_tokonomix=True, has_paperclip=False)
    # Order: autonomy(y), ambiguity(hybrid), council-enable(y), credits-policy(A),
    # specialists-enable(n), then one y/n per HARD_PARK_CATEGORIES key (opt in only the last one).
    cats = list(HARD_PARK_CATEGORIES)
    per_cat_answers = ["n"] * (len(cats) - 1) + ["y"]
    answers = ["y", "hybrid", "y", "A", "n"] + per_cat_answers
    cfg = _run_wizard_isolated(profile, answers)

    if cfg.get("specialists", {}).get("enabled") is not False:
        failures.append(
            f"[specialist] answering 'n' must set specialists.enabled False; "
            f"got {cfg.get('specialists', {}).get('enabled')!r}")

    got = (cfg.get("classify") or {}).get("consensus_assisted_categories")
    if got != [cats[-1]]:
        failures.append(
            f"[per-category] expected only {cats[-1]!r} opted in (last answer was 'y'); got {got!r}")

    try:
        config.validate_consensus_config(cfg)
    except Exception as e:  # noqa: BLE001
        failures.append(f"[per-category] wizard-produced config must pass validation; raised {e!r}")


def test_wizard_unattended_keeps_conservative_default(failures):
    """Non-interactive/unattended path (no config yet) must never prompt and must keep the
    empty consensus opt-in default — an unattended first-run is never silently opted in."""
    profile = CapabilityProfile(has_tokonomix=True, has_paperclip=True)
    work = tempfile.mkdtemp(prefix="ans-wizard-unattended-")
    input_called = []
    try:
        with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=False), \
             unittest.mock.patch("builtins.input",
                                  side_effect=lambda p: input_called.append(p) or "y"):
            cfg = config.run_wizard(os.path.join(work, "repo"), profile)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if input_called:
        failures.append(f"[unattended] must never call input(); calls were: {input_called!r}")
    got = (cfg.get("classify") or {}).get("consensus_assisted_categories")
    if got != []:
        failures.append(f"[unattended] consensus_assisted_categories must stay []; got {got!r}")


def test_pending_onboard_default_false(failures):
    c = config.default_config(_Profile())
    val = ((c.get("integrations") or {}).get("tokonomix") or {}).get("pending_onboard")
    if val is not False:
        failures.append(f"integrations.tokonomix.pending_onboard must default to False; got {val!r}")


def test_enable_tokonomix_review_flips_all_three(failures):
    cfg = {"integrations": {"tokonomix": {"enabled": False}},
           "council": {"enabled": False}, "specialists": {"enabled": False}}
    config.enable_tokonomix_review(cfg)
    if not (cfg["integrations"]["tokonomix"]["enabled"] and cfg["council"]["enabled"]
            and cfg["specialists"]["enabled"]):
        failures.append(f"enable_tokonomix_review must flip all three True; got {cfg!r}")


def test_ensure_config_reprobe_enables_review_and_rerecords_trust(failures):
    import agents_never_sleep.config as C, agents_never_sleep.onboarding as OB
    from agents_never_sleep import trust
    orig_cred = OB.credential_present
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["ANS_TEST_MODE"] = "1"; os.environ["ANS_TRUST_STORE"] = os.path.join(d, "t.json")
            # seed a keyless config with the pending marker + trust recorded on it
            cfg = C.default_config(_Profile())
            cfg["integrations"]["tokonomix"]["pending_onboard"] = True
            C.save_config(d, cfg)
            trust.record_trust(d, C.config_path(d))
            # now a credential appears
            OB.credential_present = lambda: True
            out = C.ensure_config(d, _Profile())
            if not out["council"]["enabled"]:
                failures.append("re-probe must enable review once the credential is present")
            if out["integrations"]["tokonomix"].get("pending_onboard"):
                failures.append("re-probe must clear the pending_onboard marker")
            # trust must match the NEW bytes (else a detached run bounces untrusted)
            if not trust.is_trusted(d, C.config_path(d)):
                failures.append("re-probe must re-record trust on the flipped config")
    finally:
        OB.credential_present = orig_cred
        os.environ.pop("ANS_TEST_MODE", None); os.environ.pop("ANS_TRUST_STORE", None)


def test_keyless_wizard_offers_three_way_and_skip_leaves_review_off(failures):
    # Keyless first-run (has_tokonomix False), Skip (option 3, the default). Mirrors the file's
    # _run_wizard_isolated idiom; credential_present pinned False so the offer path is deterministic.
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)
    buf = io.StringIO()
    with unittest.mock.patch("agents_never_sleep.onboarding.credential_present", return_value=False), \
         contextlib.redirect_stdout(buf):
        cfg = _run_wizard_isolated(profile, ["y", "hybrid", "3"])
    blob = buf.getvalue()
    if "no Tokonomix key" not in blob and "No Tokonomix key" not in blob:
        failures.append("keyless wizard must surface the no-key 3-way offer")
    if cfg["council"]["enabled"] or cfg["integrations"]["tokonomix"]["enabled"]:
        failures.append("Skip must leave review OFF")
    if cfg["integrations"]["tokonomix"].get("pending_onboard"):
        failures.append("Skip must NOT set pending_onboard")


def test_keyless_wizard_create_sets_pending_marker_and_no_mcp(failures):
    # Keyless first-run, Create (option 1): marker set, review NOT enabled this session, zero MCP.
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)
    with unittest.mock.patch("agents_never_sleep.onboarding.credential_present", return_value=False):
        cfg = _run_wizard_isolated(profile, ["y", "hybrid", "1"])
    if not cfg["integrations"]["tokonomix"].get("pending_onboard"):
        failures.append("Create must set pending_onboard=True")
    if cfg["council"]["enabled"]:
        failures.append("Create must NOT enable review this session (activates after reload)")


def test_keyless_wizard_paste_with_key_present_enables_review_now(failures):
    # Keyless first-run, Paste (option 2) with a key that IS present after the prompt: review flips
    # ON immediately (all three consumers) and no pending marker is left. Covers the choice-2 branch.
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)
    with unittest.mock.patch("agents_never_sleep.onboarding.credential_present", return_value=True):
        cfg = _run_wizard_isolated(profile, ["y", "hybrid", "2"])
    tok = cfg["integrations"]["tokonomix"]
    if not (tok["enabled"] and cfg["council"]["enabled"] and cfg["specialists"]["enabled"]):
        failures.append("Paste with key present must enable all three review consumers now")
    if tok.get("pending_onboard"):
        failures.append("Paste (review enabled now) must NOT leave a pending_onboard marker")


def test_unattended_wizard_never_offers(failures):
    # CLAUDE_UNATTENDED path: run_wizard bails before the offer; no marker, no prompt.
    profile = CapabilityProfile(has_tokonomix=False, has_paperclip=False)
    with unittest.mock.patch("agents_never_sleep.config.is_interactive", return_value=False), \
         tempfile.TemporaryDirectory() as d:
        cfg = config.run_wizard(os.path.join(d, "repo"), profile)
    if cfg["integrations"]["tokonomix"].get("pending_onboard"):
        failures.append("unattended run must never reach the offer / set pending_onboard")


def main():
    failures = []
    for fn in (test_default_has_empty_consensus_list, test_validate_accepts_known_categories,
               test_validate_rejects_typo, test_validate_rejects_requirement_meaning,
               test_validate_rejects_non_list_including_falsy,
               test_wizard_no_tokonomix_skips_consensus_questions,
               test_wizard_tokonomix_specialist_and_category_opt_in,
               test_wizard_unattended_keeps_conservative_default,
               test_pending_onboard_default_false,
               test_enable_tokonomix_review_flips_all_three,
               test_ensure_config_reprobe_enables_review_and_rerecords_trust,
               test_keyless_wizard_offers_three_way_and_skip_leaves_review_off,
               test_keyless_wizard_create_sets_pending_marker_and_no_mcp,
               test_keyless_wizard_paste_with_key_present_enables_review_now,
               test_unattended_wizard_never_offers):
        fn(failures)
    if failures:
        print("RESULT: ❌")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
