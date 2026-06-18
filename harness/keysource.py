"""Key source — resolve an optional secret (Paperclip token, tokonomix key) from env or Vault.

The harness already reads tokens from env. This adds the OTHER half of the existing `token_ref`
convention the config/wizard already writes: `env:VAR` (read an env var) or `vault:<logical-path>[#field]`
(read a KV-v2 secret from the configured HashiCorp Vault). Whatever is resolved is REGISTERED with the redaction
layer so it can never leak into a report / artifact / Paperclip comment.

Vault contract (HashiCorp Vault KV-v2): loopback `http://127.0.0.1:8200`, KV-v2, AppRole login
(`role_id`+`secret_id` -> `auth.client_token`) or a direct `VAULT_TOKEN`, read at `/v1/<mount>/data/
<rest>` with header `X-Vault-Token`, value at `data.data.<field>`. Errors are sanitized at the boundary
— a token / secret-id / KV value never appears in an exception message or log.

DELIBERATE DEVIATION from the vault skill's "fail hard on 403/404/503": that rule is for app crypto
where a null key corrupts data. Here the secret is OPTIONAL (Paperclip writes already default to
dry-run; a missing tokonomix key just disables advisory review), and the overriding contract is that an
unattended night must NEVER hard-stop. So a Vault read failure DEGRADES — returns no value plus a
recorded blind-spot for the morning report — rather than crashing. It still never SILENTLY defaults.
"""
from __future__ import annotations

import dataclasses
import json
import os
import urllib.error
import urllib.request

_DEFAULT_VAULT_ADDR = "http://127.0.0.1:8200"


class VaultError(Exception):
    """Raised on a Vault transport/auth/read failure. Message is SANITIZED — never a secret."""


class VaultClient:
    """Minimal KV-v2 reader. Injectable opener so resolution is unit-tested without a live Vault."""

    def __init__(self, addr: str, *, token: str | None = None, role_id: str | None = None,
                 secret_id: str | None = None, opener=None, timeout: int = 10):
        self.addr = addr.rstrip("/")
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _http(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        url = f"{self.addr}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            # NB: only the method+path+code — never the body (which may echo a token) or our creds.
            raise VaultError(f"{method} {path} -> HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VaultError(f"{method} {path} -> {type(exc).__name__}") from None

    def token(self) -> str:
        """A usable Vault token: a direct VAULT_TOKEN if given, else an AppRole login. Cached."""
        if self._token:
            return self._token
        if not (self._role_id and self._secret_id):
            raise VaultError("no Vault auth material (need VAULT_TOKEN or role_id+secret_id)")
        resp = self._http("POST", "/v1/auth/approle/login",
                          {"role_id": self._role_id, "secret_id": self._secret_id})
        tok = ((resp or {}).get("auth") or {}).get("client_token")
        if not tok:
            raise VaultError("AppRole login returned no client_token")
        self._token = tok
        return tok

    def read_kv(self, logical_path: str, field: str = "token") -> str:
        """Read one field of a KV-v2 secret. `logical_path` is `<mount>/<rest>` (e.g.
        `secret/paperclip/board-token`); the `/data/` infix is inserted for the v2 API."""
        parts = [p for p in logical_path.strip("/").split("/") if p]
        if len(parts) < 2:
            raise VaultError(f"bad vault path '{logical_path}' (need <mount>/<path>)")
        api = f"/v1/{parts[0]}/data/{'/'.join(parts[1:])}"
        resp = self._http("GET", api, headers={"X-Vault-Token": self.token()})
        data = ((resp or {}).get("data") or {}).get("data") or {}
        if field not in data or not data[field]:
            raise VaultError(f"vault secret at '{logical_path}' has no field '{field}'")
        return str(data[field])


@dataclasses.dataclass
class Resolved:
    value: str | None
    source: str            # "env" | "vault" | "absent"
    blind_spot: str | None  # a morning-report note when a CONFIGURED source could not be read


def vault_from_env(config: dict, opener=None) -> VaultClient | None:
    """Build a VaultClient from env/config auth material, or None when none is available."""
    addr = (os.environ.get("VAULT_ADDR")
            or ((config.get("integrations", {}).get("vault", {}) or {}).get("addr"))
            or _DEFAULT_VAULT_ADDR)
    token = os.environ.get("VAULT_TOKEN")
    role_id = os.environ.get("VAULT_ROLE_ID")
    secret_id = os.environ.get("VAULT_SECRET_ID")
    if not (token or (role_id and secret_id)):
        return None
    return VaultClient(addr, token=token, role_id=role_id, secret_id=secret_id, opener=opener)


def _vault_enabled(config: dict) -> bool:
    return bool((config.get("integrations", {}).get("vault", {}) or {}).get("enabled"))


def resolve_ref(ref: str | None, *, config: dict, client: VaultClient | None = None,
                register: bool = True) -> Resolved:
    """Resolve a `token_ref` (`env:VAR` or `vault:path[#field]`). Always registers a resolved value
    with the redaction layer. A Vault failure DEGRADES (value=None + blind_spot), never raises."""
    if not ref:
        return Resolved(None, "absent", None)

    def _reg(val):
        if val and register:
            from .redact import register_secret
            register_secret(val)
        return val

    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        val = os.environ.get(rest) or None
        spot = None if val else f"env token_ref configured but ${rest} is not set"
        return Resolved(_reg(val), "env" if val else "absent", spot)

    if scheme == "vault":
        path, _, field = rest.partition("#")
        field = field or "token"
        if not _vault_enabled(config):
            return Resolved(None, "absent",
                            f"vault token_ref configured but the vault integration is disabled "
                            f"(path {path!r}); enable integrations.vault or provide an env: ref")
        client = client or vault_from_env(config)
        if client is None:
            return Resolved(None, "absent",
                            f"vault token_ref {path!r} configured but no VAULT_TOKEN / "
                            f"VAULT_ROLE_ID+VAULT_SECRET_ID in env to authenticate")
        try:
            return Resolved(_reg(client.read_kv(path, field)), "vault", None)
        except VaultError as exc:
            return Resolved(None, "absent", f"vault read failed for {path!r}: {exc}")

    return Resolved(None, "absent", f"unrecognized token_ref scheme {scheme!r} (use env: or vault:)")
