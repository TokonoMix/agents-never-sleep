#!/usr/bin/env python3
"""Key-source test — resolve an optional secret from env or Vault, register it for redaction, and
DEGRADE (never crash) on a Vault failure.

Vault is exercised with an injected fake opener — no live Vault, deterministic on any box. The two
load-bearing guarantees: (1) a resolved secret is registered so redact() scrubs it everywhere, and
(2) a configured-but-unreadable Vault ref yields a blind-spot, not an exception that would stop the
night. Errors must also be SANITIZED — a token / secret-id never appears in a VaultError message.

Exit 0 = GREEN.
"""
import io
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import redact as R  # noqa: E402
from agents_never_sleep.keysource import VaultClient, VaultError, resolve_ref  # noqa: E402

VAULT_ON = {"integrations": {"vault": {"enabled": True}}}
VAULT_OFF = {"integrations": {"vault": {"enabled": False}}}


class _Resp:
    def __init__(self, body):
        self._b = body.encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._b


def _opener(routes, raise_code=None, body=b""):
    """Fake urllib opener. routes: dict {path-substring: json-body}. raise_code: raise HTTPError
    (with `body` as the error response body, to prove it never leaks into the message)."""
    def open_(req, timeout=None):
        if raise_code is not None:
            raise urllib.error.HTTPError(req.full_url, raise_code, "x", {}, io.BytesIO(body))
        for frag, b in routes.items():
            if frag in req.full_url:
                return _Resp(b)
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
    return open_


def test_read_kv(failures):
    sec = "pcp_board_FAKEvaultvalue0123456789abcdef0123456789"
    c = VaultClient("http://v", token="roottok-direct-12345",
                    opener=_opener({"/v1/secret/data/paperclip/board-token":
                                    '{"data":{"data":{"token":"%s"}}}' % sec}))
    if c.read_kv("secret/paperclip/board-token", "token") != sec:
        failures.append("[read_kv] did not return the KV field value")
    # missing field -> VaultError
    c2 = VaultClient("http://v", token="t-12345678",
                     opener=_opener({"/data/": '{"data":{"data":{"other":"x"}}}'}))
    try:
        c2.read_kv("secret/x/y", "token")
        failures.append("[read_kv] missing field should raise VaultError")
    except VaultError:
        pass
    # bad path
    try:
        c2.read_kv("nomount", "token")
        failures.append("[read_kv] single-segment path should raise")
    except VaultError:
        pass


def test_approle_login(failures):
    sec = "tok-from-approle-login-987654321"
    routes = {"/v1/auth/approle/login": '{"auth":{"client_token":"hvs.LOGINTOKEN0123456789"}}',
              "/v1/secret/data/tokonomix/api-key": '{"data":{"data":{"key":"%s"}}}' % sec}
    c = VaultClient("http://v", role_id="role-id-123456", secret_id="secret-id-123456",
                    opener=_opener(routes))
    if c.read_kv("secret/tokonomix/api-key", "key") != sec:
        failures.append("[approle] login+read chain failed")
    # no auth material at all
    try:
        VaultClient("http://v", opener=_opener({})).token()
        failures.append("[approle] no auth material should raise")
    except VaultError:
        pass


def test_sanitized_errors(failures):
    rid, sid = "ROLEID-secretmaterial", "SECRETID-secretmaterial"
    c = VaultClient("http://v", role_id=rid, secret_id=sid,
                    opener=_opener({}, raise_code=403))
    try:
        c.token()  # AppRole login -> 403
        failures.append("[sanitize] 403 login should raise")
    except VaultError as e:
        msg = str(e)
        if "403" not in msg:
            failures.append(f"[sanitize] error should name the HTTP code: {msg!r}")
        if rid in msg or sid in msg:
            failures.append("[sanitize] error message LEAKED role_id/secret_id")
    # a 403 whose RESPONSE BODY echoes a credential must not leak it into the message
    leak_body = b'{"errors":["role_id ROLEID-secretmaterial unauthorized"]}'
    c2 = VaultClient("http://v", role_id=rid, secret_id=sid,
                     opener=_opener({}, raise_code=403, body=leak_body))
    try:
        c2.token()
        failures.append("[sanitize] body-leak 403 should raise")
    except VaultError as e:
        if rid in str(e) or "unauthorized" in str(e):
            failures.append("[sanitize] HTTPError response BODY leaked into the VaultError message")


