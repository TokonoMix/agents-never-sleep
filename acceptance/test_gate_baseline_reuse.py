#!/usr/bin/env python3
"""Gate-baseline reuse cache (Q&A item 14): a green `complete` seeds the NEXT ticket's baseline
so the full gate suite doesn't have to run twice per ticket. On ANY doubt this must fall back
to running the gate for real — a wrong reuse would poison the FAIL_INTRODUCED_BY_DIFF /
FAIL_PREEXISTING taxonomy gates.py exists to protect.

Covers: a reuse-hit (gate genuinely skipped), every fall-back-to-real-gate case (tree changed,
dirty tree, gate command changed, corrupt cache file), the flag defaulting off (byte-identical
behaviour), and a non-PASS complete never writing a PASS receipt.
"""
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

from agents_never_sleep import gate_cache  # noqa: E402
from agents_never_sleep.gates import GateRunner  # noqa: E402
from agents_never_sleep.ledger import AttemptLedger  # noqa: E402
from agents_never_sleep.orchestrator import Orchestrator, ProceedToken  # noqa: E402
from agents_never_sleep.state import OutcomeState, OutcomeStore  # noqa: E402

# A gate that fails iff a BREAK marker file exists in the repo — lets a test deterministically
# flip the post-edit gate red without depending on a real test framework in a throwaway repo.
_GATE_CODE = "import sys, os; sys.exit(1 if os.path.exists('BREAK') else 0)"
_ALWAYS_GREEN = [sys.executable, "-c", "import sys; sys.exit(0)"]


