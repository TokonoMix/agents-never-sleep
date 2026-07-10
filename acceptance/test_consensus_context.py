import subprocess
import sys
from agents_never_sleep.consensus_context import (
    estimate_tokens, plan_grounding, GroundingPlan, plan_to_upload_args, VERBATIM_BYTES
)

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

def test_verbatim_head_bounded_and_remainder_digested():
    ctx = "H" * (VERBATIM_BYTES + 50000)      # exceeds verbatim budget
    p = plan_grounding(ctx, upload_available=True)   # large -> upload
    assert p.mode == "upload"
    verbatim = [f for f in p.files if f["verbatim"]]
    digest = [f for f in p.files if not f["verbatim"]]
    assert sum(len(f["content"]) for f in verbatim) <= VERBATIM_BYTES
    assert digest and sum(len(f["content"]) for f in digest) > 0
    # caller order preserved: concatenation reproduces the original context
    assert "".join(f["content"] for f in p.files) == ctx

def test_plan_to_upload_args_shape():
    p = plan_grounding("x" * (25000 * 4), upload_available=True)
    args = plan_to_upload_args(p)
    assert set(args.keys()) == {"files"}
    assert all(set(f) == {"content", "verbatim"} for f in args["files"])

def test_module_imports_pure_in_clean_subprocess():
    code = ("import agents_never_sleep.consensus_context as m;"
            "assert hasattr(m,'plan_grounding');print('IMPORT_OK')")
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "IMPORT_OK" in r.stdout

def _run():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"ok   {name}")
            except AssertionError as e: fails += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    _run()
