#!/usr/bin/env python3
"""Canonical-JSON TOFU trust digest ACCEPTANCE test (task D).

Proves `trust.config_digest` hashes a canonical JSON normalization of the config instead of
raw bytes, so cosmetic re-saves (whitespace, key order) don't force a re-trust, while a
config that differs in ANY semantic value still yields a DIFFERENT digest (weaken nothing):

  * whitespace/key-order-only diff -> SAME digest (the win),
  * any changed value (command, autonomy flag, gate name, agent preset, or any other
    key/value) -> DIFFERENT digest (no weakening),
  * invalid JSON -> falls back to the raw-bytes sha256 deterministically (never crashes),
    and a semantic change in an unparseable file still produces a different fallback digest,
  * `data=` hashes the SAME buffer passed in (preserves the TOCTOU property: trusted content
    == executed content) rather than re-reading the file,
  * `is_trusted` / `record_trust` stay consistent with `config_digest` (round-trip).

Exit 0 = GREEN. NOT pytest — a plain main()/failures list, like the rest of acceptance/.
"""
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, SKILL_ROOT)

os.environ["ANS_TEST_MODE"] = "1"

from agents_never_sleep import trust  # noqa: E402


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_whitespace_and_key_order_collapse(failures):
    """Two configs identical except formatting/whitespace/key order must hash the SAME."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        a = os.path.join(work, "a.json")
        b = os.path.join(work, "b.json")
        _write(a, '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance"}}')
        _write(b, '{\n  "launcher":   {\n    "gate": "acceptance",\n    "agent_cmd": ["claude"]\n  }\n}\n')
        da = trust.config_digest(a)
        db = trust.config_digest(b)
        if da is None or db is None:
            failures.append(f"[cosmetic] expected digests, got {da!r} / {db!r}")
        elif da != db:
            failures.append(f"[cosmetic] whitespace/key-order-only diff must collapse to the "
                             f"same digest; got {da!r} != {db!r}")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_semantic_change_differs(failures):
    """Any changed value (command, autonomy flag, gate name, agent preset, arbitrary key)
    must still produce a DIFFERENT digest — the canonicalization must not weaken trust."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        base = os.path.join(work, "base.json")
        _write(base, '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance", '
                      '"autonomy": "supervised", "agent_preset": "default"}}')
        base_digest = trust.config_digest(base)

        variants = {
            "command": '{"launcher": {"agent_cmd": ["codex"], "gate": "acceptance", '
                        '"autonomy": "supervised", "agent_preset": "default"}}',
            "autonomy_flag": '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance", '
                              '"autonomy": "unattended", "agent_preset": "default"}}',
            "gate_name": '{"launcher": {"agent_cmd": ["claude"], "gate": "smoke", '
                          '"autonomy": "supervised", "agent_preset": "default"}}',
            "agent_preset": '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance", '
                             '"autonomy": "supervised", "agent_preset": "aggressive"}}',
            "extra_key": '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance", '
                          '"autonomy": "supervised", "agent_preset": "default", "extra": true}}',
        }
        for label, text in variants.items():
            path = os.path.join(work, f"{label}.json")
            _write(path, text)
            digest = trust.config_digest(path)
            if digest == base_digest:
                failures.append(f"[semantic:{label}] a changed value must change the digest; "
                                 f"stayed {digest!r}")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_invalid_json_falls_back_deterministically(failures):
    """Unparseable JSON must never crash config_digest — it falls back to the raw-bytes
    sha256, and that fallback must still be deterministic and still change on a semantic
    (byte-level) change, since there is no JSON structure left to canonicalize."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        bad = os.path.join(work, "bad.json")
        _write(bad, '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance"')  # truncated
        d1 = trust.config_digest(bad)
        d2 = trust.config_digest(bad)
        if d1 is None:
            failures.append("[invalid-json] invalid JSON must not return None (file exists)")
        if d1 != d2:
            failures.append(f"[invalid-json] fallback digest must be deterministic; "
                             f"{d1!r} != {d2!r}")
        expected_fallback = hashlib.sha256(
            b'{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance"').hexdigest()
        if d1 != expected_fallback:
            failures.append(f"[invalid-json] fallback must be sha256 of the raw bytes; "
                             f"got {d1!r}, expected {expected_fallback!r}")

        bad2 = os.path.join(work, "bad2.json")
        _write(bad2, '{"launcher": {"agent_cmd": ["codex"], "gate": "acceptance"')  # different, still truncated
        d3 = trust.config_digest(bad2)
        if d3 == d1:
            failures.append("[invalid-json] a semantic change in an unparseable file must "
                             f"still change the fallback digest; stayed {d3!r}")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_data_param_hashes_the_passed_buffer(failures):
    """`data=` must canonicalize/hash the EXACT buffer passed in, not re-read the file — this
    preserves the TOCTOU property (trusted content == executed content)."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        path = os.path.join(work, "cfg.json")
        on_disk = '{"launcher": {"agent_cmd": ["claude"]}}'
        _write(path, on_disk)

        different_buffer = b'{"launcher": {"agent_cmd": ["codex"]}}'
        d_from_data = trust.config_digest(path, data=different_buffer)
        d_from_disk = trust.config_digest(path)
        if d_from_data == d_from_disk:
            failures.append("[data-param] config_digest(path, data=...) must hash the passed "
                             "buffer, not re-read the file from disk")

        # Passing the on-disk bytes explicitly must match reading the file directly (same
        # canonicalization path both ways).
        same_buffer = on_disk.encode("utf-8")
        if trust.config_digest(path, data=same_buffer) != d_from_disk:
            failures.append("[data-param] hashing the on-disk bytes via data= must match "
                             "reading the file directly")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_type_distinct_values_do_not_collapse(failures):
    """Canonicalization must not merge distinct JSON types/literals that read similarly —
    true vs "true" vs 1, and 1 vs 1.0, must all keep producing different digests."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        variants = {
            "bool_true": '{"launcher": {"strict": true}}',
            "string_true": '{"launcher": {"strict": "true"}}',
            "int_one": '{"launcher": {"strict": 1}}',
            "float_one": '{"launcher": {"strict": 1.0}}',
        }
        digests = {}
        for label, text in variants.items():
            path = os.path.join(work, f"{label}.json")
            _write(path, text)
            digests[label] = trust.config_digest(path)
        if len(set(digests.values())) != len(digests):
            failures.append(f"[type-distinct] distinct JSON types/literals must not collapse "
                             f"to the same digest; got {digests!r}")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_missing_file_returns_none(failures):
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    try:
        missing = os.path.join(work, "does-not-exist.json")
        if trust.config_digest(missing) is not None:
            failures.append("[missing] config_digest on a nonexistent path must return None")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_record_and_check_trust_stay_consistent(failures):
    """record_trust and is_trusted both route through config_digest — a cosmetic re-save of
    an already-trusted config must still read as trusted afterward."""
    work = tempfile.mkdtemp(prefix="ans-trust-test-")
    trust_dir = os.path.join(work, "trust-store")
    try:
        repo = os.path.join(work, "repo")
        os.makedirs(repo, exist_ok=True)
        cfg_path = os.path.join(repo, "agents-never-sleep.json")
        _write(cfg_path, '{"launcher": {"agent_cmd": ["claude"], "gate": "acceptance"}}')

        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"ANS_TRUST_STORE": trust_dir}):
            trust.record_trust(repo, cfg_path)
            if not trust.is_trusted(repo, cfg_path):
                failures.append("[round-trip] freshly recorded trust must read as trusted")

            # Cosmetic re-save (reformatted, same semantics) -> still trusted.
            _write(cfg_path, '{\n  "launcher": {\n    "gate": "acceptance",\n    '
                              '"agent_cmd": ["claude"]\n  }\n}\n')
            if not trust.is_trusted(repo, cfg_path):
                failures.append("[round-trip] a cosmetic-only re-save must still be trusted "
                                 "(that is the whole point of canonicalizing)")

            # Semantic change -> no longer trusted.
            _write(cfg_path, '{"launcher": {"agent_cmd": ["codex"], "gate": "acceptance"}}')
            if trust.is_trusted(repo, cfg_path):
                failures.append("[round-trip] a semantic change must invalidate trust")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    failures = []
    test_whitespace_and_key_order_collapse(failures)
    test_semantic_change_differs(failures)
    test_invalid_json_falls_back_deterministically(failures)
    test_data_param_hashes_the_passed_buffer(failures)
    test_type_distinct_values_do_not_collapse(failures)
    test_missing_file_returns_none(failures)
    test_record_and_check_trust_stay_consistent(failures)
    print("=" * 60)
    if failures:
        print("RESULT: ❌ RED — canonical trust digest not proven")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: ✅ GREEN — config_digest canonicalizes JSON (cosmetic re-saves "
          "collapse to the same digest), any semantic change still differs, invalid JSON "
          "falls back to a deterministic raw-bytes hash, and record_trust/is_trusted stay "
          "consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
