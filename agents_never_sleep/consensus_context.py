"""Consensus grounding router — PURE core, import-safe (no config/DB/network at import).
Plans how the agent should ground a consensus call: inline (small), upload (large + available),
or degraded (large + upload unavailable — never inline an over-budget context). The agent EXECUTES
the plan via the tokonomix MCP; this module only DECIDES."""
from __future__ import annotations
import dataclasses

DEFAULT_CAP_TOKENS = 32000
DEFAULT_ROUTE_MARGIN = 24000          # buffer below the server's 32K cap (chars/4 underestimates code)
VERBATIM_BYTES = 256 * 1024

def estimate_tokens(text: str) -> int:
    """chars/4 estimate; the server re-measures authoritatively."""
    return len(text) // 4

def _split_verbatim(repo_context: str) -> list:
    """Caller-directed: the caller passes repo_context critical-evidence-FIRST. Head up to
    VERBATIM_BYTES stays verbatim; the remainder is digest-eligible."""
    head = repo_context[:VERBATIM_BYTES]
    rest = repo_context[VERBATIM_BYTES:]
    files = [{"content": head, "verbatim": True}]
    if rest:
        files.append({"content": rest, "verbatim": False})
    return files

@dataclasses.dataclass
class GroundingPlan:
    mode: str                 # "inline" | "upload" | "degraded"
    files: list = dataclasses.field(default_factory=list)  # for upload: [{content, verbatim}]
    reason: str = ""

def plan_grounding(repo_context: str, *, cap_tokens: int = DEFAULT_CAP_TOKENS,
                   route_margin: int = DEFAULT_ROUTE_MARGIN, upload_available: bool) -> GroundingPlan:
    if estimate_tokens(repo_context) < route_margin:
        return GroundingPlan(mode="inline", reason="under route margin")
    if not upload_available:
        return GroundingPlan(mode="degraded",
                             reason="over margin and context-upload unavailable")
    return GroundingPlan(mode="upload", files=_split_verbatim(repo_context),
                         reason="over margin, upload available")

def plan_to_upload_args(plan: "GroundingPlan") -> dict:
    """Exact tokonomix_upload({files}) argument dict — the agent passes this through verbatim."""
    return {"files": [{"content": f["content"], "verbatim": f["verbatim"]} for f in plan.files]}
