#!/usr/bin/env python3
"""Capability-matrix + degradation-reporting test.

Proves the researched per-platform guarantee matrix is correctly encoded and that every DEGRADED
guarantee produces a blind-spot note (so a non-enforced guarantee is never silent). Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import capabilities as C  # noqa: E402
from agents_never_sleep.capabilities import DENY_IRREVERSIBLE, NEVER_ASK, NEVER_STOP, NATIVE, DEGRADED  # noqa: E402


def test_matrix(failures):
    # deny-irreversible is native EVERYWHERE
    for p in C.SUPPORTED:
        if not C.is_native(p, DENY_IRREVERSIBLE):
            failures.append(f"[matrix] {p}: deny-irreversible should be native")
    # never-stop: native except cursor + windsurf
    for p in ("claude", "gemini", "codex", "copilot"):
        if not C.is_native(p, NEVER_STOP):
            failures.append(f"[matrix] {p}: never-stop should be native")
    for p in ("cursor", "windsurf"):
        if C.is_native(p, NEVER_STOP):
            failures.append(f"[matrix] {p}: never-stop should be DEGRADED")
    # never-ASK: native only on claude + copilot
    for p in ("claude", "copilot"):
        if not C.is_native(p, NEVER_ASK):
            failures.append(f"[matrix] {p}: never-ASK should be native")
    for p in ("gemini", "codex", "cursor", "windsurf"):
        if C.is_native(p, NEVER_ASK):
            failures.append(f"[matrix] {p}: never-ASK should be DEGRADED")


def test_detect(failures):
    if C.detect_platform({"UE_PLATFORM": "cursor"}) != "cursor":
        failures.append("[detect] explicit UE_PLATFORM should win")
    if C.detect_platform({"UE_PLATFORM": "nonsense"}) != "claude":
        failures.append("[detect] unknown platform should default to claude")
    if C.detect_platform({}) != "claude":
        failures.append("[detect] no signal should default to claude")


def test_degradation_notes(failures):
    if C.degradation_notes("claude") or C.degradation_notes("copilot"):
        failures.append("[notes] fully-native platforms should have NO degradation notes")
    g = C.degradation_notes("gemini")
    if len(g) != 1 or "never-ASK" not in g[0]:
        failures.append(f"[notes] gemini should have exactly one never-ASK note: {g}")
    for p in ("cursor", "windsurf"):
        notes = C.degradation_notes(p)
        if len(notes) != 2:
            failures.append(f"[notes] {p} should have 2 degradation notes (never-stop + never-ASK): {notes}")
        if not any("never-stop" in n for n in notes) or not any("never-ASK" in n for n in notes):
            failures.append(f"[notes] {p} notes should name never-stop AND never-ASK: {notes}")
    # notes must read as actionable blind spots
    if not all("prose contract" in n for n in C.degradation_notes("windsurf")):
        failures.append("[notes] degradation notes should mention the prose-contract fallback")


def test_status_line(failures):
    s = C.status_line("cursor")
    if "deny-irreversible=native" not in s or "never-stop=soft-enforced" not in s or "never-ASK=soft-enforced" not in s:
        failures.append(f"[status] cursor status line wrong: {s}")
    if "live-verified" not in C.status_line("claude"):
        failures.append("[status] claude should read as live-verified")
    if "NOT live-verified" not in C.status_line("gemini"):
        failures.append("[status] non-Claude should read as NOT live-verified")


def test_verification(failures):
    # Claude's native guarantees are live-verified -> no caveat
    if C.verification_note("claude") is not None:
        failures.append("[verify] claude should have no not-yet-verified caveat")
    # non-Claude with native cells -> a caveat that says NOT live-verified
    vn = C.verification_note("gemini")
    if not vn or "NOT yet live-verified" not in vn:
        failures.append(f"[verify] gemini native guarantees should carry a not-verified caveat: {vn}")
    # report_notes folds BOTH the degraded blind-spots AND the verification caveat
    rn = C.report_notes("gemini")
    if not any("never-ASK is NOT natively enforced" in n for n in rn):
        failures.append(f"[verify] gemini report_notes missing the never-ASK degradation: {rn}")
    if not any("NOT yet live-verified" in n for n in rn):
        failures.append(f"[verify] gemini report_notes missing the verification caveat: {rn}")
    # claude report_notes: no degradation, no caveat
    if C.report_notes("claude"):
        failures.append(f"[verify] claude should have empty report_notes: {C.report_notes('claude')}")


def main() -> int:
    failures = []
    test_matrix(failures)
    test_detect(failures)
    test_degradation_notes(failures)
    test_status_line(failures)
    test_verification(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — capability matrix not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — matrix matches researched contracts; every degraded guarantee emits a "
          "blind-spot note; detection + status line correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