def test_resolve_env(failures):
    os.environ["UE_TEST_TOK"] = "env-secret-value-123456789"
    r = resolve_ref("env:UE_TEST_TOK", config=VAULT_OFF)
    if r.value != "env-secret-value-123456789" or r.source != "env":
        failures.append(f"[resolve env] wrong result: {r}")
    if "env-secret-value-123456789" in R.redact("leak env-secret-value-123456789 here"):
        failures.append("[resolve env] resolved value not registered for redaction")
    # absent env var -> 'absent' AND a blind_spot (a configured ref that didn't resolve isn't silent)
    os.environ.pop("UE_TEST_MISSING", None)
    rm = resolve_ref("env:UE_TEST_MISSING", config=VAULT_OFF)
    if rm.source != "absent":
        failures.append("[resolve env] missing var should be 'absent'")
    if not rm.blind_spot or "UE_TEST_MISSING" not in rm.blind_spot:
        failures.append(f"[resolve env] missing configured var should record a blind spot: {rm}")


def test_resolve_vault(failures):
    sec = "vault-resolved-secret-value-abcdef123456"
    client = VaultClient("http://v", token="t-12345678",
                         opener=_opener({"/v1/secret/data/svc/key": '{"data":{"data":{"k":"%s"}}}' % sec}))
    r = resolve_ref("vault:secret/svc/key#k", config=VAULT_ON, client=client)
    if r.value != sec or r.source != "vault":
        failures.append(f"[resolve vault] wrong result: {r}")
    if sec in R.redact(f"oops {sec}"):
        failures.append("[resolve vault] resolved value not registered for redaction")

    # vault disabled -> blind spot, no value, no crash
    rd = resolve_ref("vault:secret/svc/key", config=VAULT_OFF, client=client)
    if rd.value is not None or not rd.blind_spot or "disabled" not in rd.blind_spot:
        failures.append(f"[resolve vault] disabled should degrade with a blind spot: {rd}")

    # enabled but read fails -> blind spot, no value, NO exception
    bad = VaultClient("http://v", token="t-12345678", opener=_opener({}, raise_code=503))
    rf = resolve_ref("vault:secret/svc/key", config=VAULT_ON, client=bad)
    if rf.value is not None or not rf.blind_spot or "vault read failed" not in rf.blind_spot:
        failures.append(f"[resolve vault] read failure should degrade, not crash: {rf}")

    # enabled but no auth material in env and no client -> blind spot
    for k in ("VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID"):
        os.environ.pop(k, None)
    rn = resolve_ref("vault:secret/svc/key", config=VAULT_ON)
    if rn.value is not None or not rn.blind_spot or "VAULT_TOKEN" not in rn.blind_spot:
        failures.append(f"[resolve vault] no auth material should degrade with a blind spot: {rn}")


def test_resolve_edge(failures):
    if resolve_ref(None, config=VAULT_ON).source != "absent":
        failures.append("[edge] None ref should be absent")
    bad = resolve_ref("weird:thing", config=VAULT_ON)
    if bad.value is not None or "unrecognized" not in (bad.blind_spot or ""):
        failures.append(f"[edge] unknown scheme should degrade with a blind spot: {bad}")


def test_blind_spots_surfaced(failures):
    """A key-resolution blind spot must reach the morning report (regression guard: the list was
    previously collected but never read). Drive a StepDriver with no tickets -> DRAINED + report."""
    import shutil
    import tempfile
    from agents_never_sleep.gates import GateRunner
    from agents_never_sleep.orchestrator import Orchestrator
    from agents_never_sleep.state import OutcomeStore
    from agents_never_sleep.driver import StepDriver
    work = tempfile.mkdtemp(prefix="ue-ks-")
    repo = os.path.join(work, "repo")
    shutil.copytree(os.path.join(HERE, "sandbox"), repo)
    store = OutcomeStore(os.path.join(work, "state"))
    gate = GateRunner(command=["true"], cwd=repo, timeout=10)
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=os.path.join(work, "artifacts"), unattended=True)
    report_path = os.path.join(work, "report.md")
    note = "vault read failed for 'secret/svc/key': GET ... -> HTTP 503"
    driver = StepDriver(orch=orch, tickets=[], store=store, state_dir=os.path.join(work, "state"),
                        report_path=report_path, config={}, key_blind_spots=[note])
    res = driver.next_ticket()  # no tickets -> terminal, writes the report
    if res.get("status") not in ("DRAINED", "LOW_YIELD", "HALTED"):
        failures.append(f"[surfaced] expected a terminal status, got {res.get('status')}")
    report = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else ""
    if "BLIND SPOT" not in report or "vault read failed" not in report:
        failures.append(f"[surfaced] key_blind_spots did not reach the report: {report!r}")


def main() -> int:
    failures = []
    saved = {k: os.environ.get(k) for k in
             ("UE_TEST_TOK", "VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID")}
    try:
        test_read_kv(failures)
        test_approle_login(failures)
        test_sanitized_errors(failures)
        test_resolve_env(failures)
        test_resolve_vault(failures)
        test_resolve_edge(failures)
        test_blind_spots_surfaced(failures)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — key source not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — env/Vault resolution, AppRole login, sanitized errors, redaction "
          "registration, and graceful degradation (blind-spot not crash) all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
