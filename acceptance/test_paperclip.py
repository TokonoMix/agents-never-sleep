#!/usr/bin/env python3
"""Paperclip adapter test — pure, no live board calls (injected fake transport).

Proves: open-issue filtering by project+status, issue->Ticket mapping (incl the Paperclip UUID kept
for push-back), outcome->status mapping (DONE->done; DONE_LOW_CONFIDENCE never auto-closed; parked->
blocked; retryable left open), comment generation, and the DRY-RUN safety default (no live mutation
unless write_enabled). Exit 0 = GREEN.
"""
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep.sources import paperclip as pc  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore, TicketOutcome  # noqa: E402

ISSUES = [
    {"id": "u-1", "identifier": "INF-1", "projectId": "P", "title": "open one",
     "description": "body one", "status": "todo", "priority": "high", "labels": []},
    {"id": "u-2", "identifier": "INF-2", "projectId": "P", "title": "blocked one",
     "description": "body two", "status": "blocked", "priority": "low", "labels": ["x"]},
    {"id": "u-3", "identifier": "INF-3", "projectId": "P", "title": "done one",
     "description": "b3", "status": "done", "priority": "low", "labels": []},
    {"id": "u-4", "identifier": "OTH-1", "projectId": "OTHER", "title": "other project",
     "description": "b4", "status": "todo", "priority": "low", "labels": []},
]


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []  # (method, url, had_body)

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.data is not None))
        if req.get_method() == "GET":
            return _Resp(self.issues)
        return _Resp({})


def _outcome(state, **kw):
    return TicketOutcome(ticket_id="t", state=state, why=kw.get("why", "because"),
                         human_action_required=kw.get("action", ""),
                         review_coverage=kw.get("cov", "n/a"))


def main() -> int:
    failures = []

    # ---- read: filter to open issues of project P -------------------------------------
    op = _Opener(ISSUES)
    client = pc.PaperclipClient("http://x", "tok", "C", opener=op)
    opens = client.list_open_issues("P")
    ids = sorted(i["id"] for i in opens)
    if ids != ["u-1", "u-2"]:
        failures.append(f"[read] expected open P issues u-1,u-2; got {ids}")

    # ---- map issue -> ticket ----------------------------------------------------------
    t = pc.to_ticket(ISSUES[0])
    if t.id != "INF-1" or t.title != "open one" or t.body != "body one":
        failures.append(f"[map] bad ticket fields: {t.id}/{t.title}/{t.body!r}")
    if t.meta.get("paperclip_id") != "u-1":
        failures.append("[map] paperclip UUID not preserved for push-back")

    # ---- dry-run default: writes are NOT executed -------------------------------------
    op2 = _Opener(ISSUES)
    dry = pc.PaperclipClient("http://x", "tok", "C", opener=op2, write_enabled=False)
    actions = pc.push_outcome(dry, "u-1", _outcome(OutcomeState.DONE))
    if any(a.method if hasattr(a, "method") else False for a in actions):
        pass
    write_calls = [c for c in op2.calls if c[0] in ("PATCH", "POST")]
    if write_calls:
        failures.append(f"[dryrun] write_enabled=False made live write calls: {write_calls}")
    if not actions or actions[0].set_status != "done" or not actions[0].dry_run:
        failures.append(f"[dryrun] DONE should plan a dry-run status=done, got {actions}")

    # ---- write_enabled: a live PATCH happens ------------------------------------------
    op3 = _Opener(ISSUES)
    live = pc.PaperclipClient("http://x", "tok", "C", opener=op3, write_enabled=True)
    pc.push_outcome(live, "u-1", _outcome(OutcomeState.DONE))
    if not any(c[0] == "PATCH" for c in op3.calls):
        failures.append("[write] write_enabled=True did not PATCH status")

    # ---- disposition mapping ----------------------------------------------------------
    # DONE_LOW_CONFIDENCE must NOT auto-close (status None) but MUST comment (daylight review)
    a = pc.push_outcome(dry, "u-2", _outcome(OutcomeState.DONE_LOW_CONFIDENCE, action="review it"))
    statuses = [x.set_status for x in a if x.set_status]
    comments = [x.comment for x in a if x.comment]
    if statuses:
        failures.append(f"[map] DONE_LOW_CONFIDENCE must not set a status, got {statuses}")
    if not comments or "DAYLIGHT REVIEW" not in comments[0]:
        failures.append(f"[map] DONE_LOW_CONFIDENCE must comment daylight review, got {comments}")
    # parked -> blocked + comment
    a2 = pc.push_outcome(dry, "u-2", _outcome(OutcomeState.PARKED_DECISION, action="decide"))
    if [x.set_status for x in a2 if x.set_status] != ["blocked"]:
        failures.append("[map] PARKED_DECISION should set status blocked")
    # retryable -> leave open (no status), still comment
    a3 = pc.push_outcome(dry, "u-1", _outcome(OutcomeState.FAILED_RETRYABLE))
    if [x.set_status for x in a3 if x.set_status]:
        failures.append("[map] FAILED_RETRYABLE should leave the issue open (no status change)")

    # ---- truncation guard: a status hitting the row cap must RAISE, not silently truncate ----
    class _CapOpener:
        def __call__(self, req, timeout=None):
            return _Resp([{"id": f"x{n}", "projectId": "P", "status": "todo"} for n in range(3)])
    capped = pc.PaperclipClient("http://x", "tok", "C", opener=_CapOpener())
    try:
        capped.list_open_issues("P", open_statuses=("todo",), limit=3)
        failures.append("[trunc] hitting the row cap should raise PaperclipError")
    except pc.PaperclipError:
        pass

    # ---- identifier fallback must not collapse distinct issues to one id ----------------
    if pc.to_ticket({"id": "uuid-a", "title": "no id"}).id != "uuid-a":
        failures.append("[id] missing identifier+number should fall back to the UUID, not PCP-None")
    if pc.to_ticket({"id": "uuid-b", "issueNumber": 7, "title": "n"}).id != "PCP-7":
        failures.append("[id] issueNumber should produce PCP-7")
    a_id = pc.to_ticket({"id": "uuid-a", "title": "a"}).id
    b_id = pc.to_ticket({"id": "uuid-b", "title": "b"}).id
    if a_id == b_id:
        failures.append("[id] two identifier-less issues collided on the same ticket id")

    # ---- push-back is idempotent across repeated terminal/report calls ------------------
    work = tempfile.mkdtemp(prefix="ue-pcp-idem-")
    sd = os.path.join(work, "state")
    store = OutcomeStore(sd)
    store.write(TicketOutcome(ticket_id="INF-1", state=OutcomeState.DONE, why="done"))
    op_i = _Opener(ISSUES)
    client_i = pc.PaperclipClient("http://x", "tok", "C", opener=op_i, write_enabled=True)
    from agents_never_sleep.run import _push_paperclip
    ctx = types.SimpleNamespace(paperclip=client_i, pcp_id_by_ticket={"INF-1": "u-1"},
                                store=store, state_dir=sd)
    r1 = _push_paperclip(ctx)
    r2 = _push_paperclip(ctx)   # nothing changed -> must skip, not re-comment
    if not r1 or r1["actions"] < 1:
        failures.append(f"[idem] first push should act, got {r1}")
    if not r2 or r2["actions"] != 0 or r2["skipped_already_pushed"] < 1:
        failures.append(f"[idem] second push should skip the unchanged ticket, got {r2}")

    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — paperclip adapter not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — read/filter/map + outcome->status/comment + dry-run safety hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
