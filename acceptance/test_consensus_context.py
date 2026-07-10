import sys
from agents_never_sleep.consensus_context import estimate_tokens, plan_grounding, GroundingPlan

def test_estimate_tokens_chars_over_4():
    assert estimate_tokens("a" * 400) == 100

def test_small_context_inlines():
    p = plan_grounding("x" * 4000, upload_available=True)   # ~1000 tok < margin
    assert p.mode == "inline"

def test_large_context_uploads_when_available():
    p = plan_grounding("x" * (25000 * 4), upload_available=True)  # ~25000 tok >= 24000 margin
    assert p.mode == "upload"

def test_large_context_degrades_when_upload_unavailable():
    p = plan_grounding("x" * (25000 * 4), upload_available=False)
    assert p.mode == "degraded"          # MUST NOT be "inline"

def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"ok   {name}")
            except AssertionError as e: fails += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    _run()
