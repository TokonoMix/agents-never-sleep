"""Specialist reviewers — deterministic selection of WHICH review lenses apply to a change.

Sits between the light de-anchoring council and the heavy plan/diff council in the design's per-ticket
funnel. As with the council, the harness can't call LLMs — the AGENT runs each specialist review via
the tokonomix gateway. This module owns only the DETERMINISTIC half: pick which specialist lenses a
change needs (from the actual diff), plan them (model per role + est € cost), and fold their findings
into the SAME advisory trust-gating the council uses (a security/architect/tenant concern → daylight
review; never blocks the run).

Architect + security are the DEFAULT pair on any non-trivial change. The rest are CONDITIONAL — added
only when the diff actually touches their domain — so a CSS tweak doesn't pay for an i18n review.
"""
from __future__ import annotations

import dataclasses
import enum
import re


class SpecialistRole(str, enum.Enum):
    ARCHITECT = "architect"
    SECURITY = "security"
    TENANT = "tenant-safety"
    MOBILE = "mobile-responsive"
    UX = "ux-accessibility"
    I18N = "i18n"
    PERF = "performance"
    SEO = "seo"


# Conditional roles keyed by a regex over (changed file paths + diff body). Architect + security are
# unconditional on any non-trivial change and are NOT in this table.
_CONDITIONAL = {
    SpecialistRole.TENANT: re.compile(
        r"(tenant|isolation|\brls\b|row.level.security|company_id|owner_tenant|workspace_id|"
        r"\borg_id\b|multi.?tenant)", re.IGNORECASE),
    SpecialistRole.MOBILE: re.compile(
        r"(\.css$|\.scss$|@media|viewport|responsive|flex-|grid-template|min-width|max-width|"
        r"breakpoint|tailwind)", re.IGNORECASE),
    SpecialistRole.UX: re.compile(
        r"(\.tsx$|\.jsx$|\.vue$|\.svelte$|component|aria-|role=|accessib|wcag|focus|keyboard|"
        r"alt=|label)", re.IGNORECASE),
    SpecialistRole.I18N: re.compile(
        r"(i18n|locale|translation|messages?\.json|\bt\(|gettext|\b__\(|intl|en-US|zh-CN|"
        r"pluraliz)", re.IGNORECASE),
    SpecialistRole.PERF: re.compile(
        r"(n\+1|select \*|\.map\(.*await|for .*await|cache|index on|full.scan|pagination|"
        r"lazy.?load|debounce|memoiz|\bO\(n)", re.IGNORECASE),
    SpecialistRole.SEO: re.compile(
        r"(<meta|sitemap|robots\.txt|canonical|og:|twitter:|structured.data|json-ld|"
        r"\bhreflang\b|<title>)", re.IGNORECASE),
}

# Lenses whose CONCERN should force a human daylight review (high blast radius), as opposed to a
# merely-low-confidence flag. Security/architect/tenant changes that worry a reviewer must not auto-merge.
DAYLIGHT_ROLES = frozenset({SpecialistRole.ARCHITECT, SpecialistRole.SECURITY, SpecialistRole.TENANT})

_DOC_ONLY = re.compile(r"\.(md|markdown|rst|txt|adoc)$", re.IGNORECASE)


@dataclasses.dataclass
class SpecialistPlan:
    roles: list
    model_by_role: dict
    est_cost_eur: float

    def summary_line(self) -> str:
        if not self.roles:
            return "[specialists] none — trivial/doc change"
        return (f"[specialists] {','.join(r.value for r in self.roles)} · "
                f"est≈€{self.est_cost_eur:.2f}")


def select_from_diff(changed_files: list, diff_text: str) -> list:
    """Which specialist lenses this change needs, from the ACTUAL diff. Architect + security on any
    non-trivial change; conditional roles only when their domain is actually touched. [] for docs."""
    files = [f for f in (changed_files or []) if f.strip()]
    if not files or all(_DOC_ONLY.search(f) for f in files):
        return []
    blob = "\n".join(files) + "\n" + (diff_text or "")
    roles = [SpecialistRole.ARCHITECT, SpecialistRole.SECURITY]
    for role, pattern in _CONDITIONAL.items():
        if pattern.search(blob):
            roles.append(role)
    return roles


def pre_work_hint(ticket_text: str) -> list:
    """Non-binding hint at `next` so the agent can line up the right lenses before implementing.
    Always suggests architect + security; adds a conditional role if the ticket text signals it."""
    roles = [SpecialistRole.ARCHITECT, SpecialistRole.SECURITY]
    for role, pattern in _CONDITIONAL.items():
        if pattern.search(ticket_text or ""):
            roles.append(role)
    return roles


def enabled(config: dict) -> bool:
    sp = config.get("specialists", {}) or {}
    if not sp.get("enabled"):
        return False
    return bool((config.get("integrations", {}).get("tokonomix", {}) or {}).get("enabled", True))


def plan(config: dict, roles: list) -> SpecialistPlan:
    sp = config.get("specialists", {}) or {}
    default_model = sp.get("default_model", "gpt-5.4-mini")
    model_by = sp.get("model_by_role", {}) or {}
    prices = (config.get("council", {}) or {}).get("prices_cents_per_mtok", {}) or {}
    est_prompt = sp.get("est_prompt_tokens", 2500)
    out_tok = sp.get("max_tokens", 700)
    markup = (config.get("council", {}) or {}).get("markup_factor", 2.5)
    model_by_role, cents = {}, 0.0
    for role in roles:
        m = model_by.get(role.value, default_model)
        model_by_role[role.value] = m
        pin, pout = prices.get(m, [75, 450])[:2]
        cents += (est_prompt / 1_000_000) * pin + (out_tok / 1_000_000) * pout
    return SpecialistPlan(roles=roles, model_by_role=model_by_role,
                          est_cost_eur=round((cents / 100.0) * markup, 4))


def coverage_tag(roles: list) -> str:
    """A coverage fragment recording which specialist lenses applied to this change."""
    return "specialists:" + (",".join(r.value for r in roles) if roles else "none")


def parse_roles(role_strings: list) -> list:
    """Map agent-reported role strings (e.g. from `--specialist-concerns security,architect`) to
    SpecialistRole, tolerating either the value ("tenant-safety") or the name ("TENANT"); unknown
    tokens are ignored rather than crashing the record step."""
    out = []
    for s in (role_strings or []):
        key = (s or "").strip().lower()
        if not key:
            continue
        for role in SpecialistRole:
            if key in (role.value, role.name.lower()):
                if role not in out:
                    out.append(role)
                break
    return out


def daylight_concerns(concern_roles: list) -> list:
    """Of the agent-reported specialist concerns, the subset that FORCE a human daylight review:
    architect / security / tenant-safety. A reported concern in these high-blast-radius lenses must
    never auto-merge — it is folded into the same advisory trust-gating the council uses (it withholds
    the trust upgrade; it never blocks the run)."""
    return [r for r in parse_roles(concern_roles) if r in DAYLIGHT_ROLES]
