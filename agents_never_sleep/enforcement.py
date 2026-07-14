"""Provider-neutral enforcement DECISIONS — the single source of truth every platform adapter shares.

Pure functions, no platform I/O. The cross-platform dispatcher (harness/enforce.py) normalises each
platform's hook payload to (event, tool_name, command) and calls `decide()`; this module owns WHAT the
answer is, never HOW a given platform expresses it.

The three guarantees:
  * never-ASK        — deny a tool that asks the human a question (PARK/PROCEED instead).
  * deny-irreversible — deny a genuinely irreversible / outward command.
  * never-stop       — block an end-of-turn while the run-incomplete sentinel exists.

NB: the Claude adapter predates this module as three proven bash hooks (hooks/*.sh). Two of them
(deny_ask.sh, stop_guard.sh) are simple enough (single tool-name / sentinel-file check) that they stay
standalone. deny_irreversible.sh used to keep its own copy of the irreversible patterns in bash
case/glob form; it now delegates to `python3 -m agents_never_sleep.enforce claude pre_tool` (the same
dispatcher every non-Claude platform hook already calls), so this module is the ONLY place the
irreversible pattern list lives — there is nothing left to drift out of sync. ALL new "never do"
patterns go into _IRREVERSIBLE below — not in SKILL.md or any hook file.
"""
from __future__ import annotations

import dataclasses
import enum
import re

# Tool names meaning "ask the human a question", across platforms (lower-cased match).
# `clarify` is Hermes's ask tool (tools/clarify_tool.py): denying it preempts Hermes's
# fail-open clarify-timeout (cli.py:8655 "use your best judgement and proceed" = invented
# consent). Adding it here enables it for every platform — harmless, no other platform
# exposes a tool named `clarify` (v1.1).
_ASK_TOOLS = {"askuserquestion", "ask_user", "clarify"}