class CountingGateRunner(GateRunner):
    """Counts baseline() calls so a reuse-hit can assert the real gate was never re-run."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.baseline_calls = 0

    def baseline(self, cwd):
        self.baseline_calls += 1
        return super().baseline(cwd)


def _init_repo(work: str) -> str:
    repo = os.path.join(work, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("value = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _build(work: str, *, reuse: bool):
    repo = _init_repo(work)
    state_dir = os.path.join(work, "state")
    artifacts_dir = os.path.join(work, "artifacts")
    store = OutcomeStore(state_dir)
    gate = CountingGateRunner(command=[sys.executable, "-c", _GATE_CODE], cwd=repo, timeout=30)
    ledger = AttemptLedger(os.path.join(state_dir, "ledger.json"))
    orch = Orchestrator(repo_dir=repo, store=store, gate=gate, worker=None,
                        artifacts_dir=artifacts_dir, unattended=True, ledger=ledger,
                        gate_baseline_reuse=reuse)
    return repo, store, gate, orch


def _ticket(tid: str):
    return types.SimpleNamespace(id=tid, title=tid, body="")


def _run_pass_ticket(orch, tid: str):
    """Drive one PROCEED ticket end-to-end with no edits: begin_proceed -> finalize, PASS."""
    ticket = _ticket(tid)
    token = orch.begin_proceed(ticket)
    if not isinstance(token, ProceedToken):
        raise AssertionError(f"setup ticket {tid} did not proceed: {token}")
    outcome = orch.finalize_after_edit(ticket, token, attempted=f"noop for {tid}")
    if outcome.state != OutcomeState.DONE:
        raise AssertionError(f"setup ticket {tid} expected DONE, got {outcome.state}")
    return token, outcome


def main() -> int:
    failures = []

    # --- config schema: the flag exists top-level and defaults OFF ---------------------------
    from agents_never_sleep.config import default_config
    profile = types.SimpleNamespace(gates=[], has_vault=False, has_tokonomix=False,
                                    has_paperclip=False)
    cfg = default_config(profile)
    if cfg.get("gate_baseline_reuse") is not False:
        failures.append(f"[config] default_config must carry top-level gate_baseline_reuse=False, "
                        f"got {cfg.get('gate_baseline_reuse')!r}")

    # --- a. reuse-hit: a green complete -> the next begin_proceed skips the baseline gate ----
    work = tempfile.mkdtemp(prefix="ue-gatecache-hit-")
    repo, store, gate, orch = _build(work, reuse=True)
    _run_pass_ticket(orch, "ticket-a")
    calls_before = gate.baseline_calls
    token_b = orch.begin_proceed(_ticket("ticket-b"))
    if not isinstance(token_b, ProceedToken) or token_b.baseline_green is not True:
        failures.append(f"[a] ticket-b expected a reused baseline_green=True, got {token_b}")
    if gate.baseline_calls != calls_before:
        failures.append(f"[a] baseline() was re-run despite a cache hit "
                        f"({calls_before} -> {gate.baseline_calls})")

    # --- b. tree changed (a real commit landed) -> cache-miss -> gate runs for real ----------
    work = tempfile.mkdtemp(prefix="ue-gatecache-treechange-")
    repo, store, gate, orch = _build(work, reuse=True)
    _run_pass_ticket(orch, "ticket-a")
    with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("value = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "manual drift"], cwd=repo, check=True)
    calls_before = gate.baseline_calls
    token_b = orch.begin_proceed(_ticket("ticket-b"))
    if gate.baseline_calls == calls_before:
        failures.append("[b] tree changed but baseline() was NOT re-run (stale reuse)")
    if not isinstance(token_b, ProceedToken) or token_b.baseline_green is not True:
        failures.append(f"[b] expected a real green baseline after the miss, got {token_b}")

    # --- c. dirty tree -> tree_id() itself refuses (the underlying guard, tested directly) ---
    # Not exercised via Orchestrator.begin_proceed: its own pre-snapshot commit_all() always
    # commits+cleans the tree before the reuse-check point, so a genuinely dirty tree can never
    # reach that check in the real flow. This proves the guard gate_cache relies on instead.
    work = tempfile.mkdtemp(prefix="ue-gatecache-dirty-")
    repo = _init_repo(work)
    if gate_cache.tree_id(repo) is None:
        failures.append("[c] a clean repo was reported dirty (tree_id returned None)")
    with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("uncommitted\n")             # dirty: NOT committed
    if gate_cache.tree_id(repo) is not None:
        failures.append("[c] a dirty tree must yield tree_id()=None (no reuse possible)")

    # --- d. different gate command -> cache-miss (same tree, command no longer matches) ------
    work = tempfile.mkdtemp(prefix="ue-gatecache-cmdchange-")
    repo, store, gate, orch = _build(work, reuse=True)
    _run_pass_ticket(orch, "ticket-a")
    gate.command = list(_ALWAYS_GREEN)          # same tree, DIFFERENT gate command
    calls_before = gate.baseline_calls
    token_b = orch.begin_proceed(_ticket("ticket-b"))
    if gate.baseline_calls == calls_before:
        failures.append("[d] gate command changed but baseline() was NOT re-run (stale reuse)")
    if not isinstance(token_b, ProceedToken) or token_b.baseline_green is not True:
        failures.append(f"[d] expected a real green baseline after the miss, got {token_b}")

    # --- e. flag default off -> behaviour byte-identical (no cache read/write side effects) --
    work = tempfile.mkdtemp(prefix="ue-gatecache-off-")
    repo, store, gate, orch = _build(work, reuse=False)
    if orch.gate_baseline_reuse:
        failures.append("[e] gate_baseline_reuse must default False")
    _run_pass_ticket(orch, "ticket-a")
    if os.path.exists(orch.gate_cache_path):
        failures.append("[e] flag off must never write the cache file")
    calls_before = gate.baseline_calls
    orch.begin_proceed(_ticket("ticket-b"))
    if gate.baseline_calls != calls_before + 1:
        failures.append(f"[e] flag off must always run the real baseline gate "
                        f"(calls {calls_before} -> {gate.baseline_calls})")

    # --- f. corrupt cache file -> fails safe to running the gate normally --------------------
    work = tempfile.mkdtemp(prefix="ue-gatecache-corrupt-")
    repo, store, gate, orch = _build(work, reuse=True)
    _run_pass_ticket(orch, "ticket-a")
    with open(orch.gate_cache_path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json::")
    calls_before = gate.baseline_calls
    token_b = orch.begin_proceed(_ticket("ticket-b"))
    if gate.baseline_calls == calls_before:
        failures.append("[f] a corrupt cache was trusted instead of falling back to the real gate")
    if not isinstance(token_b, ProceedToken) or token_b.baseline_green is not True:
        failures.append(f"[f] expected the real gate to still find a green baseline, got {token_b}")

    # --- g. a non-PASS complete does not write a PASS cache entry ---------------------------
    work = tempfile.mkdtemp(prefix="ue-gatecache-nonpass-")
    repo, store, gate, orch = _build(work, reuse=True)
    _run_pass_ticket(orch, "ticket-a")
    cache_before = gate_cache.read(orch.gate_cache_path)
    if not cache_before:
        failures.append("[g] setup: expected ticket-a to have written a cache entry")
    ticket_b = _ticket("ticket-b")
    token_b = orch.begin_proceed(ticket_b)
    with open(os.path.join(repo, "BREAK"), "w", encoding="utf-8") as fh:
        fh.write("break the gate\n")           # post-edit gate will now fail
    outcome_b = orch.finalize_after_edit(ticket_b, token_b, attempted="broke the gate")
    if outcome_b.state == OutcomeState.DONE:
        failures.append(f"[g] setup expected a non-DONE outcome, got {outcome_b.state}")
    cache_after = gate_cache.read(orch.gate_cache_path)
    if cache_after != cache_before:
        failures.append(f"[g] a non-PASS complete rewrote the cache: {cache_before} -> {cache_after}")

    # --- h. gitignored execution-relevant file -> tree_id() key changes -> cache-miss (t06) --
    # git status --porcelain (no --ignored) omits ignored files, so a clean plain-porcelain
    # tree can still have an agent-added gitignored sitecustomize.py/.env/conftest.py that
    # silently changes runtime/test behaviour -> a stale green would be served (false-green on
    # the ONLY hard gate). tree_id() must fold execution-relevant IGNORED files into its key so
    # such an add forces a miss. Controls prove the two boundaries: an UNTRACKED file is already
    # a miss (plain porcelain shows ??), and a BENIGN ignored file (debug.log, or an ignored
    # DIRECTORY like .unattended/) must NOT change the key (else the cache never hits in practice).
    work = tempfile.mkdtemp(prefix="ue-gatecache-ignored-")
    repo = _init_repo(work)
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("sitecustomize.py\n.env\n*.log\nbuildcache/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    key_clean = gate_cache.tree_id(repo)
    if key_clean is None:
        failures.append("[h] a clean repo (only a committed .gitignore) must yield a tree_id")
    # h1: a benign ignored file and an ignored directory must NOT move the key (cache still works)
    with open(os.path.join(repo, "debug.log"), "w", encoding="utf-8") as fh:
        fh.write("noise\n")
    os.makedirs(os.path.join(repo, "buildcache"), exist_ok=True)
    with open(os.path.join(repo, "buildcache", "out.o"), "w", encoding="utf-8") as fh:
        fh.write("artifact\n")
    if gate_cache.tree_id(repo) != key_clean:
        failures.append("[h1] a benign ignored file/dir must NOT change the cache key "
                        "(would disable the cache for every real run)")
    # h2: a gitignored execution-relevant file MUST move the key -> the next baseline misses
    with open(os.path.join(repo, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write("import os; os.environ['HACK'] = '1'\n")
    key_hacked = gate_cache.tree_id(repo)
    if key_hacked is None:
        failures.append("[h2] a gitignored sitecustomize.py must still be cacheable (a NEW key), "
                        "not disable the cache")
    if key_hacked == key_clean:
        failures.append("[h2] a gitignored execution-relevant file did NOT change the cache key "
                        "-> false-green hole on the only hard gate")
    # h3: mutating that file's CONTENT must move the key again (content is in the digest)
    with open(os.path.join(repo, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write("import os; os.environ['HACK'] = '2'\n")
    if gate_cache.tree_id(repo) == key_hacked:
        failures.append("[h3] changing the ignored file's content did NOT change the key")
    # h4 (control): an UNTRACKED (non-ignored) file is already a miss via plain porcelain
    os.remove(os.path.join(repo, "sitecustomize.py"))
    with open(os.path.join(repo, "newmod.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    if gate_cache.tree_id(repo) is not None:
        failures.append("[h4] an untracked file must yield tree_id()=None (already caught)")

    if failures:
        print("RESULT: ❌ RED — gate-baseline-reuse cache not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — gate-baseline reuse hits/misses/fail-safes all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
