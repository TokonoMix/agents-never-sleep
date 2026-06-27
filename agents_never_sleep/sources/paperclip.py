"""Paperclip ticket-source adapter (site-specific — NOT part of the portable core).

The portable core works from local `.md` tickets (harness/tickets.py). This optional adapter lets a
run instead pull tickets from a Paperclip project and push outcomes back, while keeping ALL the
tracker-specific details quarantined in this one module — the orchestrator/state machine/council
never learn about Paperclip. It is pure stdlib (urllib), with an injectable opener so the read/write
logic is unit-tested without touching the live board.

SAFETY: pushing status changes / comments mutates SHARED infra. So writes default to DRY-RUN — the
adapter logs the intended change and returns it, but performs no live mutation — unless the project
config explicitly sets integrations.paperclip.write_enabled = true. Reads are always live.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request

from ..state import OutcomeState
from ..tickets import Ticket

# Paperclip statuses considered "open" (actionable). Anything else (done/cancelled) is skipped.
DEFAULT_OPEN_STATUSES = ("backlog", "todo", "in_progress", "in-progress", "blocked", "open")

# How a durable outcome maps to a Paperclip status. None = do NOT change status (only comment) — used
# where auto-closing would be wrong: DONE_LOW_CONFIDENCE needs daylight review (never silently "done"),
# and retryable/failed tickets stay open for the next resume.
OUTCOME_TO_STATUS = {
    OutcomeState.DONE: "done",
    OutcomeState.DONE_LOW_CONFIDENCE: None,
    OutcomeState.PARKED_DECISION: "blocked",
    OutcomeState.PARKED_FOUNDATIONAL: "blocked",
    OutcomeState.BLOCKED_ENV: "blocked",
    OutcomeState.FAILED_RETRYABLE: None,
    OutcomeState.FAILED_BUG_IN_AGENT: None,
}


class PaperclipError(Exception):
    pass


@dataclasses.dataclass
class PushAction:
    """What the adapter did (or, in dry-run, WOULD do) for one outcome."""
    issue_id: str
    set_status: str | None
    comment: str | None
    dry_run: bool


class PaperclipClient:
    def __init__(self, base_url: str, token: str, company_id: str, *,
                 opener=None, timeout: int = 15, write_enabled: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.company_id = company_id
        self.timeout = timeout
        self.write_enabled = write_enabled
        self._opener = opener or urllib.request.urlopen

    # ---- transport -----------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raise PaperclipError(f"{method} {path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PaperclipError(f"{method} {path} -> {type(exc).__name__}") from exc

    # ---- read ----------------------------------------------------------------------------

    def list_open_issues(self, project_id: str, open_statuses=DEFAULT_OPEN_STATUSES,
                         limit: int = 500) -> list:
        """Open issues of one project. Filters SERVER-SIDE by projectId+status (the API honours both)
        so a large company board can't push a project's tickets past the page cap. If any single
        query hits the cap, that page is truncated — we RAISE rather than silently process a subset
        (silent backlog truncation is the exact failure this skill exists to prevent)."""
        opens = {s.lower() for s in open_statuses}
        seen = {}
        for status in open_statuses:
            q = urllib.parse.urlencode({"projectId": project_id, "status": status, "limit": limit})
            data = self._request("GET", f"/api/companies/{self.company_id}/issues?{q}")
            items = data if isinstance(data, list) else (data or {}).get("issues", [])
            if len(items) >= limit:
                raise PaperclipError(
                    f"status '{status}' returned the {limit}-row cap — backlog may be truncated; "
                    "refusing to run on a partial read (paginate or narrow the project)")
            for i in items:
                # defensive client-side recheck (don't trust the server filter blindly)
                if i.get("projectId") == project_id and str(i.get("status", "")).lower() in opens:
                    seen[i.get("id")] = i
        return list(seen.values())

    # ---- write (dry-run unless write_enabled) --------------------------------------------

    def set_status(self, issue_id: str, status: str) -> PushAction:
        if self.write_enabled:
            self._request("PATCH", f"/api/issues/{issue_id}", {"status": status})
        return PushAction(issue_id, status, None, dry_run=not self.write_enabled)

    def add_comment(self, issue_id: str, body: str) -> PushAction:
        # Outward, IRREVERSIBLE write to shared infra — scrub secrets at the boundary, both on the
        # wire and in the returned/recorded PushAction (dry-run included, so the log can't leak).
        from ..redact import redact
        body = redact(body)
        if self.write_enabled:
            self._request("POST", f"/api/issues/{issue_id}/comments", {"body": body})
        return PushAction(issue_id, None, body, dry_run=not self.write_enabled)


def to_ticket(issue: dict) -> Ticket:
    """Map a Paperclip issue to a harness Ticket. The Paperclip UUID is stashed in meta so outcomes
    can be pushed back to the right issue even though the ticket id is the human identifier."""
    pid = issue.get("id")
    num = issue.get("issueNumber")
    # NB: `f"PCP-{num}"` is always truthy, so guard it — else two issues without an identifier both
    # collapse to "PCP-None" and collide in the OutcomeStore + push-back map.
    human_id = issue.get("identifier") or (f"PCP-{num}" if num else None) or pid
    return Ticket(
        id=str(human_id), title=issue.get("title") or str(human_id),
        body=issue.get("description") or "",
        meta={"paperclip_id": pid, "status": issue.get("status"),
              "priority": issue.get("priority"), "labels": issue.get("labels") or []},
        path=f"paperclip:{pid}")


def comment_for(outcome) -> str | None:
    """A concise status comment for the morning push. None when there's nothing worth saying."""
    state = outcome.state
    if state == OutcomeState.DONE:
        return None  # status->done is enough; avoid noise
    head = {
        OutcomeState.DONE_LOW_CONFIDENCE: "✅⚠️ Completed but NEEDS DAYLIGHT REVIEW (not auto-closed).",
        OutcomeState.PARKED_DECISION: "⏸️ Parked (decision needed).",
        OutcomeState.PARKED_FOUNDATIONAL: "⏸️ Parked (foundational ambiguity; dependents quarantined).",
        OutcomeState.BLOCKED_ENV: "🚧 Blocked by environment/tooling.",
        OutcomeState.FAILED_RETRYABLE: "↻ Attempt failed (reverted); retry on the next run.",
        OutcomeState.FAILED_BUG_IN_AGENT: "🐞 Needs a human look (agent/harness issue).",
    }.get(state, f"State: {state.value}")
    lines = [f"**agents-never-sleep:** {head}", f"_why:_ {outcome.why}"]
    if outcome.human_action_required:
        lines.append(f"_next action:_ {outcome.human_action_required}")
    if outcome.review_coverage and outcome.review_coverage != "n/a":
        lines.append(f"_review:_ {outcome.review_coverage}")
    return "\n".join(lines)


def push_outcome(client: PaperclipClient, paperclip_id: str, outcome) -> list:
    """Push one outcome back to its issue: set status (per the map; None = leave open) + a comment.
    Returns the PushAction(s) (dry-run unless write_enabled), so the caller can report what happened."""
    actions = []
    status = OUTCOME_TO_STATUS.get(outcome.state)
    if status is not None:
        actions.append(client.set_status(paperclip_id, status))
    body = comment_for(outcome)
    if body:
        actions.append(client.add_comment(paperclip_id, body))
    return actions