# Irreversible / outward command patterns (case-insensitive) — the sole copy (see module docstring).
# Deliberately NOT matching local `git reset --hard` / `git clean` — that's the harness's own revert.
#
# These are shape-anchored, same philosophy as redact.py's patterns: a backstop against STANDARD-FORM
# shell invocations (whitespace variance, short-flag bundling/reordering like `-qf`/`-fr`, long- vs
# short-flag spelling, force-via-`+`refspec), not a boundary against every conceivable obfuscation — a
# command routed through a non-shell wrapper (python subprocess, a git alias, a base64-decoded script)
# never produces these literal substrings and cannot be caught by any string/regex matcher. See
# SECURITY.md.
#
# Each entry is (pattern, human-denial-reason, stable-slug). The slug is a consent-map key (see
# docs/superpowers/specs/2026-07-14-ans-unattended-safety-model.md Part B) — it must never change once
# shipped; the prose may be reworded freely. Finalized against spec §A.3/§A.4 (reviewed 3 external
# rounds + security/architecture lenses + cross-vendor consensus): the deny-list is the conservative
# SAFETY FLOOR, over-blocking is cheap because Part B (not yet built) lets a human pre-authorize a
# class for a run — "when in doubt, DENY" (§A.1).
_IRREVERSIBLE = [
    (re.compile(r"git\s+push\b.*(--force\b|--force-with-lease\b|\s-[a-z]*f[a-z]*\b|[\s'\"]\+[\w][\w./-]*)",
                re.I), "force-push", "force_push"),
    (re.compile(r"git\s+push\b.*(:\S|\s--delete\b)", re.I), "remote branch/tag delete", "remote_delete"),
    (re.compile(r"git\s+push\b.*--mirror\b", re.I), "mirror push", "mirror_push"),
    # Release-tag push (incl. --follow-tags) — permanent public publish; slash-prefixed version
    # BRANCH names (e.g. `git push origin v2-rewrite`) stay ALLOW (no dotted vX.Y shape).
    (re.compile(r"git\s+push\b(?:[^\n]*\s(?:--tags\b|--follow-tags\b)"
                r"|[^\n]*\s(?:tag\s+)?['\"]?(?:refs/tags/)?v?\d+\.\d+(?:\.\d+)?[\w.\-+]*['\"]?(?:\s|$))",
                re.I), "release-tag push", "release_tag_push"),
    # A lookahead for ANY recursive/force-ish flag (bundled, reordered, or separate/long-form tokens)
    # anywhere on the line, combined with a dangerous root/home path anywhere on the line — so
    # `rm -r -f /`, `rm --recursive --force /`, and `rm -fr /` all deny alike.
    (re.compile(r"\brm\b(?=[^\n]*(?:-[a-z]*[rf][a-z]*\b|--recursive\b|--force\b|--no-preserve-root\b))"
                r"[^\n]*[\s'\"](/|~|\$\{?HOME\b)", re.I),
     "recursive delete of a root/home path", "recursive_root_delete"),
    # find -delete / -exec rm on a genuine root/home path (a project subpath like `/var/www/proj` is
    # deliberately NOT matched — only exact top-level dirs / bare `/` / `~` / `$HOME`).
    (re.compile(r"\bfind\b[^\n]*[\s'\"](?:/(?:etc|usr|bin|sbin|lib|var|boot|dev|opt|root)?(?=[\s'\"]|$)"
                r"|~|\$\{?HOME\b)[^\n]*(-delete\b|-exec\s+rm\b)", re.I),
     "find -delete/-exec rm on a root/home path", "recursive_root_delete"),
    (re.compile(r"\b(drop\s+database|drop\s+table|truncate\s+table)\b", re.I),
     "destructive SQL", "destructive_sql"),
    (re.compile(r"\bmkfs\b|\bdd\b[^\n]*\bof=['\"]?/dev/|\bshred\b", re.I),
     "disk-destructive command", "disk_destructive"),
    # Vault delete/destroy + root-credential rotation + secret overwrite, mount-agnostic (`secret/` is
    # only the DEFAULT mount — deny any `vault kv put` / `vault write <mount>/` except the known
    # non-secret system mounts sys|auth|identity|cubbyhole).
    (re.compile(r"\bvault\s+(kv\s+)?(delete|destroy|metadata\s+delete)\b"
                r"|\bvault\s+write\b[^\n]*\brotate-root\b"
                r"|\bvault\s+kv\s+put\b"
                r"|\bvault\s+write\s+(?!(?:sys|auth|identity|cubbyhole)/)[\w-]+/", re.I),
     "Vault secret delete/rotate/overwrite", "vault_secret"),
    # Redis mass/unscoped flush — Redis is as often sessions/job-queues/locks as a cache; FLUSHALL is
    # the `truncate table` of Redis. Only `redis-cli … flush(all|db)` — plain `get/set/...` stays ALLOW.
    (re.compile(r"\bredis-cli\b[^\n]*\bflush(all|db)\b", re.I), "Redis mass flush", "redis_flush"),
    # A named docker volume IS the persistent data (`docker volume rm pgdata` = wiping the DB) — same
    # class as `drop database`. Bare `docker rm <container>` stays ALLOW.
    (re.compile(r"\bdocker\s+volume\s+rm\b", re.I), "Docker volume delete", "docker_volume_rm"),
    # Mail: single-send + queue/mass forms, command-anchored (anchor on sendmail/mailx/mail as a
    # COMMAND — start of statement, after `; & |` (+ optional space), or after `sudo` — with a
    # recipient/`-s`/stdin marker, NOT the toolname anywhere; so `git commit -m 'fix mailx bug'` does
    # NOT match and `mail a@b < body` / `echo hi | mail a@b` DO).
    (re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)(?:sendmail|mailx|mail)\b(?=[^\n]*(?:@|\s-s\b|<))"
                r"|\bpostqueue\b|\bpostsuper\b|\bpostfix\s+(flush|reload|stop)\b"
                r"|\bexim\b[^\n]*\s-M\b|\bsendmail\b[^\n]*\s-q", re.I),
     "sending real email", "send_email"),
    # Publishing — permanent, public, unpullable.
    (re.compile(r"\b(npm|pnpm|yarn|poetry|cargo|gem|flit)\s+publish\b|\bgh\s+release\s+create\b", re.I),
     "package/release publish", "publish_release"),
    # Infra teardown.
    (re.compile(r"\b(terraform|terragrunt)\s+destroy\b|\baws\s+s3\s+rb\b"
                r"|\baws\s+s3\s+rm\b[^\n]*--recursive\b", re.I),
     "infra teardown", "infra_teardown"),
    # kubectl narrowed to stateful/bulk kinds — `kubectl delete pod/job` stays ALLOW.
    (re.compile(r"\bkubectl\s+delete\b[^\n]*(\b(ns|namespace|pvc|pv|deployment|statefulset|secret)\b"
                r"|--all\b)", re.I), "kubectl stateful/bulk delete", "k8s_delete"),
    # Service/system teardown — `systemctl stop/restart`, bare `docker rm <container>`, and plain
    # `docker compose down` (no -v) stay ALLOW; only the mask/prune/volume-wiping/cron-wipe shapes deny.
    (re.compile(r"\bsystemctl\s+mask\b|\bcrontab\s+-r\b|\bdocker\s+system\s+prune\b"
                r"|\bdocker\s+compose\s+down\b[^\n]*(-v\b|--volumes\b)", re.I),
     "service/system teardown", "service_teardown"),
    # Power-state, command-anchored (avoids matching a script NAME like `run-reboot-check.sh`).
    (re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)(?:shutdown|reboot|halt|poweroff)\b", re.I),
     "host power-state change", "service_teardown"),
    (re.compile(r"\b(chmod|chown)\b(?=[^\n]*(?:-[a-z]*r[a-z]*\b|--recursive\b))"
                r"[^\n]*[\s'\"](/|~|\$\{?HOME\b)", re.I),
     "recursive permission/ownership rewrite of a root/home path", "perm_rewrite"),
]

