"""Phase-0 capability preflight — MEASURE, never assume (Thread 6).

Probes the environment and writes a capability profile the rest of the run branches on. A
missing capability is never fatal on its own: it lowers the expected yield and raises
conservatism. The council added two hard requirements folded in here:
  * classify the repo EXECUTION MODE (can-build / can-lint-only / can-static-edit-only /
    cannot-safely-execute) before any ticket work — unattended runs die on tool bootstrap,
  * emit an EXPECTED-YIELD estimate and warn at config time when the env can't support a run.

Platform/provider detection is best-effort: known markers → that adapter; unknown → generic
safe mode (abstract instructions only, more conservative, no platform-specific enforcement).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess


@dataclasses.dataclass
class CapabilityProfile:
    platform: str = "unknown"            # claude-code | codex | gemini | copilot | unknown
    has_git: bool = False
    git_clean: bool = False
    exec_mode: str = "cannot-safely-execute"
    gates: list = dataclasses.field(default_factory=list)   # candidate (name, command)
    has_tokonomix: bool = False
    has_vault: bool = False
    has_paperclip: bool = False
    unattended: bool = False
    expected_yield: str = "low"          # low | medium | high
    warnings: list = dataclasses.field(default_factory=list)

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _detect_platform() -> str:
    # Explicit marker keys only (review 2026-06-10): no substring scans over the whole
    # env, and no API-key heuristics — an exported GEMINI_API_KEY is set by anyone using
    # the Gemini SDK and does not mean this session runs inside Gemini CLI. The shared
    # marker table lives in agent_clis.SESSION_MARKERS; this stays a hint, never a
    # launch-time spawn selector.
    from .agent_clis import detect_session_platform
    hint = detect_session_platform()
    return {"claude": "claude-code"}.get(hint, hint) or "unknown"


def _detect_gates(cwd: str) -> list:
    gates = []
    j = lambda *p: os.path.join(cwd, *p)
    if os.path.exists(j("package.json")):
        gates.append(("node-test", ["npm", "test", "--silent"]))
        if os.path.exists(j("tsconfig.json")):
            gates.append(("typecheck", ["npx", "tsc", "--noEmit"]))
    if os.path.exists(j("composer.json")):
        gates.append(("phpunit", ["vendor/bin/phpunit"]))
    if os.path.exists(j("pyproject.toml")) or os.path.exists(j("pytest.ini")) or os.path.exists(j("setup.cfg")):
        gates.append(("pytest", ["python3", "-m", "pytest", "-q"]))
    # always-available stdlib fallback when there are unittest-style tests
    if any(n.startswith("test_") and n.endswith(".py") for n in _safe_listdir(cwd)):
        gates.append(("unittest", ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]))
    return gates


def _safe_listdir(cwd: str) -> list:
    try:
        return os.listdir(cwd)
    except OSError:
        return []


def _detect_exec_mode(cwd: str, gates: list) -> str:
    j = lambda *p: os.path.join(cwd, *p)
    writable = os.access(cwd, os.W_OK)
    if not writable:
        return "cannot-safely-execute"
    if os.path.exists(j("package.json")):
        return "can-build" if os.path.isdir(j("node_modules")) else "can-static-edit-only"
    if os.path.exists(j("composer.json")):
        return "can-build" if os.path.isdir(j("vendor")) else "can-static-edit-only"
    if gates:
        return "can-build"          # python/unittest works with stdlib
    return "can-static-edit-only"


def _estimate_yield(profile: "CapabilityProfile") -> str:
    if not profile.has_git:
        profile.warnings.append("no VCS — reversibility not guaranteed; conservatism raised")
    if profile.exec_mode in ("cannot-safely-execute", "can-static-edit-only"):
        profile.warnings.append(f"exec mode '{profile.exec_mode}' — gates limited; more parking likely")
    if not profile.gates:
        profile.warnings.append("no deterministic gates detected — quality backbone weak")
    score = 0
    score += 1 if profile.has_git else 0
    score += 1 if profile.gates else 0
    score += 1 if profile.exec_mode == "can-build" else 0
    return {0: "low", 1: "low", 2: "medium", 3: "high"}[score]


def run_preflight(cwd: str, *, unattended: bool) -> CapabilityProfile:
    p = CapabilityProfile(unattended=unattended)
    p.platform = _detect_platform()
    if _has("git"):
        try:
            inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                    capture_output=True, text=True, timeout=30)
            p.has_git = inside.returncode == 0 and inside.stdout.strip() == "true"
            if p.has_git:
                st = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                                    capture_output=True, text=True, timeout=30)
                p.git_clean = st.stdout.strip() == ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    p.gates = _detect_gates(cwd)
    p.exec_mode = _detect_exec_mode(cwd, p.gates)
    from .onboarding import credential_present  # single shared probe (honors TOKONOMIX_CREDS_FILE)
    p.has_tokonomix = credential_present()
    p.has_vault = bool(os.environ.get("VAULT_ADDR")) or _has("vault")
    p.has_paperclip = _probe_paperclip()
    p.expected_yield = _estimate_yield(p)
    if p.platform == "unknown":
        p.warnings.append("unknown platform — running in generic safe mode (no platform enforcement)")
    return p


def _probe_paperclip() -> bool:
    # non-destructive, fast, never blocks: a 0.5s connect attempt to the board. Hosts come from
    # UE_PAPERCLIP_PROBE (comma-separated host:port) and default to localhost — no site-specific
    # address is baked into the portable core.
    import socket
    spec = os.environ.get("UE_PAPERCLIP_PROBE", "localhost:3100")
    targets = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        try:
            port_int = int(port)
            if not 0 < port_int <= 65535:
                port_int = 3100
        except (ValueError, OverflowError):
            port_int = 3100
        targets.append((host, port_int))
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def write_profile(profile: CapabilityProfile, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile.to_json(), fh, indent=2, sort_keys=True)
