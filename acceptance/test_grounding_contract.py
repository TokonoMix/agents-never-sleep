"""Doc-sync contract test for SKILL.md's consensus-grounding guidance.

HONEST SCOPE: this verifies the *document still encodes* the load-bearing invariants of the
grounding execution path — NOT that an agent obeys them at runtime. Nothing hermetic can prove
runtime obedience: execution is the agent interpreting this prose and calling the MCP tools itself
(the module "only DECIDES"; see consensus_context.py's docstring). What this DOES catch is a future
SKILL.md edit that accidentally drops a load-bearing rule.

Behavior (3) — a `grounding_not_applied` / `x_council.grounding.applied: false` response — is the
ONE genuinely SILENT failure mode (HTTP 200 with a plausible verdict that was never grounded), so it
is the highest-value invariant to lock. (1) wrong model and (2)/(4) over-budget are loud (HTTP 400 /
413). This follows the repo's Source-Anchored Publishing practice: pin the surface, fail on drift.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "SKILL.md")


def _grounding_section() -> str:
    text = open(SKILL, encoding="utf-8").read()
    # the subsection begins at its ### header and runs to the next top-level (## ) heading
    m = re.search(r"### Grounding a consensus call.*?(?=\n## )", text, re.S)
    assert m, "SKILL.md no longer has a '### Grounding a consensus call' section"
    return m.group(0)


def test_names_the_literal_consensus_model():
    # behavior (1): a grounded call must target the literal multi-model consensus model.
    assert "tokonomix-consensus" in _grounding_section(), \
        "grounding guidance must name the literal consensus model 'tokonomix-consensus'"


def test_silent_grounding_drop_invariant_present():
    # behavior (3), the ONE silent failure: a 200 response that was never actually grounded.
    sec = _grounding_section().lower()
    assert "grounding_not_applied" in sec, "must reference the grounding_not_applied warning"
    assert "applied: false" in sec or "applied:false" in sec, \
        "must reference x_council.grounding.applied: false"
    assert "degrad" in sec, "must say to degrade when grounding was not applied"
    assert "keep_parked" in sec or "keep parked" in sec, \
        "unapplied grounding must route to KEEP_PARKED, never a valid grounded review"


def test_fail_closed_on_both_network_steps():
    # behavior (4): degrade on upload AND ask errors; never inline an over-budget context.
    sec = _grounding_section().lower()
    assert "both" in sec and "network step" in sec, "must fail closed on BOTH network steps"
    assert "over-budget" in sec or "over budget" in sec, \
        "must forbid inlining an over-budget context on error"


def test_413_needs_upload_recovery_is_executable():
    # behavior (2): the documented 413 recovery must be the executable route_margin=0 re-plan,
    # not plan_to_upload_args on an inline plan (which would upload nothing).
    sec = _grounding_section()
    assert "413" in sec or "needs_upload" in sec, "must handle the needs_upload/413 signal"
    assert "route_margin=0" in sec, \
        "413 recovery must document the executable route_margin=0 re-plan"


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