ASK_DENY_REASON = (
    "agents-never-sleep: ASK is forbidden during an unattended run — there is nobody to answer. "
    "Do NOT ask. PARK this decision (record why, the candidate interpretations, the exact human "
    "next-action, and its contamination scope) and PROCEED to the next ticket; HALT only on "
    "genuinely irreversible danger.")

STOP_BLOCK_REASON = (
    "agents-never-sleep: backlog not drained — continue with the next ticket (PARK ambiguous / "
    "high-blast-radius items, do not stop or ask). When every ticket is in a terminal state, remove "
    ".unattended/run-incomplete and write the morning report.")


def irreversible_reason(kind: str) -> str:
    return (f"agents-never-sleep: blocked an irreversible/outward action ({kind}). "
            "Park it for human review instead.")


class Action(str, enum.Enum):
    ALLOW = "allow"   # let the tool/stop proceed
    DENY = "deny"     # block a tool/command
    BLOCK = "block"   # block a stop (keep working)


@dataclasses.dataclass
class Decision:
    action: Action
    reason: str = ""
    kind: str = ""   # the irreversible category, or "ask", when denied


def is_ask_tool(tool_name) -> bool:
    return (tool_name or "").strip().lower() in _ASK_TOOLS


def _irreversible_matches(command):
    """Every (kind, slug) pair from _IRREVERSIBLE the command matches, in list order."""
    if not command:
        return []
    return [(kind, slug) for pat, kind, slug in _IRREVERSIBLE if pat.search(command)]


def is_irreversible(command):
    """The stable slugs of every _IRREVERSIBLE pattern the command matches (empty list if none).

    Returns ALL matches, not just the first — the consent single-statement gate (t03) needs the
    full set so a payload touching two kinds can't ride a single consent.
    """
    return [slug for _, slug in _irreversible_matches(command)]


