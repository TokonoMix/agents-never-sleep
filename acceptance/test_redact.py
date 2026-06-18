#!/usr/bin/env python3
"""Secret-redaction test — proves credentials are scrubbed at the WRITE BOUNDARIES, and (just as
important) that the codebase's own legitimate vocabulary is left byte-identical.

Two discriminating tests carry the weight:
  1. THE OVER-MATCH GUARD: a synthetic report full of the words a naive scrubber keys on ("token",
     "security", "council:pass", "NEEDS-DAYLIGHT-REVIEW"), a ticket id and a git SHA, plus ONE
     planted real secret, must come back byte-identical except the planted secret. This is what
     separates a correct shape-anchored redactor from one that shreds reports (and breaks the suite).
  2. THE OUTWARD BOUNDARY: a secret planted in an outcome field must be gone from the comment body
     `add_comment` would POST to Paperclip (irreversible, shared infra) AND from the artifact file
     `_save_artifact` writes — not merely from a direct redact() call. The leak is only closed if it
     is closed at the destination.

Exit 0 = GREEN.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from harness import redact as R  # noqa: E402
from harness.gates import GateRunner  # noqa: E402
from harness.orchestrator import Orchestrator  # noqa: E402
from harness.sources.paperclip import PaperclipClient  # noqa: E402
from harness.state import OutcomeStore  # noqa: E402


def test_over_match_guard(failures):
    """The most important test: legitimate output must pass through untouched."""
    benign = (
        "# Morning report — unattended run\n"
        "## done (2)\n"
        "- **ticket-04-auth** — gates green; council:pass\n"
        "  - coverage: deterministic-gates · council:pass · specialists:security,architect\n"
        "  - flag: NEEDS-DAYLIGHT-REVIEW\n"
        "- note: the security token rotation work is tracked separately\n"
        "  - commit abandoned at 3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c (a git SHA, not a secret)\n"
        "  - password policy updated; authorization model unchanged\n"
    )
    if R.redact(benign) != benign:
        failures.append("[over-match] benign report was altered — a pattern is keyword-anchored or "
                        "matched a git SHA:\n" + _diff(benign, R.redact(benign)))

    # Same text + ONE planted secret -> byte-identical except that secret.
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    planted = benign + f"  - leaked: Authorization: Bearer {secret}\n"
    out = R.redact(planted)
    if secret in out:
        failures.append("[over-match] planted secret survived redaction")
    if not out.startswith(benign):
        failures.append("[over-match] redaction disturbed the benign prefix around the planted secret")


def test_patterns(failures):
    cases = {
        "Bearer sk-not": "Bearer abcdef0123456789ABCDEF",
        "pcp": "pcp_board_deadbeefcafef00d0123456789abcdef01234567",
        "jwt": "eyJhbGciOiJIUzI1Niable.eyJzdWIiOiIxMjM0NTonone.SflKxwRJSMeKKF2QT4f",
        "aws": "AKIAIOSFODNN7EXAMPLE",
        "github": "ghp_0123456789ABCDEFGHIJ0123456789ABCDEF",
        "openai": "sk-proj-0123456789ABCDEFGHIJKLMNOP",
        "tokonomix-live": "tok_live_LalGUSnSXyz12345",
        "tokonomix-label": "TOKONOMIX_API_KEY=plainvalue9876",
        "vault": "hvs.CAESIJ0123456789ABCDEFGHIJ",
        "url": "postgres://paperclip:supersecretpw@localhost:5432/paperclip",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n-----END RSA PRIVATE KEY-----",
    }
    for label, blob in cases.items():
        red = R.redact(f"prefix {blob} suffix")
        if "[REDACTED:" not in red:
            failures.append(f"[pattern:{label}] not redacted: {red!r}")
        # the URL case must keep the non-secret structure (user/host), only the password goes
        if label == "url" and ("paperclip" not in red or "localhost" not in red):
            failures.append(f"[pattern:url] over-redacted the non-secret URL parts: {red!r}")
        if label == "url" and "supersecretpw" in red:
            failures.append("[pattern:url] password survived")


def test_registry(failures):
    if R.register_secret(""):
        failures.append("[registry] empty value should be refused")
    if R.register_secret("short"):
        failures.append("[registry] sub-8-char value should be refused (would over-match)")
    val = "A1b2C3d4E5f6G7h8-literal-token-value"
    if not R.register_secret(val):
        failures.append("[registry] a long literal secret should register")
    out = R.redact(f"the value is {val} ok")
    if val in out or "[REDACTED:value]" not in out:
        failures.append(f"[registry] registered literal not scrubbed: {out!r}")
    # env harvesting
    os.environ["PAPERCLIP_TOKEN"] = "pcp_board_envharvestedtoken0123456789abcdef0123"
    R.register_env_secrets()
    if "pcp_board_envharvestedtoken0123456789abcdef0123" in R.redact("tok=pcp_board_envharvestedtoken0123456789abcdef0123"):
        failures.append("[registry] env-harvested token not scrubbed")


def test_redact_obj(failures):
    obj = {"status": "RECORDED", "why": "ok Bearer abcdef0123456789ABCDEF done",
           "nested": ["plain", {"k": "AKIAIOSFODNN7EXAMPLE"}], "n": 5, "b": True}
    out = R.redact_obj(obj)
    if "abcdef0123456789ABCDEF" in out["why"]:
        failures.append("[redact_obj] top-level string not redacted")
    if "AKIAIOSFODNN7EXAMPLE" in out["nested"][1]["k"]:
        failures.append("[redact_obj] nested string not redacted")
    if out["n"] != 5 or out["b"] is not True:
        failures.append("[redact_obj] non-string scalars must pass through unchanged")


def test_outward_boundary(failures):
    """The leak must be closed at the DESTINATION, not just in the helper."""
    secret = "ghp_DEADBEEF0123456789ABCDEFGHIJKLMNOPQR"

    # (a) Paperclip comment body that would be POSTed (dry-run; never hits the network).
    client = PaperclipClient("http://localhost:3100", "tok", "co", write_enabled=False)
    action = client.add_comment("issue-1", f"_why:_ failure dumped Authorization: Bearer {secret}")
    if secret in (action.comment or ""):
        failures.append("[boundary:paperclip] secret survived into the comment body that would POST")

    # (b) gate artifact file that _save_artifact writes.
    work = tempfile.mkdtemp(prefix="ue-redact-")
    store = OutcomeStore(os.path.join(work, "state"))
    gate = GateRunner(command=["true"], cwd=work, timeout=10)
    artifacts = os.path.join(work, "artifacts")
    os.makedirs(artifacts, exist_ok=True)  # _finalize_impl makes this in the real flow
    orch = Orchestrator(repo_dir=work, store=store, gate=gate, worker=None,
                        artifacts_dir=artifacts, unattended=True)
    path = orch._save_artifact("t1", f"FAILED: connected to postgres://u:{secret}@db and AKIAIOSFODNN7EXAMPLE")
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    if secret in content or "AKIAIOSFODNN7EXAMPLE" in content:
        failures.append(f"[boundary:artifact] secret survived into the saved gate artifact: {content!r}")

    # (c) build_report file: a secret in an outcome field must not reach the report. Hit the REAL
    #     build_report (not redact() directly), so removing its redact() call fails this test.
    from harness.report import build_report
    from harness.state import OutcomeState, TicketOutcome
    rsec = "hvs.CAESIJ0123456789ABCDEFGHIJ"
    o = TicketOutcome(ticket_id="t-sec", state=OutcomeState.FAILED_RETRYABLE,
                      why=f"failure printed token {rsec}",
                      exact_blocker=f"connected via postgres://u:{rsec}@db")
    if rsec in build_report([o], run_label="t"):
        failures.append("[boundary:report] secret survived into build_report() output")

    # (d) _emit: the JSON printed to stdout must be redacted (hit the REAL _emit).
    import contextlib
    import io
    from harness.run import _emit
    esec = "pcp_board_abcdef0123456789abcdef0123456789abcdef01"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit({"why": f"token was {esec}", "n": 1})
    if esec in buf.getvalue():
        failures.append("[boundary:_emit] secret survived into emitted JSON")


def _diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"  first diff at {i}: {a[max(0,i-20):i+20]!r} -> {b[max(0,i-20):i+20]!r}"
    return f"  length differs: {len(a)} vs {len(b)}"


def main() -> int:
    failures = []
    test_over_match_guard(failures)
    test_patterns(failures)
    test_registry(failures)
    test_redact_obj(failures)
    test_outward_boundary(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — redaction not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — shape-anchored patterns + literal registry scrub at every boundary; "
          "benign vocabulary/SHAs untouched; outward Paperclip+artifact writes proven clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
