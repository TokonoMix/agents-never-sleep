"""ASK / PARK / HALT — three distinct coded states (never collapsed).

The council's most important contradiction-fix: a junior reading "never stop" + "park ticket"
in prose will implement "park" as "stop the run" and invert the whole spine. So the three
states are explicit here:

  ASK  - ask the human. FORBIDDEN in unattended mode. The harness must never emit this unattended.
  PARK - defer THIS decision/ticket; the run keeps moving to the next independent ticket.
  HALT - stop the WHOLE run. Only on irreversible-damage-at-hook or no-safety-net (read-only fs).

Blast-radius tiering is made CONCRETE (enumerated hard-PARK categories) so the agent rarely
lands in "unsure" — the council's fix for both the safety risk and the park-starvation risk.
This MVP classifier is keyword/heuristic based; in production the agent supplies the
classification, but the contract (these three states, never-ask-unattended) is identical.
"""
from __future__ import annotations

import dataclasses
import enum
import re

from .state import ContaminationScope


class Action(str, enum.Enum):
    PROCEED = "PROCEED"   # assume + do (low blast-radius, reversible)
    PARK = "PARK"         # defer this ticket/decision
    HALT = "HALT"         # stop the whole run (irreversible / no safety net)
    ASK = "ASK"           # interactive only; never returned in unattended mode


@dataclasses.dataclass
class Decision:
    action: Action
    why: str
    category: str = ""
    foundational: bool = False
    contamination_scope: ContaminationScope = ContaminationScope.NONE
    # F5 (build-narrow): True ONLY on the requirement_meaning PARK branch — the one place a
    # consensus-assisted disambiguation is safe (FILE-scoped, reversible). decide.py just TAGS it;
    # the F5 logic + the consensus call live outside the pure classifier (harness/f5.py + driver).
    consensus_resolvable: bool = False
    # Optional human-facing next-step for a PARK (what the human must DECIDE/GRANT). When blank the
    # orchestrator falls back to a generic "decide: <title>". Set for cross_project_op so the parked
    # ticket surfaces the specific Rule #11 grant it needs, not a generic prompt.
    human_action: str = ""


# Enumerated hard-PARK categories. Each maps to (regex, foundational?, scope).
#
# INT-1825 bug 1: two tokens were narrowed because they collide with everyday engineering jargon and
# caused false-PARKs. Bare `schema` matched "JSON-Schema" (INT-1781); bare `isolation` matched
# container/network "process isolation" (s2-01). Both are now phrase-anchored to real DB/tenant
# context so genuine schema-migration / tenant-isolation work STILL parks, but the jargon does not.
HARD_PARK_CATEGORIES = {
    "db_schema_or_migration": (r"\b(migrat|alter table|drop column|add column|create table|database schema|db schema|schema migration|schema change)\b", True, ContaminationScope.SERVICE),
    "api_contract": (r"\b(api contract|response shape|request shape|public api|endpoint contract)\b", True, ContaminationScope.SERVICE),
    "security_or_tenant": (r"\b(auth|authz|permission|tenant|rbac|access control|jwt|session|tenant isolation|data isolation)\b", True, ContaminationScope.SERVICE),
    "money_or_billing": (r"\b(discount|billing|price|pricing|invoice|payment|charge|refund|tax|vat)\b", False, ContaminationScope.MODULE),
    "cross_ticket_interface": (r"\b(shared interface|cross-ticket|other tickets depend|breaking change)\b", True, ContaminationScope.PACKAGE),
}

# Signals that a ticket's REQUIREMENT MEANING is ambiguous (we don't know WHAT to build).
AMBIGUITY_SIGNALS = (
    r"\b(which|what kind|unclear|ambiguous|tbd|decide|undecided|some sort of|or something)\b",
    r"\?\s*$",
)


# --- Cross-project / irreversible OP signals -----------------------------------------------------
# A DELIBERATE MIRROR of the COMMAND categories in agentixmesh's CPS confirm-gate
# (`pm_mesh/cross_project_guard.py`: _SERVICE_CTL_RE / _HOST_CTL_RE / _DB_CLIENT_RE + _SQL_DDL_RE).
# That hook HARD-DENIES these unattended (a per-action human "ask" can't be answered at 2am, and
# cross-project mutation is never autonomous — Rule #11). ANS is stdlib-only + standalone so we
# cannot import it; these patterns are copied and CAN DRIFT — the authoritative source is that file.
#
# We mirror ONLY the command-anchored, pure-text categories (service/host/db state-change). We do
# NOT mirror CPS's `foreign-write` path check: deciding "path outside the current project" needs the
# real filesystem + git-worktree boundary (a sibling worktree / the main checkout are NOT foreign),
# which a ticket-text classifier cannot know without false-parking. So foreign-write — the most
# common but least prose-visible deny — stays CPS's reactive job. This precheck is the high-precision
# PROACTIVE subset; CPS is the backstop for what prose cannot reveal. (Same honesty as CPS: the DB
# branch is gated on a real db-client word so prose like "delete the truncate helper" never matches.)
_XP_SERVICE_CTL_RE = re.compile(
    r"\b(?:systemctl|service)\b(?![^\n;&|]*--user(?![\w-]))[^\n;&|]*?"
    r"\b(?:start|stop|restart|try-restart|reload|reload-or-restart|reload-or-try-restart|"
    r"enable|disable|reenable|preset|preset-all|mask|unmask|daemon-reload|set-default|isolate|"
    r"kexec|revert|set-property|edit|link)\b",
    re.I)