# t03 consent gate: separator/wrapper shapes that mean a command is NOT a single clean shell
# statement, so a consented slug's tail can hide an uncontested (unconsented) second command.
# Deliberately over-inclusive (same "when in doubt, DENY" posture as _IRREVERSIBLE) — a lone `&`
# is caught by the same `[;&|]` alternative that also catches `&&`/`||`'s constituent chars.
_STATEMENT_BREAK_RE = re.compile(r"&&|\|\||[;&|]|\$\(|`|<\(|<<|>")
# Interpreter `-c`/`-e`-style wrappers hide the real payload from the pattern matcher entirely
# (e.g. `bash -c "redis-cli flushall"` — the outer command is just `bash`). Never upgradeable.
_SHELL_WRAPPER_RE = re.compile(
    r"\b(?:sh|bash|dash|ksh|zsh|python[0-9.]*|perl|ruby|node)\s+(?:-\S+\s+)*-c\b", re.I)


def _is_single_clean_statement(command) -> bool:
    """True only for a command that is exactly ONE shell statement with no separator, no
    substitution/redirection, and no interpreter wrapper hiding the payload — the shape a
    consent upgrade is allowed to ride. A command fails this if ANY line (split on \\n) — or the
    whole string — contains \\r, a statement separator (;, &, &&, |, ||), command/process
    substitution ($(...), `...`, <(...)), a heredoc (<<), an output redirect (>, >>), or a shell
    wrapper (sh -c / bash -c / python -c / …)."""
    if not command or "\r" in command:
        return False
    lines = [ln for ln in command.split("\n") if ln.strip()]
    if len(lines) != 1:
        return False
    line = lines[0]
    if _STATEMENT_BREAK_RE.search(line):
        return False
    if _SHELL_WRAPPER_RE.search(line):
        return False
    return True


def decide(event: str, *, tool_name=None, command=None, sentinel_present: bool = False,
           consent=None) -> Decision:
    """The whole contract in one place.
      event="pre_tool" -> DENY an ask-tool or an irreversible command, else ALLOW.
      event="stop"     -> BLOCK while the run-incomplete sentinel exists, else ALLOW.

    `consent` (t03): a pre-authorized-action map ({slug: {"allowed": True}, ...}), read by the
    caller ONLY from the frozen UE_CONSENT env var (see enforce.py._consent) — never from
    anything repo-local. Defaults to None so every pre-t03 caller is unaffected (BC): with no
    consent, or with consent that doesn't single-slug-and-clean-statement-match, behavior is
    byte-identical to the plain deny-list floor.
    """
    if event == "stop":
        if sentinel_present:
            return Decision(Action.BLOCK, STOP_BLOCK_REASON)
        return Decision(Action.ALLOW)
    if event == "pre_tool":
        if is_ask_tool(tool_name):
            return Decision(Action.DENY, ASK_DENY_REASON, "ask")
        matches = _irreversible_matches(command)
        if not matches:
            return Decision(Action.ALLOW)
        kinds = [kind for kind, _ in matches]
        slugs = [slug for _, slug in matches]
        deny = Decision(Action.DENY, irreversible_reason(", ".join(kinds)), ",".join(slugs))
        if not consent:
            return deny
        distinct = set(slugs)
        if len(distinct) != 1:
            # A command touching two irreversible kinds can never ride a single consent, no
            # matter how the statement is shaped — this is the primary defense, ahead of the
            # single-statement check, so it backstops any gap in the separator/wrapper set.
            return deny
        slug = next(iter(distinct))
        if consent.get(slug, {}).get("allowed") is not True:
            return deny
        if not _is_single_clean_statement(command):
            return deny
        return Decision(
            Action.ALLOW,
            f"agents-never-sleep: consent-upgraded ({slug}) — a single clean shell statement "
            "matched exactly one pre-authorized action class.",
            f"consent:{slug}")
    return Decision(Action.ALLOW)
