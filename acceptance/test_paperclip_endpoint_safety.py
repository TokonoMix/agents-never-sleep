#!/usr/bin/env python3
"""Paperclip endpoint safety test (post-audit hardening, L5).

`integrations.paperclip.base_url` defaults to loopback (`http://localhost:3100`), so a bearer token
travels in cleartext but never leaves the host. If an operator points it at a REMOTE host without
also switching to https, the token goes out on the wire in cleartext on every request — silent
before this fix. `run._note_unsafe_paperclip_endpoint` appends a blind spot (not a hard-fail: the
run must still proceed) whenever that's the case; this test proves it fires for a remote http://
endpoint and stays silent for the shipped loopback default and for https.

Exit 0 = GREEN.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.run import _note_unsafe_paperclip_endpoint  # noqa: E402


class _StubCtx:
    def __init__(self):
        self.key_blind_spots = []


def test_remote_http_flagged(failures):
    ctx = _StubCtx()
    _note_unsafe_paperclip_endpoint(ctx, {"base_url": "http://paperclip.example.com"})
    if not ctx.key_blind_spots:
        failures.append("[unsafe] remote http:// base_url should add a blind spot")
    elif "cleartext" not in ctx.key_blind_spots[0]:
        failures.append(f"[unsafe] blind spot message unclear: {ctx.key_blind_spots[0]!r}")


def test_shipped_default_is_silent(failures):
    ctx = _StubCtx()
    _note_unsafe_paperclip_endpoint(ctx, {"base_url": "http://localhost:3100"})
    if ctx.key_blind_spots:
        failures.append(f"[safe] shipped loopback default should be silent: {ctx.key_blind_spots}")


def test_remote_https_is_silent(failures):
    ctx = _StubCtx()
    _note_unsafe_paperclip_endpoint(ctx, {"base_url": "https://paperclip.example.com"})
    if ctx.key_blind_spots:
        failures.append(f"[safe] https should be silent: {ctx.key_blind_spots}")


def test_missing_base_url_is_silent(failures):
    ctx = _StubCtx()
    _note_unsafe_paperclip_endpoint(ctx, {})
    if ctx.key_blind_spots:
        failures.append(f"[safe] no base_url configured should be silent: {ctx.key_blind_spots}")


def main() -> int:
    failures = []
    test_remote_http_flagged(failures)
    test_shipped_default_is_silent(failures)
    test_remote_https_is_silent(failures)
    test_missing_base_url_is_silent(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — Paperclip endpoint safety blind spot not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — a remote non-https Paperclip base_url raises a blind spot; the "
          "shipped loopback default and https stay silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