_XP_HOST_CTL_RE = re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)(?:shutdown|reboot|halt|poweroff)\b", re.I)
_XP_DB_CLIENT_RE = re.compile(
    r"\b(?:psql|mysql|mariadb|mongo|mongosh|sqlite3|sqlcmd|cockroach|clickhouse-client)\b", re.I)
_XP_SQL_DDL_RE = re.compile(
    r"\bALTER\s+SYSTEM\b|\bALTER\s+DATABASE\b|\bALTER\s+(?:USER|ROLE)\b"
    r"|\bDROP\b|\bTRUNCATE\b|\bDELETE\s+FROM\b",
    re.I)


def _cross_project_op(text: str) -> str | None:
    """The CPS command-category a ticket NAMES (or None). Pure text, command-/client-anchored."""
    if _XP_SERVICE_CTL_RE.search(text):
        return "service-control"
    if _XP_HOST_CTL_RE.search(text):
        return "host-control"
    if _XP_DB_CLIENT_RE.search(text) and _XP_SQL_DDL_RE.search(text):
        return "database-ddl"
    return None


def _matches(patterns, text) -> bool:
    return any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in patterns)


_OVERRIDE_ACTIONS = {"PROCEED": Action.PROCEED, "PARK": Action.PARK, "HALT": Action.HALT}


def classify(ticket_text: str, *, unattended: bool, has_safety_net: bool,
             override: str | None = None) -> Decision:
    """Decide ASK/PARK/HALT for a ticket. `ticket_text` = title + body.

    `override` (INT-1825 bug 1): an OPERATOR-supplied classification for THIS ticket, sourced only
    from trusted config (`classify.overrides` keyed by ticket id) — never from the agent at runtime,
    which would let an agent loosen the very PARK gate that exists to restrain it. A valid override
    forces the action and short-circuits the heuristic. It CANNOT bypass the no-safety-net HALT:
    without reversibility nothing is safe to do, so that guard runs first."""
    text = ticket_text.lower()

    # HALT: only when we cannot guarantee reversibility at all. Without a safety net even a
    # "reversible" assumption is not actually reversible -> do not risk destructive work.
    # This precedes the operator override on purpose: an override may not authorise unrevertible work.
    if not has_safety_net:
        return Decision(Action.HALT, "no VCS/backup safety net — cannot guarantee reversibility",
                        category="no_safety_net")

    if override:
        action = _OVERRIDE_ACTIONS.get(override.strip().upper())
        if action is not None:
            return Decision(action, f"operator classification override: {action.value}",
                            category="operator_override")

    # Cross-project / irreversible OP named in the ticket (service/host/db state-change).
    # UNATTENDED-ONLY on purpose: the CPS hook hard-denies these when no human can answer its
    # confirm. Parking proactively surfaces the ticket for a per-action Rule #11 grant BEFORE the
    # run wastes an attempt on a mid-run deny. In ATTENDED mode we do NOT pre-park — the human is
    # present and CPS's confirm-gate lets them approve it live, so deferring would be needlessly
    # conservative. (Unlike the blast-radius hard-PARK categories below, which defer regardless of
    # mode; this category's whole rationale is "can't ask unattended", so it is mode-gated.)
    if unattended:
        xp = _cross_project_op(text)
        if xp is not None:
            return Decision(
                Action.PARK,
                f"ticket names a cross-project / irreversible op ({xp}); the unattended CPS gate "
                f"hard-denies it mid-run — it needs a per-action Rule #11 human grant",
                category=f"cross_project_op:{xp}", foundational=True,
                contamination_scope=ContaminationScope.SERVICE,
                human_action=f"grant per-action Rule #11 approval for the {xp} op, or split it out of this ticket",
            )

    # Hard-PARK categories: high blast-radius regardless of how 'reversible' it looks.
    for cat, (pattern, foundational, scope) in HARD_PARK_CATEGORIES.items():
        if re.search(pattern, text, re.IGNORECASE):
            return Decision(Action.PARK, f"touches hard-PARK category: {cat}",
                            category=cat, foundational=foundational, contamination_scope=scope)

    # Requirement-meaning ambiguity on something NOT in a hard category:
    # hybrid is the default (build reversibly behind a flag + park the decision) ONLY when
    # locally reversible and isolated. We approximate "isolated" as: no hard category matched.
    if _matches(AMBIGUITY_SIGNALS, text):
        return Decision(Action.PARK, "requirement-meaning ambiguous; defer the decision",
                        category="requirement_meaning", foundational=False,
                        contamination_scope=ContaminationScope.FILE,
                        consensus_resolvable=True)

    # Otherwise: low blast-radius + reversible -> assume and proceed.
    return Decision(Action.PROCEED, "low blast-radius, reversible — assume + do",
                    category="routine", contamination_scope=ContaminationScope.FILE)
