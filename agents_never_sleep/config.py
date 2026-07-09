"""Per-project preferences + minimal first-run wizard (Thread 7, trimmed for MVP per council).

Config lives at <repo>/.claude/agents-never-sleep.json (per-project only — Mesut's choice).
The wizard front-loads all choices ONCE so nothing is asked during a run. It is INTERACTIVE-only:
the skill never self-schedules, so the first invocation is always interactive and the wizard
gets to run before any unattended run can exist. Defensive guard: unattended + no config →
non-destructive defaults only + a loud note (never guess for a real run).

MVP keeps the schema lean: gates, budgets, and which optional integrations are enabled. The full
council/specialist/Paperclip/Vault wiring is Phase 2; this records intent + references, never secrets.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

CONFIG_REL = os.path.join(".claude", "agents-never-sleep.json")
SCHEMA_VERSION = 1


def config_path(repo_dir: str) -> str:
    return os.path.join(repo_dir, CONFIG_REL)


def load_config(repo_dir: str) -> Optional[dict]:
    path = config_path(repo_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(repo_dir: str, config: dict) -> str:
    path = config_path(repo_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def default_config(profile) -> dict:
    """Conservative defaults derived from the capability profile (used as the wizard's
    starting point AND as the defensive unattended-no-config fallback)."""
    return {
        "schema_version": SCHEMA_VERSION,
        # Q&A item 14: reuse a green `complete`'s gate result as the next ticket's baseline
        # (skip the redundant re-run) when the tree + gate command are byte-identical. Off by
        # default — enabling it is a deliberate operator opt-in, not a silent speed-up.
        "gate_baseline_reuse": False,
        "gates": [
            {"name": name, "command": cmd, "blocking": name not in ("lint", "audit")}
            for name, cmd in profile.gates
        ],
        "budget": {
            "per_ticket_timeout_s": 1800,
            "per_ticket_fix_iterations": 3,
            "per_night_token_cap": None,
            "per_night_euro_cap": None,            # €cap on council spend (None = no € ceiling)
            "max_council_calls_per_night": 50,     # deterministic ceiling even with no €cap set
            "max_tickets_per_run": 20,             # hard cap: stop after N tickets so a surprise-large backlog doesn't consume the whole night
            "balance_threshold_euro": 1.0,         # agent stops convening councils below this balance
            "on_credits_exhausted": "stop",        # "stop" (A) or "degrade" (B) — see decide_budget
        },
        "classify": {
            "overrides": {},
            # Hard-PARK categories the project opted into a consensus-assisted resolution attempt
            # (Plan 2). Empty = today's behavior (only requirement_meaning is F5-eligible). Members
            # must be exact HARD_PARK_CATEGORIES keys — validated fail-fast at load (see
            # validate_consensus_config). requirement_meaning is never listed (always eligible).
            "consensus_assisted_categories": [],
        },
        "autonomy": {
            # unattended-no-config is conservative: non-destructive only until a human configures
            "non_destructive_only": True,
            "requirement_ambiguity": "hybrid",   # hybrid | park | assume
            # G4a live-tree isolation gate: what to do when a fresh run starts UNISOLATED (primary
            # worktree) in a DIRTY shared tree. warn (default, non-breaking: report + stderr, run
            # continues) | ack (operator acknowledged, silent) | require_isolation (HALT at start).
            "live_tree": "warn",
            # Parked-WIP protection (INT-1735): before the run, stash these TRACKED parked paths and
            # fence these untracked THROWAWAY globs into .git/info/exclude so the `git add -A`
            # snapshot never commits intentional WIP; restored after a terminal signal. Default off.
            "parked": {"enabled": False, "tracked_paths": [], "throwaway_globs": [],
                       "label": "agents-never-sleep-parked"},
        },
        "integrations": {
            "paperclip": {"enabled": False, "project_id": None, "token_ref": None,
                          "base_url": "http://localhost:3100", "company_id": None,
                          "write_enabled": False},   # write_enabled=False -> dry-run (no live mutation)
            "vault": {"enabled": bool(getattr(profile, "has_vault", False))},
            "tokonomix": {"enabled": bool(getattr(profile, "has_tokonomix", False)),
                          "token_ref": None, "council": [], "judges": [],
                          "pending_onboard": False},
        },
        # Multi-model council review (advisory; agent calls it via the tokonomix MCP gateway). Slugs
        # are a starting point — pull FRESH ones from tokonomix_list_models, they drift. Disabled
        # unless tokonomix is present; enabling it never blocks a run, only flags high-risk diffs.
        "council": {
            "enabled": bool(getattr(profile, "has_tokonomix", False)),
            "light": {"proposers": ["gpt-5.4", "gemini-2.5-pro", "deepseek/deepseek-v3.2"],
                      "judges": ["claude-sonnet-4-6", "gemini-2.5-pro"],
                      "mode": "consensus", "max_tokens": 900},
            "heavy": {"proposers": ["claude-opus-4-8", "gpt-5.4", "gemini-2.5-pro",
                                    "deepseek/deepseek-v3.2", "meta-llama/llama-4-maverick"],
                      "judges": ["claude-opus-4-8", "gpt-5.4", "gemini-2.5-pro"],
                      "mode": "consensus", "max_tokens": 1400},
            "prices_cents_per_mtok": {
                "claude-opus-4-8": [500, 2500], "gpt-5.4": [250, 1500],
                "gemini-2.5-pro": [125, 1000], "deepseek/deepseek-v3.2": [25, 38],
                "meta-llama/llama-4-maverick": [15, 60], "claude-sonnet-4-6": [300, 1500]},
            "est_prompt_tokens": 3000,
        },
        # Specialist review lenses (architect + security default; conditional ones added per-diff).
        # The agent runs each via tokonomix; a security/architect/tenant concern -> daylight review.
        "specialists": {
            "enabled": bool(getattr(profile, "has_tokonomix", False)),
            "default_model": "gpt-5.4-mini",
            "model_by_role": {"security": "claude-opus-4-8", "architect": "gpt-5.4"},
            "est_prompt_tokens": 2500,
            "max_tokens": 700,
        },
        "report": {"local_path": "night-report.md", "paperclip_parked_comments": False,
                   "push_on_finish": False},
        # Consumed by bin/ans-run (the pre-boot GO/NO-GO gate). Host-specific probes
        # (services/DB) belong in `checks` — never hardcoded in the launcher itself.
        # Agent selection is preset-based: every launchable preset needs a HUMAN-confirmed
        # autonomy decision (autonomy_confirmed) — the wizard scaffolds these from the
        # CLIs it finds installed. `env` is reserved for gateway delegation (phase 2):
        # values use env:/vault: indirection, never literal secrets.
        "launcher": {
            "target_user": None,
            "default_agent": None,
            # {name: {cmd, autonomy_confirmed, env}}. The scaffolded "managed" preset is
            # the Tokonomix-delegated tier: point the spawned CLI at an OpenAI-compatible
            # gateway so model routing, budget caps, EU-residency and billing are governed
            # centrally on ONE gateway token. The key is a TOKEN-REF (env:VAR /
            # vault:path#field) resolved by ans-run at spawn — NEVER a literal secret in
            # this file (a pasted literal is flagged at launch). The DIY path with your own
            # provider keys stays fully functional: edit or replace this preset. It cannot
            # launch until a human confirms its autonomy flag (autonomy_confirmed).
            "agents": {
                "managed": {
                    "cmd": ["codex", "exec", "--sandbox", "workspace-write"],
                    "env": {"OPENAI_BASE_URL": "https://gateway.tokonomix.ai/v1",
                            "OPENAI_API_KEY": "env:TOKONOMIX_API_KEY"},
                    "autonomy_confirmed": False,
                    "_doc": "managed gateway tier — token-ref key only, never a literal; "
                            "see SKILL.md 'Tokonomix-delegated routing'",
                },
            },
            "allow_custom_agent": False,
            "credentials_paths": None,   # None = best-effort probe (warn-only)
            "min_disk_mb": 1000,
            "log_dir": None,             # None = <repo>/.unattended/logs
            "checks": [],                # [{"name", "command", "blocking"}]
        },
        "_note": "Generated defaults. Run the wizard (interactive) to confirm/extend.",
    }


def enable_tokonomix_review(cfg: dict) -> None:
    """Flip the three tokonomix-review consumers ON in-place. Used when a credential becomes present
    (Paste path at wizard time, or the ensure_config re-probe after a keyless onboard). Idempotent."""
    cfg.setdefault("integrations", {}).setdefault("tokonomix", {})["enabled"] = True
    cfg.setdefault("council", {})["enabled"] = True
    cfg.setdefault("specialists", {})["enabled"] = True


def validate_consensus_config(config: dict) -> None:
    """Fail-fast on a misconfigured consensus-assisted opt-in. A safety toggle must NEVER silently
    no-op: a typo'd or unknown category, or a `requirement_meaning` entry (eligible by definition —
    naming it signals a misunderstanding), is a hard config error surfaced at load, never ignored."""
    from .decide import HARD_PARK_CATEGORIES
    # Only a genuinely-absent key defaults to []; a present-but-wrong-type value (including falsy
    # ones like {}, "", 0, False) must reach the isinstance check and raise — never silently no-op.
    cats = (config.get("classify") or {}).get("consensus_assisted_categories", [])
    if not isinstance(cats, list):
        raise ValueError("classify.consensus_assisted_categories must be a list")
    valid = set(HARD_PARK_CATEGORIES)
    for entry in cats:
        if entry == "requirement_meaning":
            raise ValueError(
                "classify.consensus_assisted_categories must not list 'requirement_meaning' "
                "— it is always F5-eligible by definition")
        if entry not in valid:
            raise ValueError(
                f"classify.consensus_assisted_categories has unknown category {entry!r}; "
                f"valid keys: {sorted(valid)}")


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CLAUDE_UNATTENDED")


def run_wizard(repo_dir: str, profile) -> dict:
    """Minimal interactive wizard. Refuses to run unattended (returns conservative defaults)."""
    cfg = default_config(profile)
    if not is_interactive():
        cfg["autonomy"]["non_destructive_only"] = True
        cfg["_note"] = ("UNATTENDED with no config: running NON-DESTRUCTIVE only on conservative "
                        "defaults. Run the wizard interactively to enable full autonomy.")
        return cfg

    def ask(prompt, default):
        ans = input(f"{prompt} [{default}]: ").strip()
        return ans or default

    print("=== agents-never-sleep: first-run setup (per-project) ===")
    print(f"Detected platform: {profile.platform} | exec mode: {profile.exec_mode} | "
          f"expected yield: {profile.expected_yield}")
    if profile.warnings:
        print("Warnings:")
        for w in profile.warnings:
            print(f"  - {w}")
    print(f"Detected gates: {[g['name'] for g in cfg['gates']] or 'NONE'}")

    cfg["autonomy"]["non_destructive_only"] = ask(
        "Allow file-writing autonomy? (y/n)", "y").lower().startswith("n")
    cfg["autonomy"]["requirement_ambiguity"] = ask(
        "Requirement-ambiguity policy (hybrid/park/assume)", "hybrid")
    if profile.has_paperclip:
        pc = cfg["integrations"]["paperclip"]
        pc["enabled"] = ask("Enable Paperclip integration? (y/n)", "n").lower().startswith("y")
        if pc["enabled"]:
            pc["project_id"] = ask("Paperclip project id", "") or None
            # Generic default: conventional `secret/` KV mount. Sites with a different KV mount edit this ref; env:PAPERCLIP_TOKEN is the no-Vault fallback.
            pc["token_ref"] = ("vault:secret/paperclip/board-token" if profile.has_vault
                               else "env:PAPERCLIP_TOKEN")
    if profile.has_tokonomix:
        cfg["integrations"]["tokonomix"]["enabled"] = ask(
            "Enable tokonomix council? (y/n)", "y").lower().startswith("y")
        if cfg["integrations"]["tokonomix"]["enabled"]:
            print("")
            print("Credits-exhaustion policy (applies when Tokonomix balance runs out mid-run):")
            print("  (A) stop    — stop on time, update ticket/Paperclip status, and stop cleanly.")
            print("                No new councils started; the current ticket finishes safely.")
            print("  (B) degrade — continue WITHOUT consensus. The local agent does the work itself;")
            print("                councils are skipped and affected tickets are recorded")
            print("                DONE_LOW_CONFIDENCE ('unverified, needs daylight review').")
            ans = ask("When credits run out, should I: (A) stop or (B) degrade?", "A")
            cfg["budget"]["on_credits_exhausted"] = (
                "degrade" if ans.strip().upper().startswith("B") else "stop")

        cfg.setdefault("specialists", {})["enabled"] = ask(
            "Enable specialist reviewer lenses (architect/security/etc.)? (y/n)", "y"
        ).lower().startswith("y")

        from .decide import HARD_PARK_CATEGORIES
        explain = {
            "db_schema_or_migration": "database schema / migration changes",
            "api_contract": "public API request/response shape changes",
            "security_or_tenant": "auth, permissions, or tenant-isolation changes",
            "money_or_billing": "pricing, billing, invoicing, or payment changes",
            "cross_ticket_interface": "shared interfaces other tickets depend on",
        }
        opted = []
        print("  For each high-risk area, ANS normally STOPS and waits for you. You can instead let")
        print("  ANS attempt a multi-model consensus resolution and apply it unattended — the result")
        print("  is always flagged for your daylight review, and git is the reverse button.")
        for cat in HARD_PARK_CATEGORIES:
            if ask(f"  Allow consensus-assisted resolution for {explain[cat]}? (y/n)",
                   "n").lower().startswith("y"):
                opted.append(cat)
        cfg.setdefault("classify", {})["consensus_assisted_categories"] = opted
    else:
        # Keyless first-run: no credential detected. Offer once (interactive only — we are already
        # past the is_interactive() bail). Never runs MCP / never accepts beta terms; the agent+human
        # do that. Skip is the default (bare Enter = today's fully-functional, review-OFF behaviour).
        from . import onboarding
        offer = onboarding.first_run_offer()
        print("")
        print(offer["summary"])
        print("  1) Create a free Tokonomix account now (keyless onboard — needs your email + a")
        print("     6-digit code + one beta-terms confirmation; review activates after a reload).")
        print("  2) Paste an existing key (set TOKONOMIX_API_KEY or drop it in ~/.tokonomix/credentials.json).")
        print("  3) Skip — proceed with council/specialist/F5-consensus review OFF (you can enable it")
        print("     later by re-running this setup, or it auto-enables once a key is present).")
        choice = ask("Choose 1/2/3", "3").strip()
        if choice == "1":
            print("")
            print(offer["protocol"])
            cfg["integrations"]["tokonomix"]["pending_onboard"] = True
            print("  → Recorded: review will auto-enable on the next run once the key is in place.")
        elif choice == "2":
            input("  Put the key in place (env TOKONOMIX_API_KEY or ~/.tokonomix/credentials.json), "
                  "then press Enter... ")
            if onboarding.credential_present():
                enable_tokonomix_review(cfg)
                print("  → Key detected — multi-model review enabled.")
            else:
                print("  → No key detected yet; leaving review OFF. Re-run setup once the key is in place.")
        # choice 3 / anything else: Skip — leave review OFF, no marker, zero MCP activity.

    # Launcher presets: scaffold one preset per INSTALLED known agent CLI, and make the
    # autonomy decision explicit per CLI (security review 2026-06-10: autonomy flags are
    # never applied silently — the human sees what the flag grants and confirms).
    from .agent_clis import AGENT_CLIS, detect_session_platform, installed_clis
    launcher = cfg["launcher"]
    found = installed_clis()
    if found:
        print("")
        print("Detached runs (bin/ans-run) need an agent CLI + an explicit autonomy choice.")
        for name in found:
            spec = AGENT_CLIS[name]
            print(f"  {name}: unattended needs `{' '.join(spec['cmd_unattended'])}`")
            print(f"    this flag: {spec['grants']}")
            confirmed = ask(f"  Confirm this autonomy flag for {name}? (y/n)",
                            "n").lower().startswith("y")
            launcher["agents"][name] = {
                "cmd": spec["cmd_unattended"] if confirmed else spec["cmd_safe"],
                "autonomy_confirmed": confirmed,
                "env": {},
            }
            if not confirmed:
                print(f"    {name} saved WITHOUT autonomy — detached launches with it "
                      "will refuse until you confirm (re-run the wizard or edit the "
                      "config and re-trust).")
        hint = detect_session_platform()
        default = hint if hint in launcher["agents"] else found[0]
        launcher["default_agent"] = ask("Default agent preset for detached runs",
                                        default) or default
    else:
        print("No known agent CLI found in PATH (claude/codex/gemini/copilot) — "
              "detached launches will refuse until launcher.agents is configured.")

    save_config(repo_dir, cfg)
    print(f"Saved config to {config_path(repo_dir)}")
    # The wizard wrote this config in an interactive human session — record TOFU trust so
    # the first detached run does not bounce on the config the human just authored.
    from .trust import record_trust
    record_trust(repo_dir, config_path(repo_dir))
    return cfg


def ensure_config(repo_dir: str, profile) -> dict:
    existing = load_config(repo_dir)
    if existing:
        # Re-activation (keyless onboard completed between runs): a durable pending_onboard marker
        # means "the newcomer chose Create; enable review as soon as the key exists". Silent (no
        # prompt) so it is safe in unattended runs too. Any flip re-records TOFU trust — trust.py
        # keys on the sha256 of the config bytes, so a silent flip would otherwise bounce the next
        # detached run as untrusted.
        tok = (existing.get("integrations") or {}).get("tokonomix") or {}
        if tok.get("pending_onboard"):
            from . import onboarding
            if onboarding.credential_present():
                enable_tokonomix_review(existing)
                existing["integrations"]["tokonomix"]["pending_onboard"] = False
                save_config(repo_dir, existing)
                from .trust import record_trust
                record_trust(repo_dir, config_path(repo_dir))
        return existing
    if is_interactive():
        return run_wizard(repo_dir, profile)
    # unattended + no config: defensive defaults, do not persist (let a human run the wizard)
    return run_wizard(repo_dir, profile)
