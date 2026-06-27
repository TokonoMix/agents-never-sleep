"""Read work tickets from a local directory of .md files.

MVP work-source (the council said: prove the loop on local .md tickets before wiring
Paperclip). Each ticket is a markdown file with a simple `--- key: value ---` frontmatter
block followed by the body. We parse frontmatter by hand to avoid a PyYAML dependency.
"""
from __future__ import annotations

import dataclasses
import os
import re
from typing import Optional

# A declared agent hint must be a CLI/preset slug — see Ticket.declared_agent for why (it is
# interpolated into a copy-pasteable command in the report, so no shell-meta / whitespace / newline).
_AGENT_SLUG = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclasses.dataclass
class Ticket:
    id: str
    title: str
    body: str
    meta: dict
    path: str

    # convenience accessors used by the demo's assertions (NOT by the harness logic)
    @property
    def expected_outcome(self) -> Optional[str]:
        return self.meta.get("expected_outcome")

    @property
    def declared_blast_radius(self) -> Optional[str]:
        return self.meta.get("blast_radius")

    @property
    def declared_agent(self) -> Optional[str]:
        """F2-declarative: an optional `agent:` front-matter hint naming the agent CLI this
        ticket would prefer. Metadata ONLY — the run never switches CLIs mid-flight; the
        morning report just groups tickets whose hint differs from the active agent and
        recommends a focused `ans-run --agent <name>` follow-up. None when unset.

        A valid hint is a CLI/preset SLUG (`[A-Za-z0-9_.-]`). A ticket author controls this
        value, and it is interpolated into a copy-pasteable `ans-run --agent …` command in the
        report — so anything with a space, shell metacharacter or newline (which `redact()` does
        NOT neutralize — it scrubs secrets, not shell syntax) is treated as NO hint. That makes
        the boundary the single chokepoint: an unsafe value can never reach the report command,
        and it could not name a real preset anyway (council review 2026-06-10)."""
        agent = self.meta.get("agent")
        if not isinstance(agent, str):
            return None
        agent = agent.strip()
        return agent if (agent and _AGENT_SLUG.match(agent)) else None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading `--- key: value --- ` block. Guard against a file that merely OPENS with
    `---` used as a Markdown horizontal rule (no real frontmatter): only treat the fenced block as
    frontmatter if every non-empty line is a `key: value` pair; otherwise the whole file is body, so
    the agent never receives a silently-truncated ticket."""
    meta: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            non_empty = [ln for ln in block.splitlines() if ln.strip()]
            # Real frontmatter ALWAYS opens with a `key:` line; a prose/horizontal-rule file opens
            # with a sentence. Gate on the first line only, then parse leniently (continuation/list
            # lines without a colon are skipped, exactly as before) so valid frontmatter with wrapped
            # values is never discarded.
            if non_empty and re.match(r"^\s*[\w.\-]+\s*:", non_empty[0]):
                for line in block.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()
                return meta, body
            # not real frontmatter (prose / horizontal rule) -> treat the whole file as the body
    return meta, text


def load_tickets(tickets_dir: str) -> list[Ticket]:
    tickets = []
    for name in sorted(os.listdir(tickets_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(tickets_dir, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        meta, body = _parse_frontmatter(text)
        tid = meta.get("id") or os.path.splitext(name)[0]
        title = meta.get("title") or tid
        tickets.append(Ticket(id=tid, title=title, body=body, meta=meta, path=path))
    return tickets
